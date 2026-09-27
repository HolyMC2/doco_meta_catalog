"""Catalog intake from authenticated, durable WhatsApp receipts.

The owning frappe_whatsapp receiver verifies the envelope and account before
consuming one durable atom. Order review is local work in that same transaction;
no sends, automatic customer matching or ERP order creation occur on ingestion.
Old elevated menu/Flow/media/referral jobs are retired until their respective
native CRM recipes carry explicit account, identity and conversation authority.
"""

from __future__ import annotations

DETERMINISTIC_INTAKE_FAILURES = frozenset({"order_receipt_invalid", "intake_identity_conflict"})

def on_whatsapp_message(doc, method=None):
    """Persist a cart inside its authenticated receipt transaction.

    Legacy menu, Flow, referral and media handlers cannot carry native broker
    authority. They stay inactive until a governed CRM recipe owns the action.
    """
    if (doc.get("type") or "").lower() == "outgoing":
        return
    if doc.get("content_type") == "order":
        from doco_meta_catalog.orders import intake, intake_unavailable
        from doco_meta_catalog.orders_contract import CartError

        try:
            return intake(doc)
        except CartError as error:
            # Deterministic evidence failures must not roll back the customer's message;
            # claim/lease failures stay transient so the receipt worker retries.
            if error.reason not in DETERMINISTIC_INTAKE_FAILURES:
                raise
            return intake_unavailable(doc, error.reason)


def process_menu_button(wa_message: str):
    """Old queued jobs must never regain the former Administrator authority."""
    return {"state": "Blocked", "reason_code": "native_recipe_required"}


def process_ctwa(wa_message: str):
    """Old queued jobs must never regain the former Administrator authority."""
    return {"state": "Blocked", "reason_code": "native_recipe_required"}


def process_flow_intake(wa_message: str):
    """Old queued jobs must never regain the former Administrator authority."""
    return {"state": "Blocked", "reason_code": "native_recipe_required"}


def process_media(wa_message: str):
    """Old queued jobs must never regain the former Administrator authority."""
    return {"state": "Blocked", "reason_code": "native_recipe_required"}


def process_inbound_text(wa_message: str):
    """Old queued jobs must never regain the former Administrator authority."""
    return {"state": "Blocked", "reason_code": "native_recipe_required"}


def process_order(wa_message: str):
    """Retired queue entry point; only the receipt transaction may create an intake."""
    return {"state": "Blocked", "reason_code": "receipt_transaction_required"}
