# Doco Meta Catalog

Bridges ERPNext `Item` records into a Meta Commerce Catalog so the same product list serves:

- **WhatsApp Business** product/cart messages (`interactive.type=product`, `product_list`)
- **Facebook Shop**
- **Instagram Shopping**

## Why
Avoids a parallel WooCommerce catalog. ERPNext is the single source of truth; Meta is the
broadcast surface. Hooks Item.on_update + a nightly reconcile cron keep them in sync.

## Install
```bash
bench get-app https://github.com/HolyMC2/doco_meta_catalog
bench --site <site> install-app doco_meta_catalog
```

Configure under **Desk → Meta Catalog Settings**: catalog_id, access_token, default brand,
default condition, default availability. Run **Sync All Now** for the first push.

## Architecture
- `Item.on_update` → `sync.queue_item_sync` → enqueue background job → POST to
  `/{catalog_id}/items_batch` (Meta Graph API)
- `Item.on_trash` → same path with method=DELETE
- Daily cron `full_reconcile` re-pushes everything (safety net for missed events)
- `meta_helpers.send_product_list(to_number, product_retailer_ids, body_text)` —
  used from doctype workflows / chat to send catalog messages

## Status
v0.1.0 — scaffold + Meta Catalog Settings doctype + items_batch sync.
