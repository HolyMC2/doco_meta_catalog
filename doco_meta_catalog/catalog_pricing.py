"""Read-only generic catalog quotes. Never create documents or change actor/transaction."""

import math

import frappe
from frappe import _
from frappe.utils import flt, today


def catalog_currency(settings):
    currency = settings.default_currency
    if not currency:
        frappe.throw("Set the catalog currency in Meta Catalog Settings before publishing prices.")
    return currency


def _rule_hits(codes, day, *, lock=False):
    """Active automatic selling rules that can change a price of these items:
    (rules that apply to every item, {code: rules that apply to that item}).

    A promotion on unrelated items must not block the whole catalog. Transaction-wide
    or unrecognised rules, and rules with no rows, still apply to everything.
    """
    if not codes:
        return [], {}
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
        return [], {}
    items = frappe.db.get_values(
        "Item", filters={"name": ["in", list(codes)]},
        fieldname=["name", "variant_of", "item_group", "brand"], limit=None, as_dict=True,
    )
    ancestors = {}
    for group in {row.item_group for row in items if row.item_group}:
        bounds = frappe.db.get_value("Item Group", group, ["lft", "rgt"], as_dict=True)
        if bounds:
            ancestors[group] = set(frappe.get_all("Item Group", filters={
                "lft": ["<=", bounds.lft], "rgt": [">=", bounds.rgt]}, pluck="name"))
    # ERPNext applies a template's rules to its variants.
    keys = {
        row.name: {
            "Item Code": {row.name, row.variant_of} - {None, ""},
            "Item Group": ancestors.get(row.item_group, set()),
            "Brand": {row.brand} if row.brand else set(),
        }
        for row in items
    }
    children = {
        "Item Code": ("Pricing Rule Item Code", "item_code"),
        "Item Group": ("Pricing Rule Item Group", "item_group"),
        "Brand": ("Pricing Rule Brand", "brand"),
    }
    everywhere, per_item = [], {}
    for rule in rules:
        if rule.apply_on not in children:
            everywhere.append(rule.name)
            continue
        doctype, field = children[rule.apply_on]
        values = set(frappe.get_all(doctype, filters={"parent": rule.name, "parenttype": "Pricing Rule"},
                                    pluck=field))
        if not values:
            everywhere.append(rule.name)
            continue
        for code, wanted in keys.items():
            if wanted[rule.apply_on] & values:
                per_item.setdefault(code, []).append(rule.name)
    return everywhere, per_item


def quote(codes, settings, *, lock=False, price_list=None, hold=False):
    """One current generic Item Price in the configured list, stock UOM and currency.

    Automatic rules can depend on party, quantity and company. A generic catalog
    cannot assert that price. Surface a blocker instead of silently dropping a
    discount or running the storefront's throwaway Sales Order/rollback helper.
    Reviewed orders must show their separate native ERP quote before creation.

    ``hold=True`` (catalog publication) keeps one item's price problem from stopping
    the whole catalog: an item with duplicate current prices or its own pricing rule
    comes back in ``held`` with the reason, and gets no rate. Rules that apply to every
    item still stop everything. Orders keep the strict default.
    """
    day = today()
    everywhere, per_item = _rule_hits(codes, day, lock=lock)
    if everywhere or (per_item and not hold):
        frappe.throw(
            "Automatic pricing rules require an explicit customer/quantity quote. Generic catalog pricing is unavailable."
        )
    held = {
        code: {
            "kind": "price_rule",
            "reason": _("A pricing rule ({0}) changes this price for some customers or quantities.").format(
                ", ".join(names)
            ),
        }
        for code, names in per_item.items()
    }
    # The shop's own list when the catalog mirrors a storefront; else the default list.
    if not price_list and lock:
        configured_list = frappe.db.sql(
            "SELECT value FROM tabSingles WHERE doctype='Selling Settings' AND field='selling_price_list' FOR UPDATE"
        )
        price_list = configured_list[0][0] if configured_list else None
    elif not price_list:
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
    ambiguous = {code: rates for code, rates in grouped.items() if len(rates) != 1}
    if ambiguous and not hold:
        frappe.throw(
            "More than one current generic Item Price exists. Resolve duplicate catalog prices before publication."
        )
    for code, rates in ambiguous.items():
        held.setdefault(code, {
            "kind": "duplicate_price",
            "reason": _("{0} current prices in {1} ({2}). Keep one.").format(
                len(rates), price_list, ", ".join(f"{rate:.2f}" for rate in sorted(rates))
            ),
        })
    base = {
        code: rates[0]
        for code, rates in grouped.items()
        if code not in held and math.isfinite(rates[0]) and rates[0] > 0
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
        "held": held,
        "source": "Current generic Item Price; catalog markup applied; automatic rules unavailable",
    }
