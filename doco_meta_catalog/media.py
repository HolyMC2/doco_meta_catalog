"""MA-12 — inbound WhatsApp media -> Repair Order attachment.

frappe_whatsapp downloads inbound media and attaches it to the WhatsApp Message (the
`attach` field is the File url). We pick off image/video messages and re-attach the same
file to the sender's OPEN Repair Order (e.g. a cracked-screen photo before drop-off), so
the technician sees it on the RO. Cross-app by doctype NAME only (no taller import); a
no-op when taller isn't installed or the sender has no open RO. Gated by
Meta Catalog Settings.media_capture_enabled.
"""
from __future__ import annotations

import re

import frappe

_SETTINGS = "Meta Catalog Settings"
_MEDIA_TYPES = ("image", "video")
# Statuses that mean the RO is still active (worth attaching a photo to).
_CLOSED = ("Entregado", "Cancelado")


def _open_ro_for_phone(phone: str | None) -> str | None:
    """Most recent OPEN Repair Order whose client Contact matches the phone (last 10
    digits). None if taller absent / no contact / no open RO."""
    digits = re.sub(r"\D", "", str(phone or ""))[-10:]
    if len(digits) < 10:
        return None
    contact = frappe.db.get_value("Contact", {"mobile_no": ["like", f"%{digits}"]}, "name")
    if not contact:
        return None
    rows = frappe.get_all(
        "Repair Order",
        filters={"client": contact, "status": ["not in", _CLOSED]},
        fields=["name"], order_by="creation desc", limit=1)
    return rows[0].name if rows else None


def process_media(wa_message: str):
    """Worker: attach an inbound photo/video to the sender's open RO. Idempotent per
    message_id via a Comment marker."""
    row = frappe.db.get_value(
        "WhatsApp Message", wa_message,
        ["from", "attach", "content_type", "type", "message_id", "message"], as_dict=True)
    if not row or (row.content_type or "") not in _MEDIA_TYPES or (row.type or "").lower() == "outgoing":
        return
    if not row.get("attach"):
        return  # frappe_whatsapp hasn't attached the file (download failed)
    if not frappe.db.get_single_value(_SETTINGS, "media_capture_enabled"):
        return
    if not frappe.db.exists("DocType", "Repair Order"):
        return
    ro = _open_ro_for_phone(row.get("from"))
    if not ro:
        return
    marker = f"wa-media:{row.get('message_id')}"
    if frappe.db.exists("Comment", {"reference_doctype": "Repair Order", "reference_name": ro, "content": marker}):
        return

    frappe.set_user("Administrator")
    file_url = row.get("attach")
    frappe.get_doc({
        "doctype": "File",
        "file_url": file_url,
        "file_name": file_url.rsplit("/", 1)[-1],
        "attached_to_doctype": "Repair Order",
        "attached_to_name": ro,
        "is_private": 1 if "/private/" in file_url else 0,
    }).insert(ignore_permissions=True)
    caption = (row.get("message") or "").strip()
    frappe.get_doc({
        "doctype": "Comment", "comment_type": "Comment",
        "reference_doctype": "Repair Order", "reference_name": ro,
        "content": f"📷 Foto recibida por WhatsApp{(': ' + caption) if caption else ''}",
    }).insert(ignore_permissions=True)
    frappe.get_doc({
        "doctype": "Comment", "comment_type": "Info",
        "reference_doctype": "Repair Order", "reference_name": ro, "content": marker,
    }).insert(ignore_permissions=True)
    return ro
