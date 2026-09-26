# Catalog publication evidence and recovery

Publication is a durable request, not a claim that a product is approved. `sync._post_items_batch` now queues an exact canonical request in **Meta Catalog Publication**. A worker validates the current binding and source, commits **Submitting**, then posts those exact bytes. The row records catalog ID, explicit account identity (or explicitly configured Settings token source), API version, request ID/hash, source SKUs, returned batch handles and bounded result history. Credentials are never part of the ledger.

- **Queued**: durable local request awaiting its worker or an earlier overlapping batch.
- **Pending**: Meta returned batch handles; follow-up is required.
- **Processed**: the tracked batch reported finished without item errors. This does not mean commerce approval or current-source parity.
- **Rejected**: HTTP rejection or immediate/delayed item errors; error counts and invalid IDs remain in the evidence.
- **Unknown**: timeout, interrupted submission, untrackable response or unavailable/bounded follow-up. The request is never automatically reposted.
- **Blocked**: the binding/source changed or an earlier uncertain publication prevents safe automatic continuation.

The five-minute sweep recovers queued/interrupted work and polls known pending handles, with bounded batches, response sizes, polls and timeouts. Overlapping retailer IDs wait for earlier pending work. A prior Unknown blocks later automatic refreshes, including nightly requests with fresh IDs.

A System Manager can use **Check provider batch status** to enqueue a read-only follow-up for known handles. **Reconcile current item values** requires a review note and idempotent action ID. It authorizes a new current-source request, tied to the old Unknown through an append-only **Meta Catalog Publication Resolution** (actor, time, note, exact scope and both requests). The old outcome stays Unknown. Binding mismatch blocks reconciliation; a failed reconciliation does not clear the fence. No caller can supply arbitrary wire payloads.

## Diagnostics and removal

Each **Meta Catalog Diagnostic Run** records scope, time, completeness, limits and drift. All observed items, including approved ones, append to the existing **Meta Catalog Diagnostic** table. Legacy unscoped rows are retained as history and never used to infer approval for the current binding. Missing items are **Missing** only in a complete scan; absent/incomplete/failed scans return **Unknown**. Review and WhatsApp capability fields remain separate.

Pagination always requests the fixed HTTPS Graph products edge using an opaque `after` cursor. Provider `paging.next` URLs are never followed. Redirects, repeated/missing cursors, malformed identity rows, oversized responses and the 60-page/12,000-item cap prevent completeness claims. Observations compare identity, price, availability and visibility; matching product counts are not parity proof. A scan is an observation over time, not an atomic provider snapshot.

Item unpublication, disabling, removal and lost eligibility queue DELETE intent instead of silently doing nothing. Full reconciliation removes only retailer IDs previously managed by the exact ledger binding; unrelated remote products appear as drift, never automatic deletion. Pre-ledger remote items or a changed account/API revision require explicit review rather than guessed historical ownership. Item Price, Bin update/change, Stock Ledger Entry insert and Sales Order reservation events enqueue canonical refresh after commit; template changes refresh variants and component stock changes refresh bundles.

## Safe generic prices

`catalog_pricing.quote` reads the configured selling price list, exactly one current generic Item Price in its currency and stock UOM, and catalog markup. Ambiguous prices and active automatic Pricing Rules produce explicit blockers. It does not elevate the actor, create a throwaway Sales Order, commit, or roll back. Customer/quantity rules require the separate native order review, which must show its actual ERP price source and totals. Publisher/picker prices no longer claim an unsafe automatic storefront discount. Locking reads are propagated into source metadata, prices, stock, barcodes and image privacy.

## Primary references and verification limits

Meta's [current generated ProductCatalog SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/productcatalog.py) defines the GET `check_batch_request_status` edge, `handle` and `load_ids_of_invalid_requests`. Its [CheckBatchRequestStatus type](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/checkbatchrequeststatus.py) exposes handle, raw status, errors/count, invalid request IDs and warnings. The SDK does not enumerate status strings. This implementation recognizes only `finished` as processing completion; other strings stay pending and eventually Unknown. Exact live status spelling and response shape remain a required owned-catalog acceptance check. The [developer reference](https://developers.facebook.com/docs/marketing-api/reference/product-catalog/check_batch_request_status/) returned HTTP429 during research on 2026-09-26; no restriction was bypassed.

ERPNext's [version-16 Bin implementation](https://github.com/frappe/erpnext/blob/version-16/erpnext/stock/doctype/bin/bin.py) uses direct database writes for some quantity updates, which is why document stock/reservation hooks supplement Bin events.

Pure evidence tests and native SQL tests use explicit provider doubles. They do not prove live provider ingestion, production migration, cross-process crash recovery, or provider account capability. Those acceptance gates remain separate.
