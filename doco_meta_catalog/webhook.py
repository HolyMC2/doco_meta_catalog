"""Inbound WhatsApp webhook for the Meta catalog connector — the HMAC trust boundary.

frappe_whatsapp's own webhook (image-baked) does NOT verify Meta's `X-Hub-Signature-256`, so a
forged POST to it is indistinguishable from a real one. To get authenticity WITHOUT patching the
baked image, point Meta's webhook URL at THIS endpoint:

    https://ventas.docomexico.com/api/method/doco_meta_catalog.webhook.webhook

It (1) answers Meta's GET verification challenge (reusing frappe_whatsapp's verify token),
(2) on POST, recomputes the HMAC over the RAW body and 403s fail-closed on any mismatch BEFORE
any parse / DB write, then (3) delegates to frappe_whatsapp's normal processing (chat / status /
persistence unchanged) and (4) builds DRAFT Sales Orders from any `order` messages via the
hardened `wa_helpers.handle_order_message`.

Setup (Marco): set `Meta Catalog Settings.app_secret` (App Dashboard → Settings → Basic → App
Secret), then repoint the Meta webhook callback URL from the frappe_whatsapp path to this one.
The verify token stays whatever frappe_whatsapp already uses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re

import frappe

from doco_meta_catalog import wa_helpers

SETTINGS_DOCTYPE = "Meta Catalog Settings"


def _app_secret():
    return frappe.get_cached_doc(SETTINGS_DOCTYPE).get_app_secret()


def verify_signature(raw_body: bytes, header: str | None, secret: str | None) -> bool:
    """True iff `header` == 'sha256=' + HMAC_SHA256(secret, raw_body), compared in constant time.
    Fail-closed: a missing secret, header, or 'sha256=' prefix → False."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    provided = header.split("=", 1)[1]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", provided):
        return False  # not a sha256 hex digest (e.g. non-ASCII) → reject, never raise
    expected = hmac.new(secret.encode("utf-8"), raw_body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.lower())


@frappe.whitelist(allow_guest=True)
def webhook():
    if frappe.request.method == "GET":
        # reuse frappe_whatsapp's hub.verify_token challenge (token already configured there)
        from frappe_whatsapp.utils import webhook as fw

        return fw.get()

    raw = frappe.request.get_data() or b""
    sig = frappe.get_request_header("X-Hub-Signature-256")
    if not verify_signature(raw, sig, _app_secret()):
        # FAIL CLOSED before any parse / DB write / delegation — store nothing on a bad signature.
        frappe.throw("Invalid webhook signature", frappe.PermissionError)

    # HMAC-authenticated → process as Administrator. The request runs as Guest, but both our order
    # reads (Item / Item Price) and frappe_whatsapp's post() (inserts ignore_permissions, but some
    # reads use the session user) need privileges. Isolate the delegate's errors and ALWAYS return
    # 200: this is the LIVE shared WhatsApp callback, and a non-2xx would make Meta retry then
    # DISABLE it. Orders are committed idempotently in _process_orders, so a delegate hiccup that we
    # swallow cannot lose them (the error is logged, not silent).
    _user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        _process_orders(raw)  # draft Sales Orders from order messages (independent + self-guarded)
        from frappe_whatsapp.utils import webhook as fw

        try:
            fw.post()  # frappe_whatsapp's normal inbox/status processing — behaviour unchanged
        except Exception:
            frappe.log_error(
                title="meta catalog webhook: frappe_whatsapp delegate error (logged, returning 200)",
                message=frappe.get_traceback(),
            )
    finally:
        frappe.set_user(_user)
    return "ok"


def _process_orders(raw: bytes) -> None:
    try:
        data = json.loads(raw or b"{}")
    except Exception:
        return
    for entry in data.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = (change or {}).get("value", {}) or {}
            account = _resolve_account(value)
            for msg in value.get("messages", []) or []:
                if (msg or {}).get("type") == "order":
                    try:
                        wa_helpers.handle_order_message(msg, account, signature_verified=True)
                    except Exception:
                        frappe.log_error(
                            title="meta catalog webhook: order->SO failed",
                            message=frappe.get_traceback(),
                        )


def _resolve_account(value: dict):
    """Map the inbound message's phone_number_id → the local WhatsApp Account name."""
    pid = ((value or {}).get("metadata") or {}).get("phone_number_id")
    if not pid:
        return None
    return frappe.db.get_value("WhatsApp Account", {"phone_id": pid}, "name")
