---
id: INC-2060
type: incident
title: Search service restarts caused by a memory leak after a library upgrade
services: [search-service]
tags: [jvm, memory, oom, leak, library-upgrade]
---
# Search service restarts caused by a memory leak after a library upgrade

## Summary

The search service pods restarted every few hours with OOMKilled. During the minutes before each restart, responses were slow and some requests failed with HTTP 503.

## Root cause

A dependency upgrade introduced a cache without a size limit inside a client library. The cache grew with every distinct query until the heap was full.

## Resolution

The library was rolled back to the previous version and a size bound was configured before upgrading again. A heap dump showed the cache entries as the largest retained objects.

## Lessons

Steady memory growth between restarts points to a leak, and a heap dump finds it quickly. Roll back first, then investigate.
