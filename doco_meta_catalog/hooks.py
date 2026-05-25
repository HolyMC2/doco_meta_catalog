app_name = "doco_meta_catalog"
app_title = "Doco Meta Catalog"
app_publisher = "Doco México"
app_description = "Sync ERPNext Items into Meta Commerce Catalog"
app_email = "doco.mexico@gmail.com"
app_license = "mit"
required_apps = ["frappe", "erpnext", "frappe_whatsapp"]

doc_events = {
    "Item": {
        "on_update": "doco_meta_catalog.sync.queue_item_sync",
        "on_trash":  "doco_meta_catalog.sync.queue_item_delete",
    },
}

scheduler_events = {
    "daily": [
        "doco_meta_catalog.sync.full_reconcile",
    ],
}
