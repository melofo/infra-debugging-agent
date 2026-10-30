---
id: INC-2031
type: incident
title: Orders API 500 errors after a connection pool size change
services: [orders-api]
tags: [database, connection-pool, hikari, 500, config-change, rollback]
---
# Orders API 500 errors after a connection pool size change

## Summary

For 47 minutes, about one in four requests to the orders API failed with HTTP 500. Customers saw intermittent failures when placing orders; retrying sometimes worked.

## Timeline

A release at 09:12 included a cost optimisation that reduced the database connection pool from 40 to 8 connections. The first errors appeared at 09:20 as morning traffic increased. The on call engineer first suspected the payment provider because of a few slow gateway warnings, which turned out to be unrelated.

## Root cause

The new pool size was far below the peak concurrency of the service. Under load all connections were in use and further requests waited until the 30 second connection timeout and then failed with "Connection is not available, request timed out".

## Resolution

The configuration change was reverted at 09:58 and the pool size returned to 40. Error rates dropped to zero within three minutes.

## Lessons

Resource limits such as pool sizes must be load tested before release. Add an alert on the number of threads waiting for a connection. A slow payment gateway was a distraction: it slows requests but does not exhaust the pool unless connections are held during the call.
