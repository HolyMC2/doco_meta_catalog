"""Read-only generic catalog quotes. Never create documents or change actor/transaction."""

import math

import frappe
from frappe.utils import flt, today


def catalog_currency(settings):
    currency = settings.default_currency
    if not currency:
        frappe.throw("Set the catalog currency in Meta Catalog Settings before publishing prices.")
    return currency


def _applicable_rules(codes, day, *, lock=False):
    """Active automatic selling rules that can change a price of these items.

    A promotion on unrelated items must not block the whole catalog. Transaction-wide
    or unrecognised rules still apply to everything.
    """
    if not codes:
        return []
    rules = frappe.db.get_values(
        "Pricing Rule",
        filters={"selling": 1, "disable": 0, "coupon_code_based": 0},
        fieldname=["name", "apply_on", "valid_from", "valid_upto"],
        for_update=lock,
        limit=None,
        as_dict=True,
    )
    rules = [
        row for row in rules
        if (not row.valid_from or str(row.valid_from) <= day)
        and (not row.valid_upto or str(row.valid_upto) >= day)
    ]
    if not rules:
        return []
    items = frappe.db.get_values(
        "Item", filters={"name": ["in", list(codes)]},
        fieldname=["name", "variant_of", "item_group", "brand"], limit=None, as_dict=True,
    )
    # ERPNext applies a template's rules to its variants.
    item_codes = {row.name for row in items} | {row.variant_of for row in items if row.variant_of}
    brands = {row.brand for row in items if row.brand}
    groups = set()
    for group in {row.item_group for row in items if row.item_group}:
        bounds = frappe.db.get_value("Item Group", group, ["lft", "rgt"], as_dict=True)
        if bounds:
            groups.update(frappe.get_all("Item Group", filters={
                "lft": ["<=", bounds.lft], "rgt": [">=", bounds.rgt]}, pluck="name"))
    children = {
        "Item Code": ("Pricing Rule Item Code", "item_code", item_codes),
        "Item Group": ("Pricing Rule Item Group", "item_group", groups),
        "Brand": ("Pricing Rule Brand", "brand", brands),
    }
    applicable = []
    for rule in rules:
        if rule.apply_on not in children:
            applicable.append(rule.name)
            continue
        doctype, field, wanted = children[rule.apply_on]
        values = frappe.get_all(doctype, filters={"parent": rule.name, "parenttype": "Pricing Rule"},
                                pluck=field)
        if not values or wanted.intersection(values):
            applicable.append(rule.name)
    return applicable


def quote(codes, settings, *, lock=False):
    """One current generic Item Price in the configured list, stock UOM and currency.

    Automatic rules can depend on party, quantity and company. A generic catalog
    cannot assert that price. Surface a blocker instead of silently dropping a
    discount or running the storefront's throwaway Sales Order/rollback helper.
    Reviewed orders must show their separate native ERP quote before creation.
    """
    day = today()
    if _applicable_rules(codes, day, lock=lock):
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
    currency = catalog_currency(settings)
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
