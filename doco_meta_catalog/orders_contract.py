"""Pure, lossless cart discrepancy and authenticated receipt contracts."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

BLOCKERS = frozenset({
    "unknown_item", "unpublished_item", "unpriced_item", "wrong_catalog", "currency_mismatch",
    "invalid_quantity", "quantity_limit", "line_limit", "insufficient_stock", "ambiguous_price",
    "catalog_unavailable", "account_unknown", "erp_review_required",
})


class CartError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(condition, reason="order_receipt_invalid"):
    if not condition:
        raise CartError(reason)


def receipt_order(receipt, message, account):
    """The caller locks and checks the worker's live claim before using this pure validator."""
    try:
        payload = receipt["payload"]
        require(isinstance(payload, str) and len(payload.encode()) <= 128 * 1024)
        require(hashlib.sha256(payload.encode()).hexdigest() == receipt["payload_hash"])
        require(receipt["provider"] == "WhatsApp" and receipt["event_type"] == "message")
        require(digest([receipt[k] for k in ("provider", "app_id", "account_id", "event_type", "event_id")]) == receipt["event_key"] == receipt["name"])
        evidence = json.loads(payload)
        value = evidence["change"]["value"]
        require(evidence["change"]["field"] == "messages" and value["messaging_product"] == "whatsapp")
        require(not any(key in value for key in ("statuses", "history", "message_echoes", "state_sync", "smb_app_state_sync")))
        require(value["metadata"]["phone_number_id"] == receipt["account_id"] == account["phone_id"])
        require(account["app_id"] == receipt["app_id"] and account["business_id"] == evidence["business_id"])
        require(account["status"] == "Active" and account["mode"] == "Live")
        atoms = value["messages"]
        require(isinstance(atoms, list) and len(atoms) == 1 and isinstance(atoms[0], dict))
        atom = atoms[0]
        require(atom["type"] == "order" and atom["id"] == receipt["event_id"] == message["message_id"])
        require(isinstance(atom["from"], str) and re.fullmatch(r"[0-9]{1,20}", atom["from"]))
        require(atom["from"] == message["from"])
        require(message["type"] == "Incoming" and message["content_type"] == "order")
        require(message["whatsapp_account"] == account["name"])
        order = atom["order"]
        require(isinstance(order, dict) and isinstance(order.get("product_items"), list))
        require(canonical(order) == canonical(json.loads(message["product_catalog_json"])))
    except (KeyError, TypeError, ValueError, AttributeError, UnicodeError):
        raise CartError("order_receipt_invalid") from None
    return order, atom["from"], evidence["business_id"]


def decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        # Provider values remain verbatim in requested_*; calculations must stay
        # bounded, finite and JSON representable even for malicious exponents.
        return result if result.is_finite() and abs(result) <= Decimal('1e15') else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def money(value):
    with localcontext() as context:
        context.prec = 64
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def requested_lines(order):
    """Every original index survives, including invalid and duplicate rows."""
    return [
        {"index": index, "item_code": row.get("product_retailer_id") if isinstance(row, dict) else None,
         "requested_quantity": row.get("quantity") if isinstance(row, dict) else None,
         "requested_price": row.get("item_price") if isinstance(row, dict) else None,
         "requested_currency": row.get("currency") if isinstance(row, dict) else None}
        for index, row in enumerate(order.get("product_items", []))
    ]


def evaluate_lines(order, catalog_id, currency, items, prices, stock, *, max_lines=50, max_qty=999):
    """Server metadata in; reviewable lines out. Never truncate, clamp, merge or drop."""
    lines = requested_lines(order)
    issues = []
    if not lines or len(lines) > max_lines:
        issues.append("line_limit")
    if order.get("catalog_id") != catalog_id:
        issues.append("wrong_catalog")
    counts = Counter(row["item_code"] for row in lines if isinstance(row["item_code"], str))
    quantities = {}
    for row in lines:
        qty = decimal(row["requested_quantity"])
        code = row["item_code"]
        if isinstance(code, str) and qty is not None and qty > 0:
            quantities[code] = quantities.get(code, Decimal(0)) + qty
    for row in lines:
        code = row["item_code"]
        item = items.get(code) if isinstance(code, str) else None
        price = prices.get(code, {}) if isinstance(code, str) else {}
        qty = decimal(row["requested_quantity"])
        problems = []
        if not item:
            problems.append("unknown_item")
        elif not item.get("published"):
            problems.append("unpublished_item")
        if qty is None or qty <= 0 or (item and item.get("whole_number") and qty != qty.to_integral_value()):
            problems.append("invalid_quantity")
        elif qty > max_qty or (isinstance(code, str) and quantities.get(code, 0) > max_qty):
            problems.append("quantity_limit")
        if row["requested_currency"] != currency:
            problems.append("currency_mismatch")
        if price.get("issue"):
            problems.append(price["issue"])
        rate = decimal(price.get("rate"))
        if not price.get("issue") and (rate is None or rate <= 0):
            problems.append("unpriced_item")
        requested = decimal(row["requested_price"])
        if rate is not None and (requested is None or money(requested) != money(rate)):
            problems.append("price_changed")
        available = stock.get(code) if isinstance(code, str) else None
        if item and item.get("stock_tracked") and (available is None or Decimal(str(available)) < quantities.get(code, 0)):
            problems.append("insufficient_stock")
        if isinstance(code, str) and counts.get(code, 0) > 1:
            problems.append("duplicate_line")
        row.update(item_name=item.get("item_name", "") if item else "", quantity=float(qty) if qty is not None else None,
                   rate=float(rate) if rate is not None else None, available_qty=available, issues=list(dict.fromkeys(problems)))
    issues = list(dict.fromkeys(issues + [issue for row in lines for issue in row["issues"]]))
    total = money(sum(Decimal(str(row["quantity"] or 0)) * Decimal(str(row["rate"] or 0)) for row in lines))
    return {"lines": lines, "issues": issues, "total": total, "can_create": not bool(BLOCKERS.intersection(issues))}
