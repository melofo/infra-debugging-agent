---
id: RB-110
type: runbook
title: Slow database queries and lock contention
services: [checkout-api, orders-api, search-service]
tags: [database, slow-query, lock, latency, postgres]
---
# Slow database queries and lock contention

## Symptoms

Response times rise for endpoints that read or write the same tables. The database shows long running queries, queries waiting on locks, or a sudden increase in CPU. Timeouts appear in the application, but connections are available.

## Diagnosis

1. List the longest running queries and the queries that are waiting for a lock. Identify the blocking session.
2. Check for a missing index: a query that scans a whole table after a data growth or a schema change is a common cause.
3. Check for long transactions, for example a batch job that updates many rows inside a single transaction.
4. Compare query plans before and after a recent deploy that changed queries or the ORM mapping.

## Mitigation

Cancel the blocking query if it is safe to do so. Add the missing index using a concurrent build to avoid blocking writes. Move heavy batch work out of peak hours and process it in small batches.

## Notes

Slow queries and pool exhaustion often appear together, because slow queries keep connections busy for longer. See the connection pool runbook if the symptom is a connection timeout rather than a slow query.
