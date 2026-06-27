"""MA-4 — add ctwa_clid + ctwa_source_id custom fields to WhatsApp Message.

frappe_whatsapp is upstream (baked); we attach the capture target as Custom Fields
rather than forking. They are read-only and stay empty until something populates the
inbound referral (frappe_whatsapp drops it today) — the CTWA loop reads them when filled.
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    if not frappe.db.exists("DocType", "WhatsApp Message"):
        return
    create_custom_fields({
        "WhatsApp Message": [
            {"fieldname": "ctwa_clid", "label": "CTWA Click ID", "fieldtype": "Data",
             "read_only": 1, "insert_after": "message_id"},
            {"fieldname": "ctwa_source_id", "label": "CTWA Source ID", "fieldtype": "Data",
             "read_only": 1, "insert_after": "ctwa_clid"},
        ]
    }, ignore_validate=True)
