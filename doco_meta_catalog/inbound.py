"""Inbound WhatsApp order -> draft Sales Order, picked off ASYNCHRONOUSLY from the WhatsApp Message
doctype.

frappe_whatsapp owns Meta's WABA webhook and persists every inbound message (including `order` carts,
as a WhatsApp Message row with content_type='order' + product_catalog_json). We DO NOT front that
webhook — instead a doc-event reacts to the persisted row and enqueues order->SO in the background.

Why this shape (vs an HMAC edge webhook):
  - No single point of failure: a connector bug/outage cannot blackhole the live inbox; chat keeps
    flowing through frappe_whatsapp exactly as before.
  - No Administrator elevation inside a guest request path; the worker job runs server-side.
  - Order building is decoupled from Meta's webhook timeout/retry window; a failure lands in the RQ
    failed-job queue (visible + retryable), not silently lost.
There is no Meta HMAC on this path (frappe_whatsapp's webhook is unsigned); the forged-order blast
radius is instead bounded by handle_order_message (server re-pricing, publish_on_web gate, caps,
dedup, DRAFT-only). Wired in hooks.py: WhatsApp Message after_insert.
"""

from __future__ import annotations

import json

import frappe


def on_whatsapp_message(doc, method=None):
    """after_insert on WhatsApp Message — enqueue order->SO for INBOUND `order` rows only."""
    if (doc.get("content_type") or "") != "order":
        return
    if (doc.get("type") or "").lower() == "outgoing":
        return  # ignore our own echoes / outgoing rows
    frappe.enqueue(
        "doco_meta_catalog.inbound.process_order",
        queue="short",
        job_id=f"wa_order::{doc.name}",
        deduplicate=True,
        wa_message=doc.name,
    )


def process_order(wa_message: str):
    """Background worker: rebuild the Meta order payload from the WhatsApp Message row and create a
    DRAFT Sales Order. Runs as Administrator (background job). A raise lands in the RQ failed queue."""
    frappe.set_user("Administrator")
    from doco_meta_catalog import wa_helpers

    row = frappe.db.get_value(
        "WhatsApp Message",
        wa_message,
        ["from", "message_id", "product_catalog_json", "content_type", "type"],
        as_dict=True,
    )
    if not row or (row.get("content_type") or "") != "order":
        return
    try:
        order = json.loads(row.get("product_catalog_json") or "{}")
    except Exception:
        order = {}
    message = {"id": row.get("message_id"), "from": row.get("from"), "order": order}
    wa_helpers.handle_order_message(message, trusted=True)
