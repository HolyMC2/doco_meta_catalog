"""WhatsApp Cloud API helpers for catalog messages + cart-order ingestion.

Outbound:
    send_catalog_message(to, body)                       single tappable button -> entire catalog
    send_product_message(to, retailer_id, body, footer)  one product card
    send_product_list(to, sections, body, header, footer) multi-section product list (up to 30 items)

Inbound (called from a router in webhook.py):
    handle_order_message(msg, account) -> "Sales Order" name  (creates draft SO from cart payload)

All outbound goes through the user-selected WhatsApp Account (defaults to is_default_outgoing).
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
import requests

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
    url = f"{account.url.rstrip('/')}/{account.version}/{account.phone_id}/messages"
    tok = account.get_password("token", raise_exception=False)
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if r.status_code >= 400:
        try:
            err = (r.json() or {}).get("error", {})
            meta_err = f"{err.get('code')}/{err.get('error_subcode')}: {err.get('message')}"
        except Exception:
            meta_err = f"HTTP {r.status_code}"
        safe = {k: v for k, v in payload.items() if k != "to"}  # buyer phone (`to`) is PII — redact
        frappe.log_error(
            title=f"WA send HTTP {r.status_code}",
            message=f"meta_error={meta_err}\npayload(no recipient)={json.dumps(safe)[:1000]}",
        )
        r.raise_for_status()
    return r.json()


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


# ---------------- inbound cart / order (security-critical) ----------------
#
# REACHABILITY CONTRACT: this builds a Sales Order + Customer with ignore_permissions, so it must
# run ONLY behind the HMAC-verified webhook (doco_meta_catalog.webhook.webhook), which proves the
# payload came from Meta. It is intentionally NOT @frappe.whitelist and asserts signature_verified
# before any write. The cart is composed by the buyer (attacker-influenceable), so EVERY field is
# re-derived server-side: prices from Item Price (NEVER the payload `item_price`), the sellable set
# from the catalog gate (publish_on_web), bounded qty/line counts, idempotency on the WhatsApp
# message id, and the SO stays a DRAFT (no GL/stock impact until a human submits).


def handle_order_message(
    message: dict,
    whatsapp_account_name: str | None = None,
    signature_verified: bool = False,
) -> str | None:
    """Build a DRAFT Sales Order from a Meta `order` (WhatsApp cart) message. Returns the SO name,
    or None when nothing sellable / already ingested / malformed. Raises if the caller did not
    verify the webhook signature.

    Meta `order` payload: {"text": "...", "product_items": [{"product_retailer_id", "quantity",
    "item_price", "currency"}, ...]}. `item_price` is buyer-supplied and is IGNORED — we re-price.
    """
    if not signature_verified:
        # defense in depth: never construct an order from an unverified (possibly forged) payload
        frappe.throw("handle_order_message requires a signature-verified webhook")

    order = message.get("order") or {}
    items = order.get("product_items") or []
    if not items:
        return None

    from_number = _canon_phone((message.get("from") or "").strip())
    if not _E164.match(from_number):
        return None  # malformed / forged sender

    msg_id = str(message.get("id") or "")[:120]
    if not msg_id:
        return None  # no WhatsApp message id → cannot dedup; real Meta orders always carry one
    # atomic idempotency: CLAIM the message id via a UNIQUE index. Two concurrent Meta retries
    # race here — exactly one insert wins; the loser sees the duplicate and stops. Replaces a
    # db.exists() TOCTOU that both racers could pass before either committed.
    try:
        frappe.get_doc({"doctype": "Meta Order Log", "wa_msg_id": msg_id}).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        if frappe.db.exists("Meta Order Log", {"wa_msg_id": msg_id}):
            return None  # already ingested
        raise
    po_no = f"WA-{msg_id}"

    items = items[: _sf._MAX_LINES]  # bound line count before any per-item work

    # sellable gate + server-side re-pricing — the buyer's `item_price` is NEVER trusted
    codes = [pi.get("product_retailer_id") for pi in items if pi.get("product_retailer_id")]
    eligible = {it["name"] for it in sync._eligible_leaves(codes)}  # publish_on_web, leaf
    prices = _sf._prices(list(eligible), _sf._selling_price_list())

    # accumulate qty per SKU so duplicate cart lines can't multiply the per-SKU cap
    agg: dict[str, int] = {}
    for pi in items:
        code = pi.get("product_retailer_id")
        if code not in eligible or not prices.get(code):
            continue  # not a published / priced / sellable item (forged, unknown, or unpriced)
        try:
            qty = int(pi.get("quantity") or 1)
        except (TypeError, ValueError, OverflowError):
            continue  # NaN / Infinity / garbage qty → skip this line, not the whole order
        if qty < 1:
            continue
        agg[code] = agg.get(code, 0) + qty

    if not agg:
        return None  # nothing sellable → create NO customer and NO order (no spam)

    lines = [
        {"item_code": code, "qty": min(qty, _sf._MAX_QTY), "rate": flt(prices[code])}
        for code, qty in agg.items()
    ]

    # resolve/create the customer only now (avoids empty-order Customer spam)
    contact_name = None
    try:
        from crm.integrations.api import get_contact_by_phone_number
        contact_name = (get_contact_by_phone_number(from_number) or {}).get("name")
    except Exception:
        pass
    customer = _find_or_create_customer(from_number, contact_name)

    delivery = frappe.utils.add_days(frappe.utils.today(), 1)
    so = frappe.new_doc("Sales Order")
    so.customer = customer
    so.transaction_date = frappe.utils.today()
    so.delivery_date = delivery
    so.docstatus = 0  # DRAFT — human reviews + submits; no GL/stock impact unattended
    if po_no:
        so.po_no = po_no
    for ln in lines:
        so.append("items", {**ln, "delivery_date": delivery})
    so.insert(ignore_permissions=True)
    frappe.db.commit()
    frappe.db.set_value("Meta Order Log", {"wa_msg_id": msg_id}, "sales_order", so.name, update_modified=False)

    note = (order.get("text") or "").strip()
    if note:
        so.add_comment("Comment", text=f"Nota del comprador (WhatsApp): {escape_html(note)[:500]}")
    return so.name


def _find_or_create_customer(phone: str, contact_name: str | None) -> str:
    # 1. Try existing Customer with this mobile_no
    cust = frappe.db.get_value("Customer", {"mobile_no": phone}, "name")
    if cust:
        return cust
    # 2. Try via Contact link
    if contact_name:
        for link in frappe.get_all(
            "Dynamic Link",
            filters={"parenttype": "Contact", "parent": contact_name, "link_doctype": "Customer"},
            fields=["link_name"],
        ):
            return link["link_name"]
    # 3. Create a fresh walk-in style Customer
    c = frappe.new_doc("Customer")
    c.customer_name = (contact_name or phone)[:140]
    c.customer_type = "Individual"
    c.customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"
    c.territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"
    c.mobile_no = phone
    c.insert(ignore_permissions=True)
    return c.name
