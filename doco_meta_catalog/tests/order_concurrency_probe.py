"""Two independent SQL sessions on the explicitly owned disposable site only.

Run LAST after the native suite: this proof deliberately commits fictional
fixtures and leaves their canonical receipt/cart/SO records for inspection.
No production/site wildcard, provider calls or background dispatch is allowed.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import frappe

from doco_meta_catalog import orders


def request_attempt(site, sites_path, user, operation, *, allow_abort=False):
    """One request only; callers explicitly decide whether to issue a fresh one."""
    frappe.init(site=site, sites_path=sites_path)
    frappe.connect()
    frappe.set_user(user)
    frappe.flags.in_test = True
    try:
        result = operation()
        frappe.db.commit()
        return {"outcome": "committed", "result": result}
    except frappe.QueryDeadlockError as error:
        frappe.db.rollback()
        native = getattr(error.args[0], "args", ()) if error.args else ()
        if not allow_abort or not native or native[0] != 1020:
            raise
        print("Probe request aborted: QueryDeadlockError errno=1020; rolled back, closing connection")
        return {"outcome": "aborted", "error": type(error).__name__, "errno": 1020}
    except Exception:
        frappe.db.rollback()
        raise
    finally:
        frappe.destroy()


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
        baseline = frappe.db.count("Sales Order", {"customer": case.customer})
        snapshot_isolation = bool(frappe.db.sql("SELECT @@innodb_snapshot_isolation")[0][0])
        frappe.db.commit()  # Only the explicit proof owns these fixture transactions.
        locked, snapshot = Event(), Event()
        commands = [dict(name=cart, review_token=review["review_token"],
                         request_id=f"{case.prefix}-concurrent-{index}", **case.selected) for index in range(2)]

        def writer():
            # Force the second request to establish its old view before the
            # first commits. The ordinary app locks remain responsible for safety.
            with orders._locked_cart(cart):
                locked.set()
                if not snapshot.wait(20):
                    raise AssertionError("Cart reader did not establish its old snapshot")
                result = orders.create_order(**commands[0])
                frappe.db.commit()
                return result

        def reader():
            if not locked.wait(20):
                raise AssertionError("Cart writer did not hold its native fence")
            if frappe.db.get_value(orders.DOCTYPE, cart, "sales_order"):
                raise AssertionError("Reader did not start before the created order")
            snapshot.set()
            return orders.create_order(**commands[1])

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(request_attempt, site, sites_path, "Administrator", writer)
            second = pool.submit(request_attempt, site, sites_path, "Administrator", reader, allow_abort=True)
            winner, loser = first.result(timeout=60), second.result(timeout=60)
            if winner["outcome"] != "committed" or winner["result"]["replayed"]:
                raise AssertionError("The initial cart writer did not create the draft")
            expected = "aborted" if snapshot_isolation else "committed"
            if loser["outcome"] != expected:
                raise AssertionError("Cart reader outcome differs from native snapshot isolation")
            name = winner["result"]["sales_order"]

            def assert_one_effect():
                frappe.db.rollback()  # inspect independently committed evidence
                doc = frappe.get_doc(orders.DOCTYPE, cart)
                if (doc.state != "Ordered" or doc.sales_order != name
                        or doc.create_request != commands[0]["request_id"]
                        or frappe.db.count("Sales Order", {"customer": case.customer}) != baseline + 1
                        or frappe.db.get_value("Sales Order", name, "docstatus") != 0):
                    raise AssertionError("Aborted/replayed cart request changed the winner's sole draft")
                frappe.db.rollback()

            assert_one_effect()  # prove no loser effect BEFORE a fresh request
            attempts = [{k: v for k, v in loser.items() if k != "result"}]
            if loser["outcome"] == "aborted":
                # Explicit manual caller retry; no retry inside the old transaction
                # and no new token, selection or idempotency key.
                loser = pool.submit(request_attempt, site, sites_path, "Administrator",
                                    lambda: orders.create_order(**commands[1])).result(timeout=60)
                attempts.append({k: v for k, v in loser.items() if k != "result"})
            if loser["result"]["sales_order"] != name or not loser["result"]["replayed"]:
                raise AssertionError("Fresh unchanged cart command did not replay the winner")
            assert_one_effect()
        # End the parent's old snapshot before checking independently committed rows.
        frappe.db.rollback()
        return {"cart": cart, "sales_order": name, "requests": 2, "created": 1, "replayed": 1,
                "reader_attempts": attempts, "snapshot_isolation": snapshot_isolation,
                "request_attempts": 1 + len(attempts),
                "provider_calls": 0, "fixture_prefix": case.prefix}
    finally:
        # Preserve proof records, restore singleton configuration, then close mocks.
        frappe.db.rollback()
        for (doctype, field), value in original.items():
            frappe.db.set_single_value(doctype, field, value)
        frappe.db.commit()
        case.doCleanups()
