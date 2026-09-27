"""Read-only generic catalog quotes. Never create documents or change actor/transaction."""

import math

import frappe
from frappe.utils import flt, today


def quote(codes, settings, *, lock=False):
    """One current generic Item Price in the configured list, stock UOM and currency.

    Automatic rules can depend on party, quantity and company. A generic catalog
    cannot assert that price. Surface a blocker instead of silently dropping a
    discount or running the storefront's throwaway Sales Order/rollback helper.
    Reviewed orders must show their separate native ERP quote before creation.
    """
    day = today()
    rules = frappe.db.get_values(
        "Pricing Rule",
        filters={"selling": 1, "disable": 0, "coupon_code_based": 0},
        fieldname=["name", "valid_from", "valid_upto"],
        for_update=lock,
        limit=None,
        as_dict=True,
    )
    if any(
        (not row.valid_from or str(row.valid_from) <= day)
        and (not row.valid_upto or str(row.valid_upto) >= day)
        for row in rules
    ):
        frappe.throw(
            "Automatic pricing rules require an explicit customer/quantity quote. Generic catalog pricing is unavailable."
        )
    if lock:
        configured_list = frappe.db.sql(
            "SELECT value FROM tabSingles WHERE doctype='Selling Settings' AND field='selling_price_list' FOR UPDATE"
        )
        price_list = configured_list[0][0] if configured_list else None
    else:
        price_list = frappe.db.get_single_value(
            "Selling Settings", "selling_price_list"
        )
    currency = settings.default_currency or "MXN"
    configured = (
        frappe.db.get_value(
            "Price List",
            price_list,
            ["enabled", "selling", "currency"],
            as_dict=True,
            for_update=lock,
        )
        if price_list
        else None
    )
    if (
        not configured
        or not configured.enabled
        or not configured.selling
        or configured.currency != currency
    ):
        frappe.throw(
            "Configure one enabled selling price list in the catalog currency."
        )
    if lock and codes:
        frappe.db.sql(
            "SELECT name FROM `tabItem Price` WHERE item_code IN %(codes)s ORDER BY name FOR UPDATE",
            {"codes": codes},
        )
    uoms = (
        {
            row.name: row.stock_uom
            for row in frappe.db.get_values(
                "Item",
                filters={"name": ["in", codes]},
                fieldname=["name", "stock_uom"],
                for_update=lock,
                limit=None,
                as_dict=True,
            )
        }
        if codes
        else {}
    )
    rows = (
        frappe.db.get_values(
            "Item Price",
            filters={
                "item_code": ["in", codes],
                "selling": 1,
                "price_list": price_list,
            },
            fieldname=[
                "item_code",
                "price_list_rate",
                "currency",
                "uom",
                "customer",
                "supplier",
                "valid_from",
                "valid_upto",
                "batch_no",
                "packing_unit",
            ],
            for_update=lock,
            limit=None,
            as_dict=True,
        )
        if codes
        else []
    )
    grouped = {}
    for row in rows:
        if (
            row.customer
            or row.supplier
            or row.batch_no
            or row.currency != currency
            or flt(row.packing_unit or 1) != 1
            or (row.uom and row.uom != uoms.get(row.item_code))
            or (row.valid_from and str(row.valid_from) > day)
            or (row.valid_upto and str(row.valid_upto) < day)
        ):
            continue
        grouped.setdefault(row.item_code, []).append(flt(row.price_list_rate))
    ambiguous = [code for code, rates in grouped.items() if len(rates) != 1]
    if ambiguous:
        frappe.throw(
            "More than one current generic Item Price exists. Resolve duplicate catalog prices before publication."
        )
    base = {
        code: rates[0]
        for code, rates in grouped.items()
        if math.isfinite(rates[0]) and rates[0] > 0
    }
    markup = flt(settings.price_markup_percent)
    if not math.isfinite(markup) or not 0 <= markup <= 10000:
        frappe.throw("Catalog markup must be between 0 and 10000 percent.")
    return {
        "price_list": price_list,
        "currency": currency,
        "base_rates": base,
        "rates": {
            code: flt(rate * (1 + markup / 100), 2) for code, rate in base.items()
        },
        "markup": markup,
        "source": "Current generic Item Price; catalog markup applied; automatic rules unavailable",
    }
