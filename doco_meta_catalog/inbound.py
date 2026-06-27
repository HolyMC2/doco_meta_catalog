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

_SETTINGS = "Meta Catalog Settings"


def on_whatsapp_message(doc, method=None):
    """after_insert on WhatsApp Message — ASYNC dispatch for INBOUND messages:
      - `order` carts          -> draft Sales Order        (process_order)
      - our namespaced buttons -> interactive menu reply    (process_menu_button, MA-2)
      - text with a [ref:CODE] -> deep-link attribution     (process_inbound_text, MA-6)

    Runs INSIDE frappe_whatsapp's still-open insert transaction, so every dispatch is
    enqueue_after_commit (the row must exist for the worker) and guarded — a Redis/RQ
    outage must NEVER bubble through doc.insert() and 500 / roll back the live inbox.
    """
    if (doc.get("type") or "").lower() == "outgoing":
        return  # ignore our own echoes / outgoing rows
    ct = doc.get("content_type") or ""
    if ct == "order":
        _enqueue("process_order", doc.name, "wa_order")
    elif ct == "button" and (doc.get("message") or "").startswith("doco:"):
        _enqueue("process_menu_button", doc.name, "wa_menu")  # MA-2 (gate checked in worker)
    elif ct == "text" and _wants_text_capture(doc):
        _enqueue("process_inbound_text", doc.name, "wa_reftext")  # MA-6


def _wants_text_capture(doc) -> bool:
    """Cheap pre-filter so we don't spawn a worker for every chat line: only when the
    body actually carries a ref token AND deep-link capture is enabled."""
    from doco_meta_catalog import deeplinks
    if not deeplinks.parse_ref(doc.get("message")):
        return False
    return bool(frappe.db.get_single_value(_SETTINGS, "deeplink_capture_enabled"))


def _enqueue(method: str, name: str, prefix: str) -> None:
    try:
        frappe.enqueue(
            f"doco_meta_catalog.inbound.{method}",
            queue="short",
            job_id=f"{prefix}::{name}",
            deduplicate=True,
            enqueue_after_commit=True,
            wa_message=name,
        )
    except Exception:
        frappe.log_error(title=f"WA {prefix} enqueue failed", message=frappe.get_traceback())


def process_menu_button(wa_message: str):
    """MA-2 worker: a customer tapped one of OUR namespaced menu buttons -> send the
    matching interactive reply. Gated by inbound_menu_enabled (default off) so the
    built-in menu never competes with a chatflow unless Marco opts in."""
    from doco_meta_catalog import wa_helpers

    row = frappe.db.get_value(
        "WhatsApp Message", wa_message, ["from", "message", "content_type", "type"], as_dict=True)
    if not row or (row.content_type or "") != "button" or (row.type or "").lower() == "outgoing":
        return
    bid = row.message or ""
    if not bid.startswith(wa_helpers.MENU_PREFIX):
        return
    s = frappe.get_cached_doc(_SETTINGS)
    if not s.get("inbound_menu_enabled"):
        return
    frappe.set_user("Administrator")  # senders are role-gated; the bot acts as the system
    to = row.get("from")
    if bid == wa_helpers.MENU_CATALOG:
        wa_helpers.send_catalog_message(to)
    elif bid == wa_helpers.MENU_ORDER:
        wa_helpers.send_catalog_message(to, body="Arma tu pedido desde el catálogo y envíalo 🛒")
    elif bid == wa_helpers.MENU_PAY:
        url = (s.get("checkout_url") or "").strip()
        if url:
            wa_helpers.send_cta_url(to, "Completa tu pago aquí 👇", url, button_text="Pagar")
        else:
            frappe.log_error(title="MA-2 pay button: no checkout_url configured",
                             message=f"wa_message={wa_message}")


def process_inbound_text(wa_message: str):
    """MA-6 worker: an inbound text carries a [ref:CODE] deep-link token -> record
    attribution (CRM Touchpoint). Passive; no customer-facing send."""
    from doco_meta_catalog import deeplinks

    row = frappe.db.get_value(
        "WhatsApp Message", wa_message,
        ["from", "message", "content_type", "type", "message_id"], as_dict=True)
    if not row or (row.content_type or "") != "text" or (row.type or "").lower() == "outgoing":
        return
    ref = deeplinks.parse_ref(row.message)
    if not ref:
        return
    frappe.set_user("Administrator")
    deeplinks.record_attribution(ref=ref, channel="WhatsApp", phone=row.get("from"),
                                 message_id=row.get("message_id"))


def process_order(wa_message: str):
    """Background worker: rebuild the Meta order payload from the WhatsApp Message row and create a
    DRAFT Sales Order. A raise lands the job in the RQ failed queue (visible + retryable) — we never
    swallow a real persisted order."""
    from doco_meta_catalog import wa_helpers

    row = frappe.db.get_value(
        "WhatsApp Message",
        wa_message,
        ["from", "message_id", "product_catalog_json", "content_type", "type"],
        as_dict=True,
    )
    if not row or (row.get("content_type") or "") != "order":
        return
    frappe.set_user("Administrator")  # past the guards; the SO/Customer inserts need elevation
    try:
        order = json.loads(row.get("product_catalog_json") or "{}")
    except Exception:
        frappe.log_error(title="WA order JSON parse failed", message=f"wa_message={wa_message}")
        raise  # do not silently drop a real order — let it land in the RQ failed queue
    if not isinstance(order, dict):
        order = {}  # a valid-JSON list/scalar is not an order payload
    message = {"id": row.get("message_id"), "from": row.get("from"), "order": order}
    wa_helpers.handle_order_message(message, trusted=True)
