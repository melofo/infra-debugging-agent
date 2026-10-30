---
id: RB-007
type: runbook
title: General HTTP 5xx investigation checklist
services: []
tags: [http, 5xx, triage, general]
---
# General HTTP 5xx investigation checklist

Use this checklist when a service returns HTTP 500, 502, 503 or 504 and the cause is not yet known.

## Triage

1. Find the first error in the logs and note the exact time it started. Errors that start suddenly usually point to a change; errors that grow slowly usually point to saturation or a leak.
2. Check whether the errors are constant or intermittent. Intermittent errors under load often mean a shared resource is exhausted (connections, threads, memory).
3. Check which endpoints are affected. A single endpoint suggests a code path or a dependency used only by it; every endpoint suggests a platform problem.

## Look for recent changes

Check deployments, configuration changes and feature flag toggles of the affected service close to the time the errors started. Compare the error rate before and after the change.

## Check dependencies

Check the database, the cache, message brokers and external APIs the service calls. Look at their latency and error rates for the same time window.

## Escalate

If no change or dependency explains the errors within 30 minutes, escalate to the owning team and attach the first error time, the affected endpoints and the checks already done.
