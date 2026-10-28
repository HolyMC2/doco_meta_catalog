# Turning the Meta catalog on for Mumu (ventas.mumulenceria.com)

Read from prod on 2026-10-27 (America/Mazatlán). Every step below is a prod or Meta write
and needs Marco's go.

## What is already enough

- **The connection.** Mumu's "Mumu Ventas" WhatsApp account uses the same Meta user key as
  Doco. That key has `catalog_management` and `business_management` on the Docomexico
  business, which owns the Mumu WhatsApp account and the Mumu page. No new permission is
  needed to create or fill a Mumu catalog.
- **The products.** 174 priced products, every one with a photo. The shop profile
  `mumulenceria` supplies the scope, price list, warehouses and domain.
- **The code.** `doco_meta_catalog` is in the cell-1 image already. It is not installed on
  the site.

## What it still needs

1. **A Mumu catalog in Commerce Manager.** The business owns only `Catalogue_Products`
   (Doco's) and an empty "ERP DOCO". Create an E-commerce catalog named for Mumu. Do not
   reuse Doco's: one catalog per shop, or the two shops' products mix on WhatsApp.
2. **Connect that catalog to the Mumu WhatsApp number.** In WhatsApp Manager → Catalog, pick
   the new catalog and turn on "Show catalog" and "Cart". Mumu's commerce settings are empty
   today, so WhatsApp shows no catalog.
3. **Install the app on the site** (prod write, cell-1):
   `bench --site ventas.mumulenceria.com install-app doco_meta_catalog`, then migrate.
4. **Meta Catalog Settings on Mumu:**
   - Catalog ID: the new catalog's number
   - WhatsApp account: `Mumu Ventas`
   - Currency: `MXN`
   - Web Shop: `mumulenceria`. It is the only enabled shop, so leaving it empty also works.
   - Visibility: start with `staging`. Use the overview's "Preview the catalog" to check
     what would go out, then "Show products to shoppers".
   - Tick Enabled, then **Catalog → Send all now**.
5. **Check Meta now** from the same page. The overview then shows Meta's review per channel.

## Caveats (shared with Doco)

- **Business verification.** It is `pending_submission`, and `commerce_merchant_settings`
  returns 403. Doco's catalog takes products without verification: 2,733 items, read on
  2026-10-27. Facebook Shop and Instagram Shopping need a verified business with Shops set
  up; that is not confirmed here.
- **The key is Marco's personal user token.** Its data-access window
  (`data_access_expires_at`) passed on 2026-10-19, though calls still answer 200. Replace it
  with a System User token that has the catalog assigned at full control. Then rotation does
  not depend on one person's login, and both shops keep working.
- **"Doco Ventas Escuinapa".** The Mumu site has a WhatsApp Account with this name. Check
  that it belongs there before you choose the account in step 4.
