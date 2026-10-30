---
id: RB-140
type: runbook
title: Rolling back a bad deployment
services: []
tags: [deploy, rollback, release, configuration]
---
# Rolling back a bad deployment

## When to roll back

Roll back when errors or latency started within minutes of a deployment and the change is plausibly related. Rolling back is cheaper than debugging in production: restore service first, investigate afterwards.

## Procedure

1. Identify the last known good version from the release history.
2. For configuration only changes, revert the configuration commit and redeploy the same code version.
3. For code changes, redeploy the previous version. Check that database migrations of the bad release are backward compatible before rolling back.
4. Watch the error rate and latency dashboards for ten minutes after the rollback.

## After the rollback

Record the version, the time and the reason. Open a ticket for the fix and add a test or an alert that would have caught the problem. A change that tuned a resource setting, such as a pool size, timeout or memory limit, should be load tested before it is released again.
