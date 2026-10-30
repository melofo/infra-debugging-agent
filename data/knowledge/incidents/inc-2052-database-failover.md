---
id: INC-2052
type: incident
title: Connection resets during a database failover
services: [checkout-api, orders-api]
tags: [database, failover, connection-reset, 503]
---
# Connection resets during a database failover

## Summary

During a planned maintenance window the primary database failed over to the replica. Services returned HTTP 503 for about two minutes and then recovered.

## Root cause

Existing connections in the pools pointed to the old primary. Until the pools detected the broken connections and reconnected, every request that received a stale connection failed with a connection reset error.

## Resolution

The services recovered on their own after the pool validated and replaced the stale connections. We enabled connection validation on borrow and shortened the maximum lifetime of pooled connections.

## Lessons

Failovers cause a short burst of errors that stops by itself. If errors continue for more than a few minutes after a failover, look for a pool that does not replace broken connections.
