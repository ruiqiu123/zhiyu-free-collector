# Zhiyu free collector

A small scheduler client for the owner's deployed information hub. This repository contains only the scheduler script and workflow. No website source, source lists, articles, personal data, credentials, database backups or model calls are included.

## Setup

Store the dedicated free-collection credential as the Actions repository secret `ZHIYU_COLLECT_TOKEN`. It is not a website administrator or paid-provider key. Never commit it or print it in workflow logs.

The workflow checks every 15 minutes at minutes 7, 22, 37 and 52 (UTC; the minute offsets are the same in Asia/Shanghai). Actual source collection follows the schedule saved in the website. If local synchronization succeeded within five minutes, the cloud collector skips source requests. Otherwise it resumes cloud collection in persistent batches, at most three sources per request. Logs contain counts/status only. It never recharges or calls a paid collection channel. Page refresh is independent.

The job has a 25-minute limit. Completed batches remain saved after an interruption; retries/deferred sources continue on a later run. A successful job may mean `local_active`, `disabled` or no currently due work, so check the website's collection status as well.

Use Run workflow for an initial test. Standard public-repository GitHub-hosted runners are currently free. Schedule events can be delayed/dropped under load, and public scheduled workflows are disabled after 60 days with no repository activity. This is not an exact-time or permanent-uptime guarantee. No automatic commits are used to manufacture repository activity. Check the workflow periodically and re-enable it if needed.

https://docs.github.com/en/billing/concepts/product-billing/github-actions
https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
