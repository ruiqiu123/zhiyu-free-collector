const origin=process.env.ZHIYU_ORIGIN||'https://zhiyu.ruiqiuguge.chatgpt.site';
const token=process.env.ZHIYU_COLLECT_TOKEN;
if(new URL(origin).origin!=='https://zhiyu.ruiqiuguge.chatgpt.site'||!token)throw Error('Missing authorized collector configuration');
let processed=0;
for(let n=0;n<60;n++){
 const r=await fetch(origin+'/api/service/cloud-free',{method:'POST',headers:{authorization:'Bearer '+token,'content-type':'application/json'},body:JSON.stringify({max_sources:3}),redirect:'error',signal:AbortSignal.timeout(110000)});
 if(!r.ok)throw Error('Cloud collector HTTP '+r.status);
 const result=await r.json();processed+=result.processed||0;
 // Log counts and status only; never credentials, source configuration or articles.
 console.log(JSON.stringify({batch:n+1,status:result.status,processed:result.processed||0,ready:result.ready||0,pending:result.pending||0,paid_requests:result.paid_requests||0}));
 if(result.paid_requests)throw Error('Unexpected paid request');
 if(result.status==='local_active'||!result.ready){console.log(JSON.stringify({finished:true,processed,deferred:result.pending||0}));break;}
}
