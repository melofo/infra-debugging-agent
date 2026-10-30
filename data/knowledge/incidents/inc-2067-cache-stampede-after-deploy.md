---
id: INC-2067
type: incident
title: Database overload after a cache key format change
services: [checkout-api, search-service]
tags: [cache, stampede, database, deploy, latency]
---
# Database overload after a cache key format change

## Summary

After a release, the cache miss ratio rose from five percent to over forty percent and the database CPU reached saturation. Response times tripled and a fraction of requests timed out.

## Root cause

The release changed the format of the cache keys. All previously cached entries became unreachable at the same moment, so every request fell through to the database until the cache warmed up again.

## Resolution

The cache was warmed for the hottest keys and the release was rolled out gradually. Key format changes now use a version prefix so old and new keys can coexist.

## Lessons

A sudden drop in the cache hit ratio right after a deploy suggests a key change or a mass expiry. The database is the real bottleneck during a stampede; check its load before blaming the application.
