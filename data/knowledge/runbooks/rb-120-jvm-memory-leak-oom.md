---
id: RB-120
type: runbook
title: JVM memory leak and OOM kills
services: [search-service, orders-api]
tags: [jvm, memory, oom, leak, restart]
---
# JVM memory leak and OOM kills

## Symptoms

Pods restart repeatedly with the reason OOMKilled, or the service becomes slow with long garbage collection pauses before it dies. Memory usage grows steadily between restarts instead of staying flat.

## Diagnosis

1. Plot heap usage over several hours. A saw-tooth pattern that returns to the same baseline is healthy; a rising baseline is a leak.
2. Check garbage collection logs for frequent full collections that free very little memory.
3. Take a heap dump before the process is killed and look for the object types that dominate retained memory: unbounded caches, listeners that are never removed and thread locals are common.
4. Check whether the container memory limit is smaller than the configured maximum heap plus native memory.

## Mitigation

Restart the pods to restore service while the leak is investigated. Roll back a recent library upgrade or deploy if the growth started right after it. Add a bound or an expiry to any cache that grows without limit.
