import frappe


def execute():
    """The inbound HMAC webhook was removed (replaced by an async WhatsApp Message doc-event pickoff),
    so Meta Catalog Settings.app_secret has no consumer. Drop the stored secret value so a copy of the
    Meta App Secret is not left sitting in the database."""
    frappe.db.sql(
        "delete from tabSingles where doctype = %s and field = %s",
        ("Meta Catalog Settings", "app_secret"),
    )
    frappe.db.commit()
