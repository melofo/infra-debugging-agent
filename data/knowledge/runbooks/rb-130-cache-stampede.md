---
id: RB-130
type: runbook
title: Cache stampede and cache outage
services: [checkout-api, search-service]
tags: [cache, redis, stampede, eviction, latency]
---
# Cache stampede and cache outage

## Symptoms

A drop in the cache hit ratio is followed by a spike in database load and slow responses. The cache miss ratio jumps, for example from five percent to forty percent, often right after a cache restart, a mass expiry or a deploy that changed cache keys.

## Diagnosis

1. Check the cache hit ratio and eviction counters. A sudden rise in evictions means the cache is too small or the keys are too large.
2. Check whether many keys were given the same expiry time and expired together.
3. Check whether a deploy changed the key format, which makes every old entry unreachable.
4. Look at the database load: a stampede shows as many identical queries issued at the same moment.

## Mitigation

Warm the cache for the hottest keys. Add random jitter to expiry times. Use request coalescing so that only one request rebuilds a missing entry. A cache miss alone normally makes responses slower but does not by itself cause HTTP 500 errors; check the database if errors appear.
