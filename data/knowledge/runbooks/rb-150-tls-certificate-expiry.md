---
id: RB-150
type: runbook
title: TLS certificate expiry and handshake failures
services: []
tags: [tls, certificate, expiry, handshake, 502]
---
# TLS certificate expiry and handshake failures

## Symptoms

Clients or upstream services report SSL handshake failures, certificate expired or unknown certificate errors. Failures start at an exact moment, often at midnight, and affect every request to the endpoint rather than a fraction of them.

## Diagnosis

1. Inspect the certificate served by the failing endpoint and read its expiry date.
2. Check the whole chain: an expired intermediate certificate causes the same failure as an expired leaf.
3. Check whether the automatic renewal job ran and whether the new certificate was actually loaded by the proxy or gateway. Many gateways must be reloaded after renewal.

## Mitigation

Renew the certificate and reload the gateway. Add an alert that fires thirty days and seven days before every certificate expires.
