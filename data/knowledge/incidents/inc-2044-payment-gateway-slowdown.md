---
id: INC-2044
type: incident
title: Slow checkouts caused by payment provider degradation
services: [checkout-api, payments-service]
tags: [payment, gateway, latency, external-dependency]
---
# Slow checkouts caused by payment provider degradation

## Summary

Checkout latency rose from two seconds to more than ten seconds for about 35 minutes. No significant rise in HTTP 500 errors was seen.

## Root cause

The external payment provider had a regional degradation. Every charge call took several seconds. The service waited correctly and returned successful responses, so conversions fell but error rates stayed flat.

## Resolution

The provider recovered by itself. As a follow up the timeout for charge calls was reduced to five seconds, a circuit breaker was added and retries were limited to one.

## Lessons

Slow responses from an external dependency and HTTP 500 errors are different symptoms and usually have different causes. Check the provider status page first when only latency is affected.
