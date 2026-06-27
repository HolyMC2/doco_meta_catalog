# doco_meta_catalog — Integration Spec & Plan

Status: outbound sync LIVE + verified on prod `ventas.docomexico.com` (2026-06-26, 2-item
test green, MX$ pricing correct). This doc is the contract for what exists, what goes where,
how it grows, and the security model. Living doc — update on each phase.

---

## 1. Purpose

One ERPNext-driven **Meta Commerce Catalog** that fans out to WhatsApp catalog messages,
Facebook Shop, Instagram Shopping, and dynamic ads. **ERPNext is the source of truth; Meta is
a broadcast surface.** No parallel WooCommerce. The catalog == the live web storefront by
construction (same publish gate, price, stock, image logic).

Note: there is **no public API for peer-to-peer Facebook *Marketplace* listings** for generic
retail. The attainable surface is the Commerce Catalog (one `catalog_id`); the "Facebook Shop"
storefront is eligibility-gated (MX often excluded) and **not required** — the catalog already
drives WhatsApp + ads.

---

## 2. Components — what goes where

| Layer | Owns | Where |
|---|---|---|
| ERPNext core | `Item`, `Item Price`, `Bin`, `Sales Order`, `Customer` | core (tenant cell) |
| **doco.docoutils.storefront** | canonical sellable/price/stock/image logic (single source of truth) | `doco` app |
| **doco_meta_catalog** | the bridge: settings, outbound sync, messaging, inbound order | this app |
| frappe_whatsapp | Meta Graph client + webhook receiver; persists inbound as `WhatsApp Message` | image-baked (do NOT patch in-container) |
| Meta Commerce Platform | Catalog (`catalog_id`) → FB Shop / IG Shopping / WhatsApp / ads | external |

**Rule:** the connector NEVER re-derives price/stock/publish/image. It calls the storefront
helpers (`_selling_price_list`, `_prices`, `_stock_levels`, `_image_url`). One source of truth.

---

## 3. Data flows

### A. Outbound product sync — `sync.py` — **LIVE (gated by `enabled`)**
```
Item.on_update ─▶ queue_item_sync (gate: enabled + publish_on_web + leaf) ─▶ enqueue 'short'
              ─▶ push_one ─▶ _build_payloads (storefront price/stock/image) ─▶ POST {catalog_id}/items_batch UPDATE
Item.on_trash  ─▶ queue_item_delete ─▶ delete_one ─▶ items_batch DELETE
daily cron     ─▶ full_reconcile ─▶ all eligible, chunks of 1000 (safety net)
dry_run()      ─▶ build full payload, NO POST (parity + skip inspection)
```
Payload mapping (`_build_payloads`): `id`=item_code, `title`, `description`(html-stripped),
`availability`(live Bin), `condition`(category_map|default), **`price`="<amount> <CUR>" string**
(currency pinned per item — Meta rejects a separate `currency` field and falls back to the
catalog default, which is USD → mispricing), `link`, `image_link`(public-guarded), `brand`,
`item_group_id`(variants), `google_product_category`(if mapped). Unpriced / no-public-image →
skipped (same as the storefront would refuse to sell).

### B. Outbound catalog messages — `wa_helpers.py`
`send_catalog_message` / `send_product_message` / `send_product_list` → WhatsApp Cloud API
interactive messages referencing `catalog_id` + `product_retailer_id`. **Role-gated** (§5).

### C. Inbound cart → Sales Order — `wa_helpers.handle_order_message` — **NOT wired (security-gated, §5)**
```
buyer taps catalog product ─▶ WA cart message ─▶ Meta webhook
  ─▶ frappe_whatsapp persists WhatsApp Message (content_type='order', product_catalog_json)
  ─▶ [NEW] connector HMAC-verified endpoint  (X-Hub-Signature-256, fail-closed)
  ─▶ handle_order_message: re-price from Item Price · sellable gate · caps · idempotency
  ─▶ DRAFT Sales Order ─▶ human reviews + sends payment link (MX off-Meta) ─▶ submit
```
MX has no native Meta checkout → the sale completes off-Meta (Mercado Pago Checkout Pro link).

---

## 4. Data model (doctypes)

- **Meta Catalog Settings** (Single) — `enabled` (master gate), `catalog_id`,
  `graph_api_version`, `whatsapp_account` | `access_token`, **`app_secret` (NEW, Password — for
  inbound HMAC)**, default brand/condition/currency, `image_url_base` + `fallback_image_url`,
  `price_markup_percent` (0 = parity), `category_map`. (`sync_only_website_items`,
  `default_availability` = deprecated/no-op.)
- **Meta Catalog Category Map** (child) — `item_group → condition / google_product_category`.

Token: `get_token()` reuses a `WhatsApp Account` token if set, else `access_token`. Token needs
`catalog_management` scope AND the catalog assigned to its System User at **full control**.

---

## 5. Security model (the trust boundary)

**Cryptographically verify (machine-gated, fail-closed — proves authenticity):** every inbound
WhatsApp webhook POST via `X-Hub-Signature-256 = HMAC-SHA256(app_secret, raw_body)`, compared
with `hmac.compare_digest`, BEFORE any parse / DB write / CRM match / SO creation. Missing or
mismatched ⇒ 403, store nothing.

**Human-gate (authorization + submit — limits blast radius, never relied on for authenticity):**
outbound sends are role-gated to real operators; the inbound order lands as a DRAFT (`docstatus=0`)
that a human must submit before any GL/stock impact.

### Verified findings (adversarial review 2026-06-26) + status

| Sev | Finding | Location | Fix | Status |
|---|---|---|---|---|
| HIGH | Outbound WA senders are bare `@frappe.whitelist()` — any Desk user sends from the business WABA (spam/phish/quota-burn) | `wa_helpers.py:55/74/93` | `frappe.only_for([...])` + E.164 `to` validation + `_rate_limit('wa_send')` | **batch 1 — this commit** |
| LOW | Buyer phone (PII) leaked into Error Log on failed sends | `wa_helpers.py:_post_message`, `sync.py:_post_items_batch` | strip `to`, log parsed Meta error code+message not raw body | **batch 1 — this commit** |
| HIGH | No `X-Hub-Signature-256` HMAC on inbound webhook — any host forges `order`/`text`/status | `frappe_whatsapp/utils/webhook.py` (image-baked) | NEW connector guest endpoint: read raw body → recompute HMAC → `compare_digest` → 403 fail-closed → delegate. Point Meta webhook at it. Reuse `frappe_whatsapp/api/flow_endpoint.py:140` pattern | **batch 2 — before inbound wiring** |
| HIGH | `handle_order_message` builds SO + Customer with `ignore_permissions` — unauth SO-creation primitive the moment it's wired | `wa_helpers.py:134` | keep un-whitelisted, reachable ONLY behind the HMAC gate; re-price from Item Price (never trust payload `item_price`); sellable gate (`publish_on_web`); caps (qty>0 ≤ `_MAX_QTY`, line count); E.164 `from`; idempotency on WA message id; stay DRAFT | **batch 2 — before inbound wiring** |

### Batch 2 status (built, lab-tested, NOT wired) + go-live gate

Batch 2 (HMAC endpoint + hardened order→SO) is implemented and passed an adversarial review — no
auth/forgery bypass; HMAC over raw bytes, fail-closed 403 before any write; order→SO is draft-only.
Applied from the review: variant-template eligibility gate (also fixes the live push), duplicate-
line merge + summed-qty cap, Infinity-safe qty, non-ASCII signature header → 403 (not 500), blank
msg-id reject, and the webhook no longer swallows a frappe_whatsapp failure into a 200 (Meta retries
instead of silent message loss). 37 unit tests.

**MUST land before repointing the Meta webhook URL** (go-live gate — inbound stays unwired until then):
- **Atomic idempotency** — replace the `db.exists(po_no)` TOCTOU with DB-enforced uniqueness (a
  dedup doctype with a UNIQUE WhatsApp message id, insert-first) so concurrent Meta retries cannot
  create duplicate draft Sales Orders.
- **Storefront Profile `hidden_items`** — thread the authoritative profile's hidden set into both the
  push gate and the order gate (add a `storefront` link to Meta Catalog Settings to pick the profile).
- **Phone canonicalization** in `_find_or_create_customer` (match crm normalization) to avoid forking
  duplicate Customers.
- **End-to-end webhook tests** — tampered-body POST → 403 + zero rows written; concurrent duplicate delivery.

Identifiers `catalog_id`/`phone_id` are Meta business IDs (not secrets). The bearer **token** is a
secret (Password field, never logged — confirmed). `app_secret` likewise.

Infra caveat: the cell proxy must set `X-Forwarded-For` from `$remote_addr` for the per-IP rate
limiter to bind; a global per-bucket cap backstops until then.

---

## 6. Roadmap — how it grows

- **P0 DONE** — storefront-parity sync, prod-installed (`enabled=0`).
- **P1 DONE** — live 2-item test, MX$ pricing verified; price-string fix.
- **P2 (now)** — full catalog go-live (`sync_all_now`, ~2,302 items) + monitor `last_full_reconcile_status`.
- **P3** — security: batch 1 (senders/logs) **done**; batch 2 (inbound HMAC + hardened order→SO).
- **P4** — WhatsApp catalog messages tested end-to-end; `sale_price` from storefront `_sale_prices`; populate `category_map` (e.g. Seminuevos→refurbished); `fallback_image_url` for photoless priced items.
- **P5** — multi-surface: FB/IG Shop when eligible, collections/sets, dynamic-ads feed (same `catalog_id`, no new sync code).
- **P6** — multi-tenant: mumulenceria own `catalog_id` + own Settings per site; optional Storefront Profile scope parity (included/excluded groups).
- **P7** — reliability: per-`handle` ingestion-error reconciliation, delta sync (only changed since last reconcile), retry/backoff, observability metrics (synced/skipped/errors), optional scheduled feed-URL fallback.

---

## 7. Testing strategy

- **Unit** — pure payload mapping with storefront helpers monkeypatched (`tests/test_sync.py`, 11)
  + send-guard tests (`tests/test_wa_helpers.py`). No DB fixtures.
- **dry_run()** — no-POST; compare eligible/skip counts against storefront parity.
- **Controlled push** — `_build_payloads([codes], s)` + `_post_items_batch` (bypasses `enabled`)
  → read back price/currency/stock from `/{catalog_id}/products` (dump `data` only — paging URLs
  echo the token).
- **Inbound (batch 2)** — signed-payload tests (valid/invalid/missing HMAC → 200/403), payload
  re-pricing, qty/line caps, idempotent replay, draft-only assertion.

---

## 8. Open decisions

1. P2 go-live timing (now vs nightly reconcile). Rotate the exposed token first.
2. Set the catalog **default currency to MXN** in Commerce Manager (belt-and-suspenders; code already pins it).
3. Operator role for senders — `System Manager` + `Sales User`, or a dedicated `WhatsApp Manager` role.
4. Inbound payment-link provider automation (Mercado Pago Checkout Pro) on order confirm.
5. Multi-tenant token strategy (per-site System User vs shared).
