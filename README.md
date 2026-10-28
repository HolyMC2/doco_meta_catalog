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

Configure under **Desk → Meta Catalog Settings**: `catalog_id`, token (reuse a WhatsApp
Account or set `access_token` directly — must carry `catalog_management` scope), default
brand/condition, optional `category_map` (item group → condition / google category). Flip
**Enabled** and run **Sync All Now** for the first push.

## Architecture
- Catalog == the live storefront: the eligible set + price + stock + image all come from
  `doco.docoutils.storefront` (single source of truth), so Facebook/Instagram/WhatsApp
  show exactly what the web shop sells.
  - gate: `Item.publish_on_web` (not `show_in_website`); leaves only (`has_variants=0`)
  - price: `Item Price.price_list_rate`; availability: live Bin; image: public-only guard
  - variants pushed individually, grouped on Meta via `item_group_id`
- `Item.on_update` → `sync.queue_item_sync` → enqueue background job → POST to
  `/{catalog_id}/items_batch` (`item_type=PRODUCT_ITEM`)
- `Item.on_trash` → same path with `method=DELETE`
- Daily cron `full_reconcile` re-pushes everything (safety net for missed events)
- `dry_run()` builds the whole payload WITHOUT posting — inspect parity + skip reasons first
- One web shop (Storefront Profile) per catalog: its hidden items, scope, price list,
  warehouses and domain; product links go to `/producto/<slug>` on that domain
- An item with a price problem (two current prices, its own pricing rule) is held at its
  last Meta price; the rest of the catalog still syncs
- Desk: **Meta Catalog Settings** shows what reaches Meta and what needs fixing, with an
  action per problem; the Item form shows each product's Meta status. A daily read-only check
  reads Meta's review per product
- `wa_helpers.send_product_list(to, sections, body)` — send catalog messages over WhatsApp

## Status
Live on prod `ventas.docomexico.com` (catalog enabled; products still `staging`). Not
installed on Mumu: see `docs/mumu-enablement.md`. Open items: `AGENTS.md` → Outstanding.
