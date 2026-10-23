"""Local-only preparation. No URLs, network, credentials, or PDF JavaScript.

PDF text layers and DOCX text are read before rendering/embedded-picture OCR.
The caller owns the temporary directory and cleans it when the lease ends.
"""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import warnings
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from PIL import Image, ImageOps

MAX_PIXELS = 40_000_000
MAX_CHUNKS = 96
MAX_PAGES = 40
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def pdf_picture_state(resources, max_depth=8, max_objects=256):
    """Look through Form resource trees without executing document content.

    Unknown/malformed/cyclic trees are not evidence of a text-only page. They
    request the same bounded rendering used for scanned pages instead.
    """
    state = {'has_picture': False, 'uncertain': False, 'objects': 0, 'reasons': []}
    visited = set()

    def unknown(reason):
        state['uncertain'] = True
        if reason not in state['reasons']:
            state['reasons'].append(reason)

    def resolved(value):
        return value.get_object() if hasattr(value, 'get_object') else value

    def identity(reference, value):
        indirect = reference if hasattr(reference, 'idnum') else getattr(value, 'indirect_reference', None)
        return ('indirect', indirect.idnum, indirect.generation) if indirect is not None else ('direct', id(value))

    def walk(resource_value, depth, ancestors):
        if depth > max_depth:
            unknown('resource_depth_limit')
            return
        try:
            resource = resolved(resource_value)
            if not isinstance(resource, Mapping):
                unknown('resource_dictionary_invalid')
                return
            objects = resolved(resource.get('/XObject', {}))
            if not isinstance(objects, Mapping):
                unknown('xobject_dictionary_invalid')
                return
            for reference in objects.values():
                if state['objects'] >= max_objects:
                    unknown('xobject_count_limit')
                    return
                state['objects'] += 1
                value = resolved(reference)
                if not isinstance(value, Mapping):
                    unknown('xobject_dictionary_invalid')
                    continue
                key = identity(reference, value)
                if key in ancestors:
                    unknown('xobject_cycle')
                    continue
                if key in visited:
                    continue
                visited.add(key)
                subtype = value.get('/Subtype')
                if subtype == '/Image':
                    state['has_picture'] = True
                elif subtype == '/Form':
                    child_resources = value.get('/Resources')
                    if child_resources is None:
                        unknown('form_resources_missing')
                    else:
                        walk(child_resources, depth + 1, ancestors | {key})
                else:
                    unknown('xobject_subtype_unknown')
        except Exception:
            # The caller renders this page or returns a renderer failure. Do
            # not silently claim a malformed resource graph was read in full.
            unknown('resource_graph_unreadable')

    walk(resources if resources is not None else {}, 0, set())
    return state


def prepare(source, destination, poppler=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if source.stat().st_size > 8_000_000:
        raise ValueError('body_too_large')
    chunks, text_pages, diagnostics = [], [], []
    kind, pages = 'image', 1

    def image_chunks(image, page):
        image = ImageOps.exif_transpose(image).convert('RGB')
        if image.width * image.height > MAX_PIXELS:
            raise ValueError('poster_too_large')
        variants = [('native', image)]
        # A bounded second pass improves small poster typography. Preserve its
        # output independently, since added scale cannot restore absent pixels.
        scale = 1.5 if image.width <= 1400 else 1.25 if image.width <= 1800 else 1
        if scale > 1 and image.width * image.height * scale * scale <= MAX_PIXELS:
            enhanced = ImageOps.autocontrast(ImageOps.grayscale(image))
            enhanced = enhanced.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
            variants.append(('readable', enhanced.convert('RGB')))
        for variant, bitmap in variants:
            for top in range(0, bitmap.height, 2140):
                for left in range(0, bitmap.width, 2140):
                    if len(chunks) >= MAX_CHUNKS:
                        raise ValueError('poster_too_large')
                    tile = bitmap.crop((left, top, min(left + 2300, bitmap.width), min(top + 2300, bitmap.height)))
                    target = destination / ('chunk-%03d.png' % len(chunks))
                    tile.save(target)
                    chunks.append({'path': str(target), 'page': page, 'variant': variant, 'x': left, 'y': top})

    prefix = source.read_bytes()[:16]
    if prefix.startswith(b'%PDF-'):
        from pypdf import PdfReader
        kind = 'pdf'
        reader = PdfReader(str(source))
        if reader.is_encrypted:
            raise ValueError('document_encrypted')
        pages = len(reader.pages)
        if pages > MAX_PAGES:
            # Reject rather than claim that a partial extract covers the file.
            raise ValueError('document_too_many_pages')
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            picture_state = pdf_picture_state(page.get('/Resources'))
            has_picture = picture_state['has_picture']
            if text.strip():
                text_pages.append({'page': number, 'text': text, 'method': 'pdf_text_layer'})
                if not has_picture and not picture_state['uncertain']:
                    continue
            binary = poppler or shutil.which('pdftoppm')
            if not binary:
                raise ValueError('pdf_renderer_unavailable')
            target = destination / ('page-%03d' % number)
            result = subprocess.run([binary, '-f', str(number), '-l', str(number), '-r', '144', '-scale-to', '2600', '-singlefile', '-png', str(source), str(target)], timeout=20, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode != 0:
                raise ValueError('pdf_render_failed')
            with Image.open(str(target) + '.png') as image:
                image_chunks(image, number)
            reason = 'unknown_pdf_structure' if picture_state['uncertain'] else 'embedded_picture' if has_picture else 'no_text_layer'
            diagnostics.append({'page': number, 'method': 'pdf_render_ocr', 'reason': reason, 'resource_scan': picture_state})
    elif prefix.startswith(b'PK\x03\x04'):
        kind = 'docx'
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > 1000 or sum(m.file_size for m in members) > 40_000_000:
                raise ValueError('document_archive_too_large')
            if 'word/document.xml' not in archive.namelist():
                raise ValueError('document_format_unsupported')
            body = ET.fromstring(archive.read('word/document.xml'))
            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            paragraphs = [''.join(p.itertext()) for p in body.findall('.//w:p', ns)]
            text_pages.append({'page': 1, 'text': '\n'.join(paragraphs), 'method': 'docx_text'})
            images = [m for m in members if m.filename.startswith('word/media/') and Path(m.filename).suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff', '.webp')]
            if len(images) > 20:
                raise ValueError('document_too_many_images')
            for number, member in enumerate(images, 1):
                with Image.open(io.BytesIO(archive.read(member))) as image:
                    image_chunks(image, number)
            diagnostics.append({'method': 'docx_embedded_images', 'count': len(images)})
    else:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(source) as image:
                # Posters use the first frame. Animations are not static source
                # evidence; report that limitation rather than silently omit it.
                frames = getattr(image, 'n_frames', 1)
                if frames > 1:
                    raise ValueError('image_multiframe_unsupported')
                image_chunks(image, 1)
    manifest = destination / 'manifest.json'
    manifest.write_text(json.dumps(chunks, ensure_ascii=False), encoding='utf-8')
    return {'version': 1, 'kind': kind, 'pages': pages, 'manifest': str(manifest), 'chunks': len(chunks), 'text_pages': text_pages, 'diagnostics': diagnostics}



import time
import urllib.request
ORIGIN = 'https://zhiyu.ruiqiuguge.chatgpt.site'
TOKEN = os.environ.get('ZHIYU_COLLECT_TOKEN')
if not TOKEN:
    raise RuntimeError('missing_collector_configuration')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect)

def call(action, body=None, asset=False):
    request = urllib.request.Request(ORIGIN + '/api/service/cloud-reading/' + action,
        data=json.dumps(body or {}).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'})
    with opener.open(request, timeout=110) as response:
        data = response.read(8_000_001 if asset else 1_000_001)
        if len(data) > (8_000_000 if asset else 1_000_000):
            raise ValueError('body_too_large')
        value = data if asset else json.loads(data)
        return value if asset else value.get('data', value)

def ocr(raw, directory):
    source = Path(directory) / 'asset.bin'
    source.write_bytes(raw)
    value = prepare(source, Path(directory) / 'prepared')
    chunks = json.loads(Path(value['manifest']).read_text(encoding='utf-8'))
    texts = {'native': [], 'readable': []}
    for chunk in chunks:
        # Both the asset preparer and OCR subprocess have size/time bounds.
        result = subprocess.run(['tesseract', chunk['path'], 'stdout', '-l', 'chi_sim+eng',
            '--psm', '3' if chunk['variant'] == 'native' else '6'],
            capture_output=True, timeout=35, check=True)
        texts[chunk['variant']].append(result.stdout.decode('utf-8'))
    layer = '\n'.join(page['text'] for page in value['text_pages'])
    native = layer + '\n' + '\n'.join(texts['native'])
    alternate = layer + '\n' + '\n'.join(texts['readable'])
    text = native if native.strip() else alternate
    if not text.strip():
        raise ValueError('ocr_empty')
    if len(text) > 250_000:
        raise ValueError('ocr_text_too_large')
    return {'text': text, 'alternatives': [alternate] if texts['readable'] else [],
        'reader': {'engine': 'Cloud Tesseract', 'language': 'chi_sim+eng', 'kind': value['kind'],
            'pages': value['pages'], 'tiles': value['chunks'], 'truncated': False}}

if __name__ == '__main__':
    started = time.monotonic()
    for _ in range(32):
        if time.monotonic() - started > 780:
            break
        result = call('text')
        print(json.dumps({'phase': 'original', 'status': result['status'],
            'processed': result.get('processed', 0), 'error_code': result.get('error_code')}), flush=True)
        if not result.get('processed'):
            break
    for _ in range(32):
        if time.monotonic() - started > 1000:
            break
        job = call('claim').get('job')
        if not job:
            break
        try:
            raw = call('asset', job, asset=True)
            with tempfile.TemporaryDirectory(prefix='zhiyu-ocr-') as directory:
                body = ocr(raw, directory)
        except Exception as error:
            # Never log HTTP headers, source content or credentials.
            code = str(error) if isinstance(error, ValueError) else 'ocr_execution_failed'
            body = {'error_code': code[:80]}
        result = call('result', {**job, **body})
        print(json.dumps({'phase': 'asset', 'status': result['status'],
            'changed': result.get('changed', 0)}), flush=True)
    print(json.dumps({'reading': call('status')}), flush=True)
