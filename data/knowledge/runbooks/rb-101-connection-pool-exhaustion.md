---
id: RB-101
type: runbook
title: Database connection pool exhaustion
services: [checkout-api, orders-api, payments-service]
tags: [database, connection-pool, hikari, timeout, 500]
---
# Database connection pool exhaustion

## Symptoms

Requests fail with errors such as "Connection is not available, request timed out after 30000ms" from HikariPool, or SQLTransientConnectionException. Failures are intermittent and get worse under load. The pool metrics show active connections equal to the maximum and the number of waiting threads growing.

## Diagnosis

1. Compare the configured maximum pool size with the expected concurrency. A pool of 5 connections cannot serve hundreds of concurrent requests.
2. Look for recent configuration changes to pool settings, especially a reduced maximumPoolSize or a shorter connection timeout.
3. Look for slow queries or long transactions that hold connections open. A slow downstream call made while holding a connection also exhausts the pool.
4. Check that connections are always returned: leaks show up as active connections that never go down even when traffic is low.

## Mitigation

If a recent change reduced the pool size, restore the previous value through a configuration rollback. This is usually the fastest fix. If the pool size is correct, fix the slow queries or the code that holds connections too long. Do not raise the pool size beyond what the database can accept: the sum of all service pools must stay below the database connection limit.

## Verification

After the change, error rates should drop within a few minutes and waiting threads should return to zero.
