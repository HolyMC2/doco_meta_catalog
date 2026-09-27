app_name = "doco_meta_catalog"
app_title = "Doco Meta Catalog"
app_publisher = "Doco México"
app_description = "Sync ERPNext Items into Meta Commerce Catalog"
app_email = "doco.mexico@gmail.com"
app_license = "mit"
# doco is the shared core: storefront publish gate / Item Price / Bin / image guard are
# reused from doco.docoutils.storefront so the Meta catalog == the live web shop.
required_apps = ["frappe", "erpnext", "frappe_whatsapp", "doco"]

# Unsupported cores only disable cart intake/review; install and migrate never block.
before_install = "doco_meta_catalog.orders.warn_if_unsupported"
before_migrate = "doco_meta_catalog.orders.warn_if_unsupported"
after_install = "doco_meta_catalog.install.ensure_catalog_schema"
after_migrate = "doco_meta_catalog.install.ensure_catalog_schema"

# Optional in CRM: the adapter grants no account access or transport authority.
crm_catalog_commerce = "doco_meta_catalog.commerce"

doc_events = {
    "Item": {
        "on_update": "doco_meta_catalog.sync.queue_item_sync",
        "on_trash":  "doco_meta_catalog.sync.queue_item_delete",
    },
    "Item Price": {
        "on_update": "doco_meta_catalog.sync.queue_price_or_stock_sync",
        "on_trash": "doco_meta_catalog.sync.queue_price_or_stock_sync",
    },
    "Bin": {
        "on_update": "doco_meta_catalog.sync.queue_price_or_stock_sync",
        "on_change": "doco_meta_catalog.sync.queue_price_or_stock_sync",
    },
    # ERPNext writes many Bin quantities via db.set_value, bypassing Bin hooks.
    "Stock Ledger Entry": {
        "after_insert": "doco_meta_catalog.sync.queue_price_or_stock_sync",
    },
    "Sales Order": {
        "on_submit": "doco_meta_catalog.sync.queue_document_stock_sync",
        "on_cancel": "doco_meta_catalog.sync.queue_document_stock_sync",
        "on_update_after_submit": "doco_meta_catalog.sync.queue_document_stock_sync",
    },
    # Inbound WhatsApp cart -> draft Sales Order, picked off async (frappe_whatsapp owns the WABA
    # webhook + persists order rows). NOT a fronting webhook — see inbound.py.
    "WhatsApp Message": {
        "after_insert": "doco_meta_catalog.inbound.on_whatsapp_message",
    },
    # MA-3 Conversions API: server-side Purchase / Lead (no-op unless capi_enabled).
    "Sales Invoice": {
        "on_submit": "doco_meta_catalog.capi.on_sales_invoice_submit",
    },
    "CRM Lead": {
        "after_insert": "doco_meta_catalog.capi.on_crm_lead_insert",
    },
}

scheduler_events = {
    "cron": {"*/5 * * * *": ["doco_meta_catalog.publication.process_pending"]},
    "daily": [
        "doco_meta_catalog.sync.full_reconcile",
    ],
}

# Raw receipt/cart evidence is available only through the conversation-authorized broker.
permission_query_conditions = {
    "Meta Order Log": "doco_meta_catalog.doco_meta_catalog.doctype.meta_order_log.meta_order_log.get_permission_query_conditions",
}
has_permission = {
    "Meta Order Log": "doco_meta_catalog.doco_meta_catalog.doctype.meta_order_log.meta_order_log.has_permission",
}
filter_shared_documents = {
    "Meta Order Log": "doco_meta_catalog.doco_meta_catalog.doctype.meta_order_log.meta_order_log.filter_shared_documents",
}
