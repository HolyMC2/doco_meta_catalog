# Doco account verification

Observed 2026-09-26 America/Mazatlan (latest Facebook reads 2026-09-27 00:07 UTC).
These are account observations, not release acceptance. No publication, message or payment was
performed for these checks. Credentials remained in the existing Doco backend; retained receipts
exclude access tokens and provider pagination URLs.

| Capability | Observed | Remaining proof |
|---|---|---|
| Catalog ownership/read | Existing Doco catalog readable, 2,711 products | Current per-item price, stock, visibility and publication receipt reconciliation |
| WhatsApp catalog connection | User screenshot shows the selected catalog connected; catalog/cart toggles read true | WABA catalog edge still returns an empty list; preserve both observations without reconnecting |
| WhatsApp product review | Two sampled products report `WHATSAPP=APPROVED`, `visibility=staging` | Actual selected-item visibility and owned-recipient product/cart journey |
| Facebook Page | Doco Page appears in the owned business's `owned_pages` result | Page-specific receiving/reply and commerce visibility acceptance |
| Facebook Shop | Business `commerce_merchant_settings` GET returns 403 / OAuth code 200 | Account UI or appropriately authorized provider evidence; no claim that a shop exists or is absent |
| Facebook Marketplace listing | Not verified by these reads | Separate supported API/account/region contract and actual listing evidence |
| Payment | External Mercado Pago links and POS payment are the selected path | Native order/payment/receipt/fulfilment/return acceptance; no native Meta checkout claim |

The permission denial says the app lacks a required permission or capability. It does not identify
which requirement is missing. Meta's permissions reference returned HTTP 429 during research;
we did not confirm a permission name, an App Review remedy or Mexico eligibility from that page.
Do not broaden grants or create duplicate commerce assets based only on this response.

Provider contracts consulted:

- Meta's [Business SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/business.py)
  defines the read-only `owned_pages` and `commerce_merchant_settings` edges.
- Its [CommerceMerchantSettings type](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/commercemerchantsettings.py)
  defines merchant status, Page and channel fields; field existence does not prove account eligibility.
- Its [ProductCatalog type](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/productcatalog.py)
  exposes catalog ownership and commerce settings separately.
- The Meta-maintained [WhatsApp Cloud API collection](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api)
  documents phone-scoped commerce toggles separately from messages and delivery receipts.

The candidate's `commerce.get_context()` therefore continues reporting Facebook catalog visibility,
Shop and Marketplace listing as `unverified`, with an external checkout path and Commerce Manager
handoff. Native permissions, outbox controls, actual provider receipt and complete worker journeys
remain required before the CRM roadmap can be accepted.
