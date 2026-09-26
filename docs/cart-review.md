# Reviewed WhatsApp carts

A cart is evidence to review, not an automatic order. The signed durable receiver's current Processing receipt and the WhatsApp Message must agree on provider, app, phone account, business, sender, message ID and the complete original order payload. Intake runs synchronously in the receiver's transaction; only that owner commits. Replays can fill a missing intake and never send a message.

The same provider message ID on two accounts produces two independent receipt identities. Meta Order Log remains the sole intake ledger. Its original message IDs and existing Sales Order links survive migration. Historical logs are explicitly `account_unknown`; a manager may view permitted historical order links, but no phone match or default account assigns them a new identity.

In the CRM conversation, open the cart and select a Customer, Company, leaf Warehouse and optional Deal. Each API reloads the cart's exact canonical conversation and checks the current actor's channel/shop/reference permissions. The selected ERP documents require actor read access, and Sales Order creation uses the actor's ordinary create permission. Raw receipt payloads, review fingerprints and account evidence stay behind the private broker. Generic reads and DocShare grants do not expose them. The supported Muelle shared-document scope hook version 1 is required; installation, migration and schema-active requests fail closed on an incompatible base.

Every requested line and value stays visible, including malformed and duplicate lines. Unknown/unpublished/unpriced products, catalog or currency mismatch, invalid quantities, configured limits, insufficient stock and ambiguous prices block creation. Price changes and duplicate lines are review advisories. No line is dropped, merged or capped. The configured catalog account and currency are explicit; no default-account fallback is used.

Review uses the configured Selling Settings price list and current applicable Item Price rows. Ambiguous rows require cleanup. Catalog markup is supplied as the native ERP margin percentage, and the native selected-customer/quantity pricing rules and taxes determine the final displayed rates and total. Generic catalog publication cannot assert customer-specific discounts and has its own explicit pricing-rule blocker. Stock availability is scoped to the selected Warehouse, includes reserved quantities and product-bundle component demand, and is advisory until a later native stock action.

Native pricing preview never inserts a Sales Order. The combination of automatic Item Price insertion and updating existing prices is blocked because ERP's normal preview path could write prices as a side effect; an operator must review that configuration. A 15-minute review token freezes actor, selected documents, receipt fingerprint, quote, rules and native financial totals. Create takes the conversation fence and intake row lock, rechecks current price/stock/configuration and inserts one native draft. If any snapshot or native insertion totals differ, it requires a new review and rolls back only the create savepoint. Repeated clicks return the existing permitted draft.

The resulting record is a draft Sales Order. It is not an invoice, payment, stock reservation or submitted commitment. Continue through the linked canonical ERP record and the owning payment/POS workflow. Buyer notes and original strings must render as text, never HTML.

The obsolete asynchronous `wa_order` worker and `handle_order_message` entry point are harmless no-ops. They cannot create customers or orders, elevate an actor, commit, or send. Receipt and cart APIs also never create a Customer from a phone number.

Verification commands:

- Pure contract: `python3 -m unittest doco_meta_catalog.tests.test_orders_contract -v`.
- Native graph: `heavy -- ... bench --site <isolated-site> run-tests --module doco_meta_catalog.tests.test_orders_native` and `--module doco_meta_catalog.tests.test_inbound`.
- Run native tests after supported framework patch and additive schema migration. A native two-session concurrency proof and legacy-index migration proof are distinct integration gates; pure tests do not claim them.
