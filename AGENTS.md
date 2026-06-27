# doco_meta_catalog — ERPNext Item → Meta Commerce Catalog bridge

> ⚠ **Multi-agent stomping safeguard**: before any edit/restart, read [muelle/AGENTS.md → Coordination](../muelle/AGENTS.md#coordination--multi-agent-freshness-read-before-any-write). Use `bash ../muelle/scripts/muelle-restart.sh <svc> --reason "..."` not raw `docker compose restart`. Memory entries: `feedback_agent_freshness_protocol`, `feedback_restart_coordination`.

New Frappe app (v0.2.0). Bridges ERPNext `Item` records into a Meta Commerce Catalog so the same product list serves WhatsApp Business Catalog messages, Facebook Shop, and Instagram Shopping — without running a parallel WooCommerce stack.

Status: **INSTALLED on prod `ventas.docomexico.com` (2026-06-26), master gate `enabled=0` → zero Meta calls until configured.** Also installed on lab. NOT yet wired to a real Meta Catalog (no `catalog_id`/token). Sync logic now mirrors the live storefront (see "Storefront parity" below).

> **Storefront parity (commit `d8fa363`, 2026-06-26).** The earlier scaffold diverged from the live shop on four axes; `sync.py` now sources from `doco.docoutils.storefront` (the single source of truth) so the Meta catalog == the web shop by construction: publish gate = `Item.publish_on_web`; price = `Item Price.price_list_rate`; availability = live Bin `actual_qty - reserved_qty`; image = public `/files`/https only (`_image_url` guard, rejects `/private` + signed B2/S3). Variants pushed as individual `retailer_id`s grouped via `item_group_id`. `doco` is now a required app. Prod dry_run: 2,302 eligible / 1,992 skipped (unpriced or no photo — same items the storefront also won't sell).

See [`muelle-host/AGENTS.md`](../AGENTS.md) for the broader stack map.

## Why this app exists

Research in session 2026-05-24:
- No mature open-source ERPNext↔Meta-Catalog bridge in the wild
- Building one directly = ~300 LOC + ~2 days
- WooCommerce-as-middleman = ~600-2000 MB extra stack + WP security patching + sync drift (`woocommerce_fusion`)
- Twilio WhatsApp Channels = paid markup over the same Meta API the existing `frappe_whatsapp` already speaks

Conclusion: build small, direct, server-side. Same Meta Catalog drives WhatsApp + FB + IG (three surfaces, one sync job).

## What this app owns

- `Meta Catalog Settings` (Single doctype) — catalog_id, token (reuse from WhatsApp Account OR set directly), default brand/condition/currency, image base + fallback, price markup % (applied to the REAL selling price; default 0 = parity), master `enabled` gate, `category_map` child table (per-item-group condition + google_product_category). NOTE: `sync_only_website_items` + `default_availability` are now deprecated/no-op (gate is always `publish_on_web`; availability is computed live)
- `Meta Catalog Category Map` (child of Settings) — `item_group → condition / google_product_category` override (e.g. `Seminuevos → refurbished`), by DATA, not hardcoded to any vertical
- `Item.on_update` hook → enqueue background push to Meta `items_batch` endpoint
- `Item.on_trash` hook → enqueue DELETE
- Daily `full_reconcile` cron — safety net for missed webhooks
- WA messaging helpers: `send_catalog_message`, `send_product_message`, `send_product_list` (`interactive.type=product_list`)
- Inbound `order` webhook handler → creates draft Sales Order from cart payload (called from `frappe_whatsapp` webhook router, NOT directly subscribed to Meta)

## Repo layout

```
doco_meta_catalog/
  doco_meta_catalog/
    __init__.py
    hooks.py                              ← Item doc_events + scheduler_events
    modules.txt
    sync.py                               ← queue_item_sync, push_one, delete_one, full_reconcile, sync_all_now
    wa_helpers.py                         ← send_catalog_message, send_product_message, send_product_list, handle_order_message
    doco_meta_catalog/
      __init__.py
      doctype/
        meta_catalog_settings/
          meta_catalog_settings.json
          meta_catalog_settings.py        ← get_token(), get_graph_root()
```

## Key contracts

### Item → Meta payload mapping (`sync._build_payloads`, mirrors storefront)
```python
{
  "method": "UPDATE",
  "data": {
    "id": item.name,                       # retailer_id = ERPNext item_code
    "title": item.item_name[:200],
    "description": strip_html_tags(item.description)[:9999],
    "availability": "in stock|out of stock",  # from storefront _stock_levels (live Bin)
    "condition": <category_map override> or default_condition or "new",
    "price": int(price_list_rate * (1+markup/100) * 100),  # Item Price, minor units (MXN cents)
    "currency": "MXN",
    "link": f"{base}/shop/{item.name}",    # Meta field is `link` (NOT `url`)
    "image_link": <public HTTPS>,          # storefront _image_url guard; else fallback_image_url
    "brand": item.brand or default_brand,
    "item_group_id": item.variant_of,      # only for variant leaves — groups variants
    "google_product_category": <category_map override>,  # only if mapped
  }
}
```
POSTed to `/{catalog_id}/items_batch` with `item_type=PRODUCT_ITEM`. An item with no
Item Price OR no public image is SKIPPED (never sent) — same as the storefront. Use the
whitelisted `dry_run()` to inspect the full payload + skip reasons WITHOUT posting.

### Image URL gotcha (MUST resolve to public HTTPS)
- Meta scrapes `image_link` from outside the network. If the URL is auth-gated or non-public, sync silently fails.
- Defaults to `frappe.utils.get_url() + image_path` if `image_url_base` not set.
- Override via Settings to use a CDN.

### Sync gate
- `Meta Catalog Settings.enabled` = master switch. When OFF, every `Item.on_update` hook + the daily reconcile short-circuit in `_get_settings()` → zero side effects, safe install state (current prod state).
- Eligible set = published, sellable LEAF items: `Item.publish_on_web == 1` AND not disabled AND `has_variants == 0` (mirrors the storefront `catalog()` gate). This is the canonical doco web-publish flag — NOT the legacy `show_in_website` / `Website Item` row the scaffold used. Templates are excluded; their priced variants are pushed and grouped via `item_group_id`. Keeps internal SKUs (parts inventory, repair stock) out automatically — they are not `publish_on_web`.

### Token strategy
- Reuse existing `WhatsApp Account.token` if `whatsapp_account` field set on Meta Catalog Settings → no token duplication, single rotation point
- Override via `access_token` Password field directly if needed
- System User token MUST have `catalog_management` scope (in addition to `whatsapp_business_messaging` if you also send product messages from this app)

### Inbound cart → Sales Order
- Customer taps a product in WhatsApp Catalog → sends shopping cart message (Meta `message_type=order`)
- Webhook routes to `wa_helpers.handle_order_message(message, account_name)`
- Resolves customer via CRM `get_contact_by_phone_number`, creates draft Sales Order, attaches buyer note
- Checkout happens OFF-WhatsApp (MX market — Meta-hosted checkout is US/IN/BR only); send payment link as next message (Mercado Pago Checkout Pro or similar)

## Setup (when ready to wire prod)

1. **Meta Commerce Manager**: create Catalog of type "E-commerce", connect to existing WABA `2954965221375122`
2. **System User**: add `catalog_management` scope to existing token via Business Settings → System Users → Add Asset → Catalogs
3. **Install app on lab first**: `bench get-app https://github.com/HolyMC2/doco_meta_catalog && bench --site ventas.lab... install-app doco_meta_catalog`
4. **Configure Meta Catalog Settings** in Desk:
   - `catalog_id` = numeric ID from Commerce Manager
   - `whatsapp_account` = `Doco Ventas` (reuse token)
   - `image_url_base` = `https://ventas.docomexico.com`
   - `fallback_image_url` = public placeholder PNG
5. **Run first sync manually**: bench console → `frappe.get_attr("doco_meta_catalog.sync.sync_all_now")()`
6. **Verify**: Commerce Manager → Catalog → Items page should populate
7. **Send a test product message**: bench console → `frappe.get_attr("doco_meta_catalog.wa_helpers.send_product_message")(to="+526691530561", retailer_id="<item_code>")`

## Companion repos

- [`whatsapp_chat-fork/`](../whatsapp_chat-fork/AGENTS.md) — the Desk chat bubble that will display incoming order messages (once we route them through)
- Upstream `frappe_whatsapp` — Meta API client + webhook router; this app extends its webhook handler via `handle_order_message`
- [`crm/`](../crm/AGENTS.md) — `get_contact_by_phone_number` used to resolve buyer to existing Lead/Deal/Contact
- ERPNext core — `Item` doctype + `Website Item` + `Sales Order`

## Memory (read-only)

- `~/.claude/projects/-home-holymc2/memory/project_doco_whatsapp.md` — Meta WABA + token setup, shared with this app
- `~/.claude/projects/-home-holymc2/memory/project_fcrm_twilio_sip.md` — companion telephony stack

## Deploy

Standard Frappe app install flow. No frontend bundle to build.

```bash
# Lab (after first commit to fork)
cd ~/muelle-host/muelle
docker compose exec -T backend bash -c \
  "cd /home/frappe/frappe-bench && bench --site ventas.lab.xoloitzcuintles.com install-app doco_meta_catalog"

# Prod (after lab verify)
ssh contavm 'cd ~/muelle && docker compose exec -T backend bash -c \
  "cd /home/frappe/frappe-bench && bench get-app --resolve-deps doco_meta_catalog https://github.com/HolyMC2/doco_meta_catalog && \
   bench --site ventas.docomexico.com install-app doco_meta_catalog"'
```

## Outstanding work

- [x] Storefront-parity refactor of `sync.py` (gate/price/stock/image) + variant grouping + category map — commit `d8fa363`, 11/11 unit tests, dry-tested on lab + prod
- [x] Installed on prod `ventas.docomexico.com` (2026-06-26), `enabled=0`
- [ ] **Marco**: create Meta Commerce Catalog (E-commerce) → set `catalog_id` + token-with-`catalog_management`-scope (reuse WhatsApp Account or direct `access_token`)
- [ ] First test: controlled 2-item push via `sync._build_payloads([..], s)` + `_post_items_batch` (ignores `enabled` gate — pushes without arming hooks); verify in Commerce Manager
- [ ] Go-live: flip `enabled=1`, run `sync_all_now`; restart `backend queue-short queue-long scheduler` if on-edit pushes don't fire
- [ ] Optional: set `fallback_image_url` to also publish priced-but-photoless items (≈519 lab / part of 1,992 prod skipped)
- [ ] Populate `category_map` (e.g. `Seminuevos → refurbished`) once catalog is live
- [ ] Test product_list interactive message + verify cart payload arrives in webhook
- [ ] Wire `handle_order_message` into `frappe_whatsapp` webhook router (it doesn't auto-listen yet — needs a hook)
- [ ] `image_url_base` left empty on prod → resolves to `https://ventas.docomexico.com` via `get_url()` (verified). Set only if images move to a CDN.

## Conventions

- New app — no upstream constraint. Free to refactor.
- All Meta API calls go through `Meta Catalog Settings.get_token()` + `get_graph_root()` for graph_api_version centralization.
- Push errors → `frappe.log_error` with Meta response body (first 2000 chars). Don't fail the Item save.
- Background queue: `short` for single-Item pushes, `long` for `full_reconcile`.

---

*Living doc. Update when you wire a phase + verify on real Meta Catalog.*
