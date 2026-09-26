"""WhatsApp Cloud API helpers for catalog messages + cart-order ingestion.

Outbound:
    send_catalog_message(to, body)                       single tappable button -> entire catalog
    send_product_message(to, retailer_id, body, footer)  one product card
    send_product_list(to, sections, body, header, footer) multi-section product list (up to 30 items)

Inbound (called from a router in webhook.py):
    handle_order_message(msg, account) -> "Sales Order" name  (creates draft SO from cart payload)

Legacy outbound endpoints fail closed; catalog delivery uses the CRM conversation outbox.
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _

from doco_meta_catalog.utils import assert_outbound_allowed

# Reuse the storefront's per-IP + global rate limiter so a single compromised / low-priv
# Desk session cannot blast the business WhatsApp number or burn the metered quota.
from doco.docoutils import storefront as _sf
from frappe.utils import escape_html, flt

# sync provides the canonical "published, sellable leaf" gate reused to validate inbound order
# lines against the same universe the catalog publishes.
from doco_meta_catalog import sync

_E164 = re.compile(r"^\+?\d{8,15}$")
_SEND_ROLES = ["System Manager", "Sales User"]


def _guard_send(to: str | None = None) -> None:
    """Authorize + rate-limit + validate recipient for EVERY outbound WhatsApp send.
    The WABA number is a verified business asset — only real operators may send from it,
    never faster than the bucket. SECURITY: do not weaken/remove; these senders are
    @frappe.whitelist() and are otherwise reachable by any low-privilege Desk login."""
    frappe.only_for(_SEND_ROLES)
    _sf._rate_limit("wa_send", limit=30, window_sec=60)
    if to is not None and not _E164.match(str(to or "").strip()):
        frappe.throw(_("Invalid recipient phone number"))


def _canon_phone(p: str) -> str:
    """Digits-only canonical E.164 ('+<digits>') so the same number stored as '+52155…' or
    '52155…' resolves to one Customer — reduces duplicate-customer forking on inbound orders."""
    d = re.sub(r"\D", "", p or "")
    return ("+" + d) if d else ""


def _claim_order(msg_id: str) -> bool:
    """Legacy unsigned/global-ID jobs cannot claim receipt-backed intakes."""
    return False


def _outgoing_account():
    """Pick the WhatsApp Account that sends product messages."""
    s = frappe.get_cached_doc("Meta Catalog Settings")
    if s.whatsapp_account:
        return frappe.get_doc("WhatsApp Account", s.whatsapp_account)
    default = frappe.db.get_value("WhatsApp Account", {"is_default_outgoing": 1}, "name")
    if not default:
        frappe.throw("No default outgoing WhatsApp Account")
    return frappe.get_doc("WhatsApp Account", default)


def _post_message(account, payload):
    """Compatibility endpoint: the old account-default transport is retired.

    A phone number alone is insufficient authority to send. Existing callers
    must supply a canonical conversation, ownership generation and request id
    to crm.api.catalog_commerce.queue_catalog (or the native text composer).
    """
    assert_outbound_allowed()
    frappe.throw(
        _("Open the customer's CRM conversation to send this message with its current owner and account."),
        frappe.PermissionError,
    )


def _catalog_id():
    return frappe.db.get_single_value("Meta Catalog Settings", "catalog_id")


@frappe.whitelist()
def send_catalog_message(to: str, body: str = "Mira nuestro catálogo:", footer: str | None = None):
    """Single-button message that opens the connected catalog inside WhatsApp."""
    _guard_send(to)
    acct = _outgoing_account()
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "catalog_message",
            "body": {"text": body},
            "action": {"name": "catalog_message", "parameters": {"thumbnail_product_retailer_id": ""}},
        },
    }
    if footer:
        payload["interactive"]["footer"] = {"text": footer}
    return _post_message(acct, payload)


@frappe.whitelist()
def send_product_message(to: str, retailer_id: str, body: str = "", footer: str | None = None):
    """Single-product card. retailer_id == ERPNext item_code that was synced."""
    _guard_send(to)
    acct = _outgoing_account()
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "product",
            "body": {"text": body or " "},
            "action": {"catalog_id": _catalog_id(), "product_retailer_id": retailer_id},
        },
    }
    if footer:
        payload["interactive"]["footer"] = {"text": footer}
    return _post_message(acct, payload)


@frappe.whitelist()
def send_product_list(
    to: str,
    sections: list | str,
    body: str,
    header: str = "Productos",
    footer: str | None = None,
):
    """Multi-section interactive product list. sections is a list of dicts:
        [{"title": "Accesorios", "product_items": ["IT-CABLE-USBC", "IT-CARG-30W"]}, ...]
    Max 10 sections, 30 total products across sections.
    """
    _guard_send(to)
    if isinstance(sections, str):
        sections = json.loads(sections)
    acct = _outgoing_account()
    formatted = [
        {
            "title": (s.get("title") or "Productos")[:24],
            "product_items": [{"product_retailer_id": pid} for pid in s.get("product_items", [])],
        }
        for s in sections
    ]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "product_list",
            "header": {"type": "text", "text": header[:60]},
            "body": {"text": body[:1024]},
            "action": {"catalog_id": _catalog_id(), "sections": formatted},
        },
    }
    if footer:
        payload["interactive"]["footer"] = {"text": footer[:60]}
    return _post_message(acct, payload)


@frappe.whitelist()
def send_cta_url(to: str, body: str, url: str, button_text: str = "Pagar", footer: str | None = None):
    """Interactive CTA-URL button — e.g. push the Mercado Pago checkout link (MX has no native Meta
    checkout, so the sale completes off-Meta). Free inside the 24h window. Role/rate/E.164-gated like
    the catalog senders. [roadmap MA-2]"""
    _guard_send(to)
    if not str(url or "").startswith("https://"):
        frappe.throw(_("CTA URL must be an https:// link"))
    acct = _outgoing_account()
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": (body or " ")[:1024]},
            "action": {"name": "cta_url", "parameters": {"display_text": (button_text or "Pagar")[:20], "url": url}},
        },
    }
    if footer:
        payload["interactive"]["footer"] = {"text": footer[:60]}
    return _post_message(acct, payload)


# Namespaced ids for the built-in inbound menu — so a tapped button is unambiguously
# OURS (routed in inbound.py) and never collides with a chatflow's own button ids.
MENU_PREFIX = "doco:"
MENU_CATALOG = "doco:catalog"
MENU_ORDER = "doco:order"
MENU_PAY = "doco:pay"


@frappe.whitelist()
def send_reply_buttons(to: str, body: str, buttons: list | str, header: str | None = None, footer: str | None = None):
    """Up to 3 quick-reply buttons. `buttons` = [{"id": "...", "title": "..."}] (title <=20).
    The tapped reply comes back inbound as a `button` message whose body is the button id."""
    _guard_send(to)
    if isinstance(buttons, str):
        buttons = json.loads(buttons)
    acct = _outgoing_account()
    action = {"buttons": [
        {"type": "reply", "reply": {"id": str(b["id"])[:256], "title": (b.get("title") or " ")[:20]}}
        for b in buttons[:3]
    ]}
    interactive = {"type": "button", "body": {"text": (body or " ")[:1024]}, "action": action}
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    return _post_message(acct, {"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": interactive})


@frappe.whitelist()
def send_list_menu(to: str, body: str, sections: list | str, button_text: str = "Menú",
                   header: str | None = None, footer: str | None = None):
    """Text list menu (<=10 rows total). `sections` = [{"title": "...",
    "rows": [{"id": "...", "title": "...", "description": "..."}]}]. The chosen row
    comes back inbound as a `button` message whose body is the row id."""
    _guard_send(to)
    if isinstance(sections, str):
        sections = json.loads(sections)
    acct = _outgoing_account()
    formatted = [
        {
            "title": (s.get("title") or " ")[:24],
            "rows": [
                {"id": str(r["id"])[:200], "title": (r.get("title") or " ")[:24],
                 "description": (r.get("description") or "")[:72]}
                for r in s.get("rows", [])
            ],
        }
        for s in sections
    ]
    interactive = {
        "type": "list",
        "body": {"text": (body or " ")[:1024]},
        "action": {"button": (button_text or "Menú")[:20], "sections": formatted},
    }
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    return _post_message(acct, {"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": interactive})


@frappe.whitelist()
def send_menu(to: str, body: str = "¿Cómo te ayudamos? 👇", footer: str | None = None):
    """The standard inbound menu: Catálogo · Pedir · Pagar (namespaced ids routed in
    inbound.py). A convenience over send_reply_buttons for the built-in flow."""
    return send_reply_buttons(
        to, body,
        buttons=[
            {"id": MENU_CATALOG, "title": "Ver catálogo"},
            {"id": MENU_ORDER, "title": "Hacer pedido"},
            {"id": MENU_PAY, "title": "Pagar"},
        ],
        footer=footer,
    )


# ---------------- inbound cart / order (security-critical) ----------------
#
# REACHABILITY CONTRACT: builds a Sales Order + Customer with ignore_permissions, so it must run
# ONLY from the trusted inbound path — the `WhatsApp Message` after_insert doc-event in
# doco_meta_catalog.inbound (frappe_whatsapp already received + persisted the inbound order on the
# WABA webhook it owns). It is NOT @frappe.whitelist and refuses to run unless the caller passes
# trusted=True. There is NO Meta HMAC here (frappe_whatsapp's webhook is unsigned), so the blast
# radius of a forged order is bounded structurally instead: EVERY field is re-derived server-side
# (prices from Item Price, NEVER the buyer `item_price`; sellable set from the publish_on_web gate;
# bounded qty/line counts), the message id is deduped, and the SO is a DRAFT (no GL/stock impact
# until a human reviews + submits). Runs ASYNC (enqueued) off the webhook request path.


def handle_order_message(message, whatsapp_account_name=None, trusted=False):
    """Retired compatibility entry point: no Customer, Sales Order or outbound side effects."""
    return None


def _find_or_create_customer(phone: str, contact_name: str | None) -> str:
    """A reviewer must explicitly select a permitted Customer."""
    raise frappe.PermissionError("Select the Customer in the conversation cart review.")
