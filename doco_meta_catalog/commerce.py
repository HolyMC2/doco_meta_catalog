"""Catalog adapter for CRM's conversation/outbox and reviewed order broker.

No provider HTTP or delivery retry belongs here. Catalog and account bindings
are read under locks at queue and dispatch; a default outgoing account is never
an authority for a customer's existing conversation.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, flt

from doco_meta_catalog import sync

SETTINGS = "Meta Catalog Settings"
KINDS = {"catalog_message", "product", "product_list"}


def _settings():
    # Single documents have no normal row to lock. Use the current locking
    # values rather than a cached document/RR snapshot at the dispatch boundary.
    rows = frappe.db.sql("SELECT field,value FROM tabSingles WHERE doctype=%s FOR UPDATE", SETTINGS)
    settings = frappe._dict(rows)
    settings.category_map = frappe.get_all(
        "Meta Catalog Category Map", filters={"parent": SETTINGS, "parenttype": SETTINGS},
        fields=["item_group", "condition", "google_product_category", "exclude", "visibility"],
        order_by="idx asc", for_update=True,
    )
    return settings


def _binding(account_name, *, enabled=True):
    from doco_meta_catalog.utils import assert_outbound_allowed

    if enabled:
        assert_outbound_allowed()
    settings = _settings()
    if not settings.catalog_id or not settings.whatsapp_account:
        raise ValueError("catalog_not_configured")
    if enabled and not cint(settings.enabled):
        raise ValueError("catalog_disabled")
    if settings.whatsapp_account != account_name:
        raise ValueError("wrong_account")
    if not re.fullmatch(r"[0-9]{1,40}", settings.catalog_id):
        raise ValueError("catalog_not_configured")
    return settings


def _fail(reason):
    labels = {
        "catalog_not_configured": _("Configure a catalog and its exact WhatsApp account first."),
        "catalog_disabled": _("Catalog commerce is disabled in Meta Catalog Settings."),
        "wrong_account": _("This catalog belongs to a different WhatsApp account."),
        "catalog_item_unavailable": _("A selected product is no longer available in the published catalog. Refresh the selection."),
    }
    frappe.throw(labels.get(reason, _("Catalog configuration changed. Refresh and review the selection.")))


def _require_binding(account_name):
    try:
        return _binding(account_name)
    except ValueError as error:
        _fail(str(error))


def _product_snapshot(codes, settings, *, user=None, require_all=True):
    """Lock source metadata and reuse the publisher's actual eligibility/payload."""
    actor = user or frappe.session.user
    for code in sorted(set(codes)):
        if not isinstance(code, str) or not code or len(code) > 140 or code != code.strip():
            raise ValueError("catalog_item_unavailable")
        item = frappe.get_doc("Item", code, for_update=True)
        if not frappe.has_permission("Item", "read", doc=item, user=actor):
            raise frappe.PermissionError
        if item.variant_of:
            frappe.get_doc("Item", item.variant_of, for_update=True)
    if codes:
        # Lock the exact rows used by canonical publication before re-reading
        # its derived values. Insert/update of an item's price is serialized.
        price = frappe.qb.DocType("Item Price")
        (frappe.qb.from_(price).select(price.name).where(price.item_code.isin(codes)).for_update()).run()
        from doco.docoutils.storefront._pricing import _lock_bins

        _lock_bins(codes)
    payloads, _skipped = sync._build_payloads(codes, settings, lock=True)
    products = {row["data"]["id"]: row["data"] for row in payloads}
    products = {code: product for code, product in products.items() if product.get("visibility") == "published"}
    if require_all and any(code not in products for code in codes):
        raise ValueError("catalog_item_unavailable")
    return [products[code] for code in codes if code in products]


def _revision(settings, account, products, request_fingerprint):
    # Existing intent fingerprint format is unchanged: source_action already
    # belongs to IMMUTABLE. This new namespace freezes the binding and product
    # snapshot without invalidating any pre-existing text/template intent.
    values = {
        "version": 1,
        "account": [account.name, account.app_id, account.business_id],
        "catalog": settings.catalog_id,
        "settings": {key: settings.get(key) for key in (
            "enabled", "whatsapp_account", "default_currency", "price_markup_percent", "default_visibility",
            "category_map", "variant_group_attribute", "variant_color_attribute",
        )},
        "products": products,
    }
    from crm.api.catalog_commerce import compact_digest
    from crm.api.outbox_policy import account_revision

    return "cat1:" + compact_digest(account_revision(account)) + ":" + compact_digest(values) + ":" + request_fingerprint


def _account(conversation):
    account = frappe.db.get_value(
        "WhatsApp Account", conversation.account_record, ["name", "phone_id", "app_id", "business_id"],
        as_dict=True, for_update=True,
    )
    if not account or account.phone_id != conversation.account_id:
        _fail("wrong_account")
    return account


def get_context(conversation):
    from doco_meta_catalog import connection, orders

    result = {
        "available": False, "reason_code": "", "capabilities": dict.fromkeys(sorted(KINDS), False),
        "carts": orders.list_carts(conversation), "company": None, "warehouse": None,
        "facebook": {
            "catalog_visibility": "unverified", "shop": "unverified", "marketplace_listing": "unverified",
            "checkout": "external", "management_url": "https://business.facebook.com/commerce/",
        },
    }
    try:
        settings = _binding(conversation.account_record)
    except ValueError as error:
        result["reason_code"] = str(error)
        return result
    _account(conversation)
    result.update({"available": True, "catalog_id": settings.catalog_id,
                   "account_name": conversation.account_record, "capabilities": dict.fromkeys(sorted(KINDS), True),
                   "connection": connection.latest(settings)})
    return result


def get_products(conversation, query="", offset=0):
    settings = _require_binding(conversation.account_record)
    if not isinstance(query, str) or len(query) > 140:
        frappe.throw(_("Search using at most 140 characters."))
    if isinstance(offset, bool) or not str(offset).isdigit() or not 0 <= int(offset) <= 10000:
        frappe.throw(_("Invalid catalog page."))
    filters = {"publish_on_web": 1, "disabled": 0, "has_variants": 0}
    search = [["name", "like", "%" + query + "%"], ["item_name", "like", "%" + query + "%"]] if query else None
    rows = frappe.get_list("Item", filters=filters, or_filters=search, fields=["name"],
                           order_by="item_name asc, name asc", start=int(offset), page_length=26)
    result = []
    products = _product_snapshot([row.name for row in rows[:25]], settings, require_all=False)
    for product in products:
        amount, currency = product.get("sale_price", product["price"]).rsplit(" ", 1)
        result.append({"item_code": product["id"], "item_name": product["title"], "rate": flt(amount),
                       "currency": currency, "image_url": product["image_link"], "stock": product["availability"]})
    return {"items": result, "has_more": len(rows) > 25, "next_offset": int(offset) + 25}


def freeze(selection, conversation):
    from crm.api.catalog_commerce import catalog_request_fingerprint

    request_fingerprint = catalog_request_fingerprint(selection)
    account = _account(conversation)
    settings = _require_binding(account.name)
    if not isinstance(selection, dict) or set(selection) != {"kind", "products", "body", "header", "footer"}:
        frappe.throw(_("Invalid catalog selection."))
    kind, codes = selection["kind"], selection["products"]
    bounds = {"catalog_message": (0, 1), "product": (1, 1), "product_list": (1, 30)}
    if kind not in bounds or not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
        frappe.throw(_("Invalid catalog selection."))
    minimum, maximum = bounds[kind]
    if not minimum <= len(codes) <= maximum or len(set(codes)) != len(codes):
        frappe.throw(_("Select the required number of distinct products."))
    try:
        products = _product_snapshot(codes, settings)
    except ValueError as error:
        _fail(str(error))
    interactive = {"type": kind, "body": {"text": selection["body"]}}
    if selection["footer"]:
        interactive["footer"] = {"text": selection["footer"]}
    if kind == "catalog_message":
        interactive["action"] = {"name": "catalog_message"}
        if codes:
            interactive["action"]["parameters"] = {"thumbnail_product_retailer_id": codes[0]}
    elif kind == "product":
        interactive["action"] = {"catalog_id": settings.catalog_id, "product_retailer_id": codes[0]}
    else:
        interactive["header"] = {"type": "text", "text": selection["header"]}
        interactive["action"] = {"catalog_id": settings.catalog_id, "sections": [
            {"product_items": [{"product_retailer_id": code} for code in codes]}
        ]}
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": conversation.peer_id,
               "type": "interactive", "interactive": interactive}
    from frappe_whatsapp.native_outbox import validate_payload

    try:
        frozen = validate_payload(payload, account_id=conversation.account_id, peer_id=conversation.peer_id).decode()
    except ValueError:
        frappe.throw(_("Check the catalog message text and product selection."))
    return frozen, ("WhatsApp Account", account.name, _revision(settings, account, products, request_fingerprint))


def dispatch_reason(intent, account, payload):
    from crm.api.catalog_commerce import SOURCE_PATTERN

    source = re.fullmatch(SOURCE_PATTERN, intent.source_action or "")
    if not source:
        return "catalog_producer_required"
    try:
        settings = _binding(account.name)
        interactive = payload["interactive"]
        action = interactive["action"]
        if interactive["type"] == "catalog_message":
            code = action.get("parameters", {}).get("thumbnail_product_retailer_id")
            codes = [code] if code else []
        else:
            if action["catalog_id"] != settings.catalog_id:
                return "catalog_configuration_changed"
            codes = [action["product_retailer_id"]] if interactive["type"] == "product" else [
                row["product_retailer_id"] for section in action["sections"] for row in section["product_items"]
            ]
        products = _product_snapshot(codes, settings, user=intent.actor_user)
        if intent.source_action != _revision(settings, account, products, source[3]):
            return "catalog_configuration_changed"
    except (KeyError, TypeError, ValueError, frappe.DoesNotExistError):
        return "catalog_item_unavailable"
    return None


def get_cart(name):
    from doco_meta_catalog.orders import get_cart as handler
    return handler(name)


def review_cart(name, customer, company, warehouse, deal=None):
    from doco_meta_catalog.orders import review_cart as handler
    return handler(name, customer, company, warehouse, deal=deal)


def create_order(name, review_token, request_id, customer, company, warehouse, deal=None):
    from doco_meta_catalog.orders import create_order as handler
    return handler(name, review_token, request_id, customer, company, warehouse, deal=deal)
