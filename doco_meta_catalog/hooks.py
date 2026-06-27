app_name = "doco_meta_catalog"
app_title = "Doco Meta Catalog"
app_publisher = "Doco México"
app_description = "Sync ERPNext Items into Meta Commerce Catalog"
app_email = "doco.mexico@gmail.com"
app_license = "mit"
# doco is the shared core: storefront publish gate / Item Price / Bin / image guard are
# reused from doco.docoutils.storefront so the Meta catalog == the live web shop.
required_apps = ["frappe", "erpnext", "frappe_whatsapp", "doco"]

doc_events = {
    "Item": {
        "on_update": "doco_meta_catalog.sync.queue_item_sync",
        "on_trash":  "doco_meta_catalog.sync.queue_item_delete",
    },
    # Inbound WhatsApp cart -> draft Sales Order, picked off async (frappe_whatsapp owns the WABA
    # webhook + persists order rows). NOT a fronting webhook — see inbound.py.
    "WhatsApp Message": {
        "after_insert": "doco_meta_catalog.inbound.on_whatsapp_message",
    },
}

scheduler_events = {
    "daily": [
        "doco_meta_catalog.sync.full_reconcile",
    ],
}
