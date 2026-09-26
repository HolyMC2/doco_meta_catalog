"""Two independent SQL sessions on the explicitly owned disposable site only.

Run LAST after the native suite: this proof deliberately commits fictional
fixtures and leaves their canonical receipt/cart/SO records for inspection.
No production/site wildcard, provider calls or background dispatch is allowed.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import frappe

from doco_meta_catalog import orders


def run():
    if frappe.local.site != "crm-roadmap.localhost" or not frappe.conf.get("allow_tests"):
        raise RuntimeError("This proof is restricted to the owned CRM roadmap disposable site")
    orders._ready()
    from doco_meta_catalog.tests.test_orders_native import TestOrdersNative
    TestOrdersNative.setUpClass()
    case = TestOrdersNative("test_review_uses_native_totals_then_creates_one_actor_owned_draft")
    site = frappe.local.site
    sites_path = frappe.local.sites_path
    keys = (("Selling Settings", "selling_price_list"),
            ("Stock Settings", "auto_insert_price_list_rate_if_missing"),
            *(("Meta Catalog Settings", field) for field in
              ("catalog_id", "whatsapp_account", "default_currency", "price_markup_percent")))
    original = {(doctype, field): frappe.db.get_single_value(doctype, field) for doctype, field in keys}
    case.setUp()
    try:
        cart, _, _ = case.receive()
        review = orders.review_cart(cart, **case.selected)
        if not review["can_create"]:
            raise AssertionError(review["issues"])
        baseline = frappe.db.count("Sales Order")
        frappe.db.commit()  # Only the explicit proof owns these fixture transactions.
        barrier = Barrier(2)

        def create(index):
            frappe.init(site=site, sites_path=sites_path)
            frappe.connect()
            frappe.set_user("Administrator")
            frappe.flags.in_test = True
            try:
                barrier.wait(timeout=15)
                result = orders.create_order(cart, review["review_token"], f"concurrent-{index}", **case.selected)
                frappe.db.commit()
                return result
            except Exception:
                frappe.db.rollback()
                raise
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create, index) for index in range(2)]
            results = [future.result(timeout=45) for future in futures]
        # End the parent's old snapshot before checking independently committed rows.
        frappe.db.rollback()
        names = {result["sales_order"] for result in results}
        if len(names) != 1 or sum(bool(result["replayed"]) for result in results) != 1:
            raise AssertionError("Concurrent requests did not converge on one created draft")
        name = names.pop()
        if frappe.db.count("Sales Order") != baseline + 1 or frappe.db.get_value("Sales Order", name, "docstatus") != 0:
            raise AssertionError("Concurrent draft count/status differs")
        return {"cart": cart, "sales_order": name, "requests": 2, "created": 1, "replayed": 1,
                "provider_calls": 0, "fixture_prefix": case.prefix}
    finally:
        # Preserve proof records, restore singleton configuration, then close mocks.
        frappe.db.rollback()
        for (doctype, field), value in original.items():
            frappe.db.set_single_value(doctype, field, value)
        frappe.db.commit()
        case.doCleanups()
