---
id: RB-210
type: runbook
title: Payment gateway latency
services: [checkout-api, payments-service]
tags: [payment, gateway, latency, external, timeout]
---
# Payment gateway latency

## Symptoms

Log lines such as "Slow response from payment gateway" with latency above 1500 ms. Checkouts become slow, but the service usually keeps returning successful responses.

## Diagnosis

1. Check the status page of the payment provider and recent incident announcements.
2. Compare latency per provider region and per payment method to see whether the slowdown is limited to one of them.
3. Check the retry and timeout configuration: aggressive retries on a slow gateway multiply the load and make things worse.

## Mitigation

Slow gateway responses alone normally cause slow checkouts but not HTTP 500 errors. Reduce the timeout so slow calls fail fast, add a circuit breaker, and make sure no database connection is held open while waiting for the gateway.
