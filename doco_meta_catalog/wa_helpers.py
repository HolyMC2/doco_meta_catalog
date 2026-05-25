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

import frappe
import requests


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
        frappe.log_error(
            title=f"WA product message HTTP {r.status_code}",
            message=f"URL: {url}\nPayload: {json.dumps(payload)[:1500]}\nResponse: {r.text[:1500]}",
        )
        r.raise_for_status()
    return r.json()


def _catalog_id():
    return frappe.db.get_single_value("Meta Catalog Settings", "catalog_id")


@frappe.whitelist()
def send_catalog_message(to: str, body: str = "Mira nuestro catálogo:", footer: str | None = None):
    """Single-button message that opens the connected catalog inside WhatsApp."""
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


# ---------------- inbound cart / order ----------------


def handle_order_message(message: dict, whatsapp_account_name: str) -> str | None:
    """Called from frappe_whatsapp webhook router when message_type == 'order'.

    Meta `order` payload shape:
        {
            "catalog_id": "...",
            "text": "optional buyer note",
            "product_items": [
                {"product_retailer_id": "IT-XYZ", "quantity": 1, "item_price": 19900, "currency": "MXN"},
                ...
            ]
        }
    Currency: minor units (19900 = 199.00 MXN).

    Returns Sales Order docname, or None if dropping silently.
    """
    order = message.get("order") or {}
    items = order.get("product_items") or []
    if not items:
        return None

    from_number = message.get("from")
    contact_name = None
    try:
        from crm.integrations.api import get_contact_by_phone_number
        c = get_contact_by_phone_number(from_number) or {}
        contact_name = c.get("name")
    except Exception:
        pass

    customer = _find_or_create_customer(from_number, contact_name)
    so = frappe.new_doc("Sales Order")
    so.customer = customer
    so.delivery_date = frappe.utils.add_days(frappe.utils.today(), 1)
    so.transaction_date = frappe.utils.today()
    so.docstatus = 0  # draft
    so.po_no = f"WA-{message.get('id','')[:24]}"

    for pi in items:
        if not frappe.db.exists("Item", pi.get("product_retailer_id")):
            continue
        so.append(
            "items",
            {
                "item_code": pi["product_retailer_id"],
                "qty": pi.get("quantity") or 1,
                "rate": (pi.get("item_price") or 0) / 100.0,  # minor -> major units
                "delivery_date": so.delivery_date,
            },
        )
    if not so.items:
        return None
    so.insert(ignore_permissions=True)
    frappe.db.commit()

    note = order.get("text")
    if note:
        so.add_comment("Comment", text=f"Buyer note via WhatsApp: {note}")
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
