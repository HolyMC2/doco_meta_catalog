"""Receipt-backed cart intake and explicit, conversation-authorized ERP draft orders.

The caller owns every transaction. There are no commits, sends, enqueues, automatic
Customer matches or privilege changes here. Meta Order Log is the sole intake ledger.
"""
from __future__ import annotations

import hmac
import json
import secrets
from collections import defaultdict
from contextlib import contextmanager
from decimal import Decimal
from urllib.parse import quote

import frappe
from frappe.utils import add_days, get_datetime, now_datetime, today

from doco_meta_catalog.orders_contract import (
    BLOCKERS, CartError, canonical, decimal, digest, evaluate_lines, money, receipt_order,
    requested_lines, require,
)

DOCTYPE = "Meta Order Log"
_SERVICE_TOKEN = object()
IMMUTABLE = ("intake_key", "identity_state", "receipt", "provider", "app_id", "account_id",
             "business_id", "whatsapp_account", "peer_id", "conversation", "wa_message", "wa_msg_id",
             "catalog_id", "raw_order", "raw_order_hash")


def _save(doc, *, new=False):
    doc.flags.meta_order_service = _SERVICE_TOKEN
    return doc.insert() if new else doc.save()


def _ready():
    from frappe import share
    if getattr(share, "FILTER_SHARED_DOCUMENTS_VERSION", 0) != 1:
        raise frappe.PermissionError("Cart review requires the supported shared-document scope base.")


def check_request_compatibility():
    if frappe.db.has_column(DOCTYPE, "intake_key"):
        _ready()


def intake(message):
    """Only a currently claimed durable receipt can create immutable cart evidence."""
    name = frappe.flags.get("meta_webhook_receipt")
    if not name:
        return {"state": "Ignored", "reason_code": "receipt_transaction_required"}
    require(not getattr(frappe, "request", None), "receipt_transaction_required")
    _ready()
    row = frappe.db.get_value("Meta Webhook Receipt", name,
        ["name", "event_key", "provider", "app_id", "account_id", "event_type", "event_id",
         "payload", "payload_hash", "state", "attempts", "lease_until"], as_dict=True, for_update=True)
    require(row and row.state == "Processing" and type(row.attempts) is int and row.attempts >= 1
            and row.lease_until and get_datetime(row.lease_until) > now_datetime(), "receipt_claim_required")
    account = frappe.db.get_value("WhatsApp Account", message.whatsapp_account,
        ["name", "phone_id", "app_id", "business_id", "status", "mode"], as_dict=True)
    require(bool(account))
    order, peer, business = receipt_order(row, message.as_dict(), account)
    existing = frappe.db.get_value(DOCTYPE, {"intake_key": name}, "name")
    if existing:
        old = frappe.get_doc(DOCTYPE, existing)
        require(old.raw_order_hash == digest(order) and old.account_id == row.account_id
                and old.peer_id == peer, "intake_identity_conflict")
        return {"name": existing, "state": old.state, "replayed": True}
    if "crm" not in frappe.get_installed_apps():
        return {"state": "Ignored", "reason_code": "conversation_unavailable"}
    from crm.api import conversations
    conversation_name = conversations.conversation_key("WhatsApp", row.account_id, peer)
    with conversations.conversation_fence(conversation_name):
        current_account = frappe.db.get_value("WhatsApp Account", account.name,
            ["name", "phone_id", "app_id", "business_id", "status", "mode"], as_dict=True, for_update=True)
        receipt_order(row, message.as_dict(), current_account)
        return _insert_intake(message, row, account, order, peer, business)


def _insert_intake(message, row, account, order, peer, business):
    from crm.api import conversations
    conversation = conversations.get_or_create("WhatsApp", row.account_id, peer)
    doc = frappe.get_doc({"doctype": DOCTYPE, "intake_key": row.name, "identity_state": "verified",
        "state": "Needs Review", "receipt": row.name, "provider": "WhatsApp", "app_id": row.app_id,
        "account_id": row.account_id, "business_id": business, "whatsapp_account": account.name,
        "peer_id": peer, "conversation": conversation.name, "wa_message": message.name,
        "wa_msg_id": message.message_id,
        "catalog_id": order.get("catalog_id") if isinstance(order.get("catalog_id"), str) and len(order["catalog_id"]) <= 140 else None,
        "raw_order": canonical(order), "raw_order_hash": digest(order)})
    _save(doc, new=True)
    return {"name": doc.name, "state": doc.state, "replayed": False}


def _load(name, *, lock=False):
    if not isinstance(name, str) or not name or len(name) > 140:
        raise frappe.PermissionError("Invalid cart identity.")
    return frappe.get_doc(DOCTYPE, name, for_update=lock)


def _conversation(doc, *, write=False):
    _ready()
    if doc.identity_state != "verified":
        raise frappe.PermissionError("Historical cart account identity is unknown.")
    from crm.api import conversations
    expected = conversations.conversation_key("WhatsApp", doc.account_id, doc.peer_id)
    if doc.provider != "WhatsApp" or expected != doc.conversation:
        raise frappe.PermissionError("Invalid cart conversation identity.")
    conversation = conversations._load(expected)
    if conversation.account_record != doc.whatsapp_account:
        raise frappe.PermissionError("Cart account changed.")
    conversations._authorize(conversation, write=write)
    return conversation


@contextmanager
def _locked_cart(name):
    from crm.api import conversations
    first = _load(name)
    _conversation(first, write=True)
    with conversations.conversation_fence(first.conversation):
        doc = _load(name, lock=True)
        _conversation(doc, write=True)
        yield doc


def _order_link(name):
    if not name:
        return None
    if not frappe.has_permission("Sales Order", "read", doc=name):
        return None
    return "/app/sales-order/" + quote(name, safe="")


def _public(doc, *, legacy=False):
    order = {} if legacy else json.loads(doc.raw_order)
    url = _order_link(doc.sales_order)
    return {"name": doc.name, "state": doc.state, "creation": doc.creation,
        "catalog_id": None if legacy else order.get("catalog_id"), "buyer_note": order.get("text", ""),
        "lines": requested_lines(order), "line_count": len(order.get("product_items", [])),
        "sales_order": doc.sales_order if url else None, "sales_order_url": url,
        "reason_code": "account_unknown" if legacy else doc.reason_code}


def list_carts(conversationDoc):
    from crm.api import conversations
    # Reload exact canonical identity; never trust a caller-provided account/peer.
    current = conversations._load(conversationDoc.name)
    conversations._authorize(current)
    _ready()
    names = frappe.get_all(DOCTYPE, filters={"conversation": current.name, "identity_state": "verified"},
        pluck="name", order_by="creation desc, name desc", limit_page_length=0)
    result = []
    for name in names:
        doc = _load(name)
        _conversation(doc)
        public = _public(doc)
        result.append({key: public[key] for key in ("name", "state", "creation", "catalog_id", "line_count",
                                                   "sales_order", "sales_order_url", "reason_code")})
    return result


def get_cart(name):
    doc = _load(name)
    if doc.identity_state != "verified":
        frappe.only_for("System Manager")
        return _public(doc, legacy=True)
    _conversation(doc)
    return _public(doc)


def _selection(customer, company, warehouse, deal):
    selected = {}
    for doctype, name in (("Customer", customer), ("Company", company), ("Warehouse", warehouse)):
        if not isinstance(name, str) or not name or len(name) > 140:
            frappe.throw("Select a Customer, Company and Warehouse.")
        doc = frappe.get_doc(doctype, name, for_update=True)
        doc.check_permission("read")
        selected[doctype.lower()] = doc
    if selected["customer"].get("disabled") or selected["warehouse"].get("disabled"):
        frappe.throw("Choose an active Customer and Warehouse.")
    if selected["warehouse"].company != company or selected["warehouse"].is_group:
        frappe.throw("Choose a leaf Warehouse belonging to the selected Company.")
    if not frappe.has_permission("Sales Order", "create"):
        raise frappe.PermissionError("Sales Order creation permission is required.")
    if deal:
        linked = frappe.get_doc("CRM Deal", deal, for_update=True)
        linked.check_permission("read")
        if linked.get("sales_company") and linked.sales_company != company:
            frappe.throw("The Deal belongs to a different Company.")
        if linked.get("customer") and linked.customer != customer:
            frappe.throw("The Deal belongs to a different Customer.")
    return {"customer": customer, "company": company, "warehouse": warehouse, "deal": deal or None}


def _catalog_snapshot(doc, selection, *, lock=False):
    """Lossless requested lines, explicit price currency and unique applicable rows."""
    from doco.docoutils.storefront import _pricing as stock_api
    from doco_meta_catalog import sync
    from doco_meta_catalog.commerce import _settings
    settings = _settings() if lock else frappe.get_doc("Meta Catalog Settings")

    def single(doctype, field):
        if not lock:
            return frappe.db.get_single_value(doctype, field)
        rows = frappe.db.sql("SELECT value FROM tabSingles WHERE doctype=%s AND field=%s FOR UPDATE", (doctype, field))
        return rows[0][0] if rows else None
    order = json.loads(doc.raw_order)
    codes = sorted({row["item_code"] for row in requested_lines(order)
                    if isinstance(row["item_code"], str) and 0 < len(row["item_code"]) <= 140})
    currency = settings.default_currency
    price_list = single("Selling Settings", "selling_price_list")
    binding_ok = settings.whatsapp_account == doc.whatsapp_account and bool(settings.catalog_id and currency)
    price_list_ok = price_list and frappe.db.get_value("Price List", price_list, ["enabled", "selling", "currency"], as_dict=True, for_update=lock)
    issues = []
    if (int(single("Stock Settings", "auto_insert_price_list_rate_if_missing") or 0)
            and int(single("Stock Settings", "update_existing_price_list_rate") or 0)):
        # Native preview must not mutate Item Price as a side effect.
        issues.append("erp_review_required")
    if not binding_ok or not price_list_ok or not price_list_ok.enabled or not price_list_ok.selling:
        issues.append("catalog_unavailable")
    account = frappe.db.get_value("WhatsApp Account", doc.whatsapp_account,
        ["app_id", "business_id", "phone_id"], as_dict=True, for_update=lock)
    if not account or account.app_id != doc.app_id or account.business_id != doc.business_id or account.phone_id != doc.account_id:
        issues.append("account_unknown")
    if price_list_ok and price_list_ok.currency != currency:
        issues.append("currency_mismatch")
    markup = decimal(settings.price_markup_percent or 0)
    if markup is None or markup < 0 or markup > 10000:
        issues.append("catalog_unavailable")
        markup = Decimal(0)
    if lock and codes:
        frappe.db.sql("SELECT name FROM `tabItem` WHERE name IN %(codes)s ORDER BY name FOR UPDATE", {"codes": codes})
        frappe.db.sql("SELECT name FROM `tabItem Price` WHERE item_code IN %(codes)s ORDER BY name FOR UPDATE", {"codes": codes})
    records = frappe.get_all("Item", filters={"name": ["in", codes]},
        fields=["name", "item_name", "stock_uom", "is_stock_item", "is_sales_item", "item_group"], for_update=lock) if codes else []
    for row in records:
        if not frappe.has_permission("Item", "read", doc=row.name):
            raise frappe.PermissionError("Item read permission is required for cart review.")
    groups = sync._group_overrides(settings)
    eligible = {row["name"] for row in sync._eligible_leaves(codes, lock=lock)
                if not groups.get(row["item_group"], {}).get("exclude")
                and groups.get(row["item_group"], {}).get("visibility") != "staging"}
    items = {row.name: {"item_name": row.item_name, "published": row.name in eligible and bool(row.is_sales_item),
        "stock_tracked": bool(row.is_stock_item), "stock_uom": row.stock_uom,
        "whole_number": bool(frappe.db.get_value("UOM", row.stock_uom, "must_be_whole_number"))} for row in records}
    prices = {}
    rows = frappe.get_all("Item Price", filters={"item_code": ["in", codes], "selling": 1, "price_list": price_list},
        fields=["name", "item_code", "price_list_rate", "currency", "uom", "customer", "supplier", "batch_no",
                "packing_unit", "valid_from", "valid_upto"], for_update=lock, order_by="name asc", limit_page_length=0) if codes and price_list else []
    by_code = defaultdict(list)
    for row in rows:
        if row.valid_from and str(row.valid_from) > today() or row.valid_upto and str(row.valid_upto) < today():
            continue
        if row.customer and row.customer != selection["customer"]:
            continue
        by_code[row.item_code].append(row)
    for code, candidates in by_code.items():
        if len(candidates) != 1 or any(row.supplier or row.batch_no or (row.uom and row.uom != items[code]["stock_uom"])
                                      or decimal(row.packing_unit or 1) != 1 for row in candidates):
            prices[code] = {"issue": "ambiguous_price"}
            continue
        row = candidates[0]
        if row.currency != currency:
            prices[code] = {"issue": "currency_mismatch"}
            continue
        base = decimal(row.price_list_rate)
        prices[code] = {"rate": money(base * (1 + markup / 100)) if base is not None else None,
                       "base_rate": float(base) if base is not None else None,
                       "source": row.name}
    bundles = stock_api._product_bundles(codes) if not lock else _locked_bundles(codes)
    all_codes = sorted(set(codes) | {part["item_code"] for parts in bundles.values() for part in parts})
    if lock:
        stock_api._lock_bins(all_codes, [selection["warehouse"]])
    available = stock_api._avail_qty(all_codes, [selection["warehouse"]], lock=lock)
    physical = dict(available)
    nonstock = stock_api._nonstock(all_codes, lock=lock)
    demand = defaultdict(Decimal)
    for row in requested_lines(order):
        qty = decimal(row["requested_quantity"])
        code = row["item_code"]
        if not isinstance(code, str) or qty is None or qty <= 0:
            continue
        for part in bundles.get(code, [{"item_code": code, "qty": 1}]):
            if part["item_code"] not in nonstock:
                demand[part["item_code"]] += qty * Decimal(str(part["qty"]))
    for code in items:
        if code in bundles:
            stock_parts = [part for part in bundles[code] if part["item_code"] not in nonstock]
            items[code]["stock_tracked"] = bool(stock_parts)
            available[code] = min((float(Decimal(str(available.get(part["item_code"], 0))) / Decimal(str(part["qty"])))
                                   for part in stock_parts), default=None)
        parts = bundles.get(code, [{"item_code": code, "qty": 1}])
        if any(Decimal(str(physical.get(part["item_code"], 0) or 0)) < demand[part["item_code"]]
               for part in parts if part["item_code"] not in nonstock):
            available[code] = 0
    result = evaluate_lines(order, settings.catalog_id, currency, items, prices, available)
    result["issues"] = list(dict.fromkeys(issues + result["issues"]))
    result.update(currency=currency, price_list=price_list, markup_percent=float(markup),
                  base_rates={code: row.get("base_rate") for code, row in prices.items()},
                  pricing_rules=[{"name": row.name, "modified": str(row.modified)} for row in
                      frappe.get_all("Pricing Rule", fields=["name", "modified"], order_by="name asc", for_update=lock, limit_page_length=0)],
                  can_create=not bool(BLOCKERS.intersection(result["issues"])))
    return result


def _locked_bundles(codes):
    result = {}
    if codes:
        for row in frappe.get_all("Product Bundle", filters={"new_item_code": ["in", codes], "disabled": 0},
                fields=["name", "new_item_code"], order_by="name asc", for_update=True):
            result[row.new_item_code] = frappe.get_all("Product Bundle Item", filters={"parent": row.name},
                fields=["item_code", "qty"], order_by="idx asc", for_update=True)
    return result


def _new_order(selection, snapshot):
    so = frappe.new_doc("Sales Order")
    so.update({"customer": selection["customer"], "company": selection["company"],
        "set_warehouse": selection["warehouse"], "transaction_date": today(), "delivery_date": add_days(today(), 1),
        "order_type": "Sales", "currency": snapshot["currency"], "selling_price_list": snapshot["price_list"],
        "ignore_pricing_rule": 0})
    if selection["deal"] and so.meta.has_field("crm_deal"):
        so.crm_deal = selection["deal"]
    for line in snapshot["lines"]:
        so.append("items", {"item_code": line["item_code"], "qty": line["quantity"],
            "price_list_rate": snapshot["base_rates"][line["item_code"]],
            "margin_type": "Percentage", "margin_rate_or_amount": snapshot["markup_percent"],
            "warehouse": selection["warehouse"], "delivery_date": so.delivery_date})
    so.run_method("set_missing_values")
    so.run_method("calculate_taxes_and_totals")
    return so


def _financial(so):
    fields = ("currency", "conversion_rate", "total", "net_total", "total_taxes_and_charges", "grand_total",
              "rounded_total", "base_grand_total", "discount_amount", "additional_discount_percentage")
    item_fields = ("item_code", "qty", "uom", "conversion_factor", "rate", "net_rate", "amount", "net_amount", "warehouse")
    tax_fields = ("charge_type", "account_head", "rate", "tax_amount", "total", "included_in_print_rate")
    def values(row, wanted):
        return {field: row.get(field) for field in wanted}
    return {**values(so, fields), "items": [values(row, item_fields) for row in so.items],
            "taxes": [values(row, tax_fields) for row in so.taxes]}


def _review(doc, selection, *, lock=False):
    snapshot = _catalog_snapshot(doc, selection, lock=lock)
    financial = None
    if snapshot["can_create"]:
        so = _new_order(selection, snapshot)
        financial = _financial(so)
        if len(so.items) != len(snapshot["lines"]) or any(
            row.item_code != line["item_code"] or decimal(row.qty) != decimal(line["quantity"])
            for row, line in zip(so.items, snapshot["lines"])):
            snapshot["issues"].append("erp_review_required")
            snapshot["can_create"] = False
        else:
            for row, line in zip(so.items, snapshot["lines"]):
                line["rate"] = row.rate
                line["issues"] = [issue for issue in line["issues"] if issue != "price_changed"]
                requested = decimal(line["requested_price"])
                if requested is None or money(requested) != money(row.rate):
                    line["issues"].append("price_changed")
            snapshot["issues"] = list(dict.fromkeys(
                [issue for issue in snapshot["issues"] if issue != "price_changed"]
                + [issue for line in snapshot["lines"] for issue in line["issues"]]))
        snapshot["total"] = money(so.grand_total)
    frozen = {"selection": selection, "quote": snapshot, "financial": financial, "raw_order_hash": doc.raw_order_hash}
    return snapshot, frozen


def review_cart(name, customer, company, warehouse, deal=None):
    with _locked_cart(name) as doc:
        if doc.sales_order:
            frappe.throw("This cart already has a Sales Order.")
        selected = _selection(customer, company, warehouse, deal)
        snapshot, frozen = _review(doc, selected)
        token = secrets.token_hex(32)
        doc.update({**selected, "review_token": token, "review_snapshot": canonical(frozen),
                    "reviewed_by": frappe.session.user, "reviewed_at": now_datetime(),
                    "state": "Reviewed" if snapshot["can_create"] else "Needs Review",
                    "reason_code": next((issue for issue in snapshot["issues"] if issue in BLOCKERS), None)})
        _save(doc)
        return {"review_token": token, **{key: snapshot[key] for key in
            ("currency", "price_list", "lines", "issues", "total", "can_create", "markup_percent")}}


def create_order(name, review_token, request_id, customer, company, warehouse, deal=None):
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100 or any(ord(c) < 32 for c in request_id):
        frappe.throw("A stable create request ID is required.")
    with _locked_cart(name) as doc:
        selected = _selection(customer, company, warehouse, deal)
        if doc.sales_order:
            if any(doc.get(field) != value for field, value in selected.items()):
                frappe.throw("This cart was ordered with a different reviewed selection.")
            url = _order_link(doc.sales_order)
            if not url:
                raise frappe.PermissionError("Sales Order read permission is required.")
            return {"sales_order": doc.sales_order, "sales_order_url": url, "replayed": True}
        if (doc.state != "Reviewed" or doc.reviewed_by != frappe.session.user or not isinstance(review_token, str)
                or not hmac.compare_digest(review_token, doc.review_token or "")
                or not doc.reviewed_at or (now_datetime() - get_datetime(doc.reviewed_at)).total_seconds() > 900):
            frappe.throw("Review this cart again before creating the order.", frappe.TimestampMismatchError)
        snapshot, frozen = _review(doc, selected, lock=True)
        if not snapshot["can_create"] or digest(frozen) != digest(json.loads(doc.review_snapshot)):
            frappe.throw("Prices, stock or order details changed. Review this cart again.", frappe.TimestampMismatchError)
        savepoint = "meta_cart_" + secrets.token_hex(8)
        frappe.db.savepoint(savepoint)
        try:
            so = _new_order(selected, snapshot)
            so.insert()  # Current actor's normal ERP create permission and native validation.
            if so.docstatus != 0 or digest(_financial(so)) != digest(frozen["financial"]):
                frappe.throw("ERP totals changed. Review this cart again.", frappe.TimestampMismatchError)
            doc.update({"sales_order": so.name, "state": "Ordered", "create_request": request_id, "reason_code": None})
            _save(doc)
        except Exception:
            frappe.db.rollback(save_point=savepoint)
            raise
        return {"sales_order": so.name, "sales_order_url": "/app/sales-order/" + quote(so.name, safe=""), "replayed": False}
