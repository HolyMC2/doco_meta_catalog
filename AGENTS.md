# doco_meta_catalog — ERPNext Item → Meta Commerce Catalog bridge

New Frappe app (v0.1.0). Bridges ERPNext `Item` records into a Meta Commerce Catalog so the same product list serves WhatsApp Business Catalog messages, Facebook Shop, and Instagram Shopping — without running a parallel WooCommerce stack.

Status: **scaffolded + installed on lab, not yet wired to a real Meta Catalog on prod.** See [`muelle-host/AGENTS.md`](../AGENTS.md) for the broader stack map.

## Why this app exists

Research in session 2026-05-24:
- No mature open-source ERPNext↔Meta-Catalog bridge in the wild
- Building one directly = ~300 LOC + ~2 days
- WooCommerce-as-middleman = ~600-2000 MB extra stack + WP security patching + sync drift (`woocommerce_fusion`)
- Twilio WhatsApp Channels = paid markup over the same Meta API the existing `frappe_whatsapp` already speaks

Conclusion: build small, direct, server-side. Same Meta Catalog drives WhatsApp + FB + IG (three surfaces, one sync job).

## What this app owns

- `Meta Catalog Settings` (Single doctype) — catalog_id, token (reuse from WhatsApp Account OR set directly), default brand/condition/availability/currency, image base, price markup %, sync gate
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

### Item → Meta payload mapping
```python
{
  "method": "UPDATE",
  "data": {
    "id": item.name,                      # retailer_id = ERPNext item_code
    "title": item.item_name,
    "description": item.description[:9999],
    "availability": "in stock",           # configurable default
    "condition": "new",                   # configurable default
    "price": int(rate * (1+markup/100) * 100),  # minor units (MXN cents)
    "currency": "MXN",
    "image_link": <public HTTPS>,         # from Item.image OR Website Item.website_image
    "brand": item.brand or default_brand,
    "url": f"{base}/shop/{item.name}",
  }
}
```

### Image URL gotcha (MUST resolve to public HTTPS)
- Meta scrapes `image_link` from outside the network. If the URL is auth-gated or non-public, sync silently fails.
- Defaults to `frappe.utils.get_url() + image_path` if `image_url_base` not set.
- Override via Settings to use a CDN.

### Sync gate
- `Meta Catalog Settings.enabled` = master switch. When OFF, every `Item.on_update` hook short-circuits in `_should_sync` → zero side effects, safe install state.
- `sync_only_website_items` (default ON) — limits push to Items with `show_in_website` OR a `Website Item` row. Keeps internal SKUs (parts inventory, repair stock) out of the customer-facing catalog.

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

- [ ] Wire prod Meta Commerce Catalog (waiting on catalog_id + token-with-catalog_management-scope)
- [ ] Configure `Meta Catalog Settings.image_url_base` so Meta can scrape Item images
- [ ] Test product_list interactive message + verify cart payload arrives in webhook
- [ ] Wire `handle_order_message` into `frappe_whatsapp` webhook router (it doesn't auto-listen yet — needs a hook)
- [ ] Field map audit: confirm `Item.brand` field path, `Website Item.website_image` vs `Item.image` resolution priority

## Conventions

- New app — no upstream constraint. Free to refactor.
- All Meta API calls go through `Meta Catalog Settings.get_token()` + `get_graph_root()` for graph_api_version centralization.
- Push errors → `frappe.log_error` with Meta response body (first 2000 chars). Don't fail the Item save.
- Background queue: `short` for single-Item pushes, `long` for `full_reconcile`.

---

*Living doc. Update when you wire a phase + verify on real Meta Catalog.*
