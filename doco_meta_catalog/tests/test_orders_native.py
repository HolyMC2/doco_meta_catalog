"""Native receipt/cart/ERP contracts on the disposable full application graph.

No network. These tests use actual controllers and permissions, not mocked ERP
prices, SO insertion or conversation authorization. Cross-process locking proof
belongs to the isolated concurrency driver; this suite tests replay and stale state.
"""
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase
from frappe.tests.utils import make_test_records
from frappe.utils import add_to_date, now_datetime
from frappe_whatsapp.webhook_receipts import record_events

from doco_meta_catalog import orders
from doco_meta_catalog.orders_contract import CartError, canonical


class TestOrdersNative(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        for doctype in ("Company", "Customer"):
            make_test_records(doctype, commit=False)
        cls.company = "_Test Company 1"
        if not frappe.db.exists("Company", cls.company):
            raise RuntimeError("Native ERP company fixture is required")

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.point = "cart_" + uuid4().hex
        frappe.db.savepoint(self.point)
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("No provider calls")))
        self.enterContext(patch("frappe.sendmail"))
        self.enterContext(patch("frappe.enqueue"))
        self.enterContext(patch("frappe.publish_realtime"))
        self.prefix = "cart-" + uuid4().hex[:10]
        self.peer = "5215550100888"
        self.account = self.account_fixture()
        self.customer_group = frappe.get_doc({"doctype": "Customer Group",
            "customer_group_name": self.prefix + " customers", "parent_customer_group": "All Customer Groups",
            "is_group": 0}).insert().name
        self.customer = frappe.get_doc({"doctype": "Customer", "customer_name": self.prefix,
            "customer_type": "Individual", "customer_group": self.customer_group, "territory": "All Territories"}).insert().name
        self.warehouse = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": self.prefix,
            "company": self.company}).insert().name
        from erpnext.stock.doctype.item.test_item import make_item
        self.item = make_item(self.prefix, properties={"is_stock_item": 0, "is_sales_item": 1,
            "publish_on_web": 1, "stock_uom": "Nos"}).name
        self.price_list = frappe.get_doc({"doctype": "Price List", "price_list_name": self.prefix,
            "selling": 1, "enabled": 1, "currency": "USD"}).insert().name
        self.price = frappe.get_doc({"doctype": "Item Price", "item_code": self.item,
            "price_list": self.price_list, "price_list_rate": 10, "currency": "USD", "selling": 1, "uom": "Nos"}).insert()
        frappe.db.set_single_value("Selling Settings", "selling_price_list", self.price_list)
        frappe.db.set_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing", 0)
        for key, value in {"catalog_id": "998877", "whatsapp_account": self.account.name,
                           "default_currency": "USD", "price_markup_percent": 0}.items():
            frappe.db.set_single_value("Meta Catalog Settings", key, value)
        self.payload = {"catalog_id": "998877", "text": "Keep <script>literal buyer note</script>",
            "product_items": [{"product_retailer_id": self.item, "quantity": 2, "item_price": "10", "currency": "USD"}]}
        self.selected = {"customer": self.customer, "company": self.company, "warehouse": self.warehouse}

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.flags.meta_webhook_receipt = None
        frappe.flags.meta_webhook_replay = False
        frappe.db.rollback(save_point=self.point)
        super().tearDown()

    def account_fixture(self):
        identifier = "98" + str(int(uuid4().hex[:12], 16))
        values = {"doctype": "WhatsApp Account", "account_name": self.prefix + uuid4().hex[:4],
                  "phone_id": identifier, "status": "Active", "mode": "Demo"}
        if frappe.db.has_column("WhatsApp Account", "doco_shop"):
            values["doco_shop"] = frappe.get_doc({"doctype": "Social Shop", "shop_name": self.prefix + uuid4().hex[:4], "enabled": 1}).insert().name
        doc = frappe.get_doc(values).insert()
        frappe.db.set_value("WhatsApp Account", doc.name, {"mode": "Live", "app_id": "9988", "business_id": "7766"})
        return doc.reload()

    @contextmanager
    def claim(self, receipt):
        old = frappe.flags.get("meta_webhook_receipt")
        frappe.flags.meta_webhook_receipt = receipt
        try:
            yield
        finally:
            frappe.flags.meta_webhook_receipt = old

    def receive(self, *, account=None, msg_id=None, payload=None):
        account = account or self.account
        msg_id = msg_id or "wamid." + uuid4().hex
        payload = payload or self.payload
        atom = {"id": msg_id, "from": self.peer, "type": "order", "order": payload}
        event = {"provider": "WhatsApp", "app_id": account.app_id, "account_id": account.phone_id,
            "event_type": "message", "event_id": msg_id, "payload": {"business_id": account.business_id,
            "change": {"field": "messages", "value": {"messaging_product": "whatsapp",
                "metadata": {"phone_number_id": account.phone_id}, "messages": [atom]}}}}
        receipt = record_events([event])[0]
        frappe.db.set_value("Meta Webhook Receipt", receipt,
            {"state": "Processing", "attempts": 1, "lease_until": add_to_date(now_datetime(), minutes=5)})
        with self.claim(receipt), patch.object(frappe.db, "commit", side_effect=AssertionError("No nested commit")):
            message = frappe.get_doc({"doctype": "WhatsApp Message", "type": "Incoming", "content_type": "order",
                "message": "Cart", "from": self.peer, "message_id": msg_id, "whatsapp_account": account.name,
                "product_catalog_json": canonical(payload)}).insert(ignore_permissions=True)
        name = frappe.db.get_value(orders.DOCTYPE, {"intake_key": receipt}, "name")
        self.assertTrue(name, "after_insert must persist intake in the receipt transaction")
        return name, message, receipt

    def test_intake_atomic_replay_and_two_accounts_keep_same_provider_id_distinct(self):
        count = frappe.db.count("Sales Order")
        name, message, receipt = self.receive(msg_id="same-id")
        with self.claim(receipt), patch.object(frappe.db, "commit", side_effect=AssertionError("No nested commit")):
            replay = orders.intake(message)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["name"], name)
        other, _, _ = self.receive(account=self.account_fixture(), msg_id="same-id")
        self.assertNotEqual(other, name)
        self.assertEqual(frappe.db.count("Sales Order"), count)
        self.assertEqual(orders.get_cart(name)["line_count"], 1)

    def test_unsigned_historical_message_does_not_create_intake(self):
        before = frappe.db.count(orders.DOCTYPE)
        message = frappe.get_doc({"doctype": "WhatsApp Message", "type": "Incoming", "content_type": "order",
            "message": "Historic", "from": self.peer, "message_id": "historic-" + uuid4().hex,
            "whatsapp_account": self.account.name, "product_catalog_json": canonical(self.payload)})
        self.assertEqual(orders.intake(message)["reason_code"], "receipt_transaction_required")
        self.assertEqual(frappe.db.count(orders.DOCTYPE), before)

    def test_expired_or_changed_receipt_cannot_replay(self):
        _, message, receipt = self.receive()
        frappe.db.set_value("Meta Webhook Receipt", receipt, "lease_until", add_to_date(now_datetime(), minutes=-1))
        with self.claim(receipt), self.assertRaises(CartError):
            orders.intake(message)

    def test_review_uses_native_totals_then_creates_one_actor_owned_draft(self):
        name, _, _ = self.receive()
        user = self.prefix + "-seller@example.invalid"
        frappe.get_doc({"doctype": "User", "email": user, "first_name": "Cart reviewer",
            "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}).insert()
        if self.account.get("doco_shop"):
            frappe.get_doc({"doctype": "User Permission", "user": user, "allow": "Social Shop",
                           "for_value": self.account.doco_shop}).insert()
        frappe.set_user(user)
        count = frappe.db.count("Sales Order")
        with patch.object(frappe.db, "commit", side_effect=AssertionError("No nested commit")):
            review = orders.review_cart(name, **self.selected)
            self.assertTrue(review["can_create"], review)
            self.assertEqual(frappe.db.count("Sales Order"), count)
            result = orders.create_order(name, review["review_token"], "first-click", **self.selected)
            again = orders.create_order(name, review["review_token"], "second-click", **self.selected)
        self.assertEqual(result["sales_order"], again["sales_order"])
        self.assertTrue(again["replayed"])
        so = frappe.get_doc("Sales Order", result["sales_order"])
        self.assertEqual(so.docstatus, 0)
        self.assertEqual(so.owner, frappe.session.user)
        self.assertEqual(so.customer, self.customer)
        self.assertEqual(so.company, self.company)
        self.assertEqual(len(so.items), 1)
        self.assertEqual(so.items[0].qty, 2)
        self.assertEqual(so.grand_total, review["total"])
        self.assertEqual(frappe.db.count("Sales Order"), count + 1)

    def test_native_customer_pricing_rule_is_reviewed_without_writes(self):
        name, _, _ = self.receive()
        frappe.get_doc({"doctype": "Pricing Rule", "title": self.prefix, "apply_on": "Item Code",
            "items": [{"item_code": self.item}], "selling": 1, "company": self.company,
            "currency": "USD", "price_or_product_discount": "Price", "rate_or_discount": "Discount Percentage",
            "discount_percentage": 10, "min_qty": 1}).insert()
        count = frappe.db.count("Sales Order")
        price_count = frappe.db.count("Item Price")
        review = orders.review_cart(name, **self.selected)
        self.assertTrue(review["can_create"], review)
        self.assertEqual(review["lines"][0]["rate"], 9)
        self.assertIn("price_changed", review["issues"])
        self.assertEqual(frappe.db.count("Sales Order"), count)
        self.assertEqual(frappe.db.count("Item Price"), price_count)
        result = orders.create_order(name, review["review_token"], "rule-click", **self.selected)
        self.assertEqual(frappe.db.get_value("Sales Order", result["sales_order"], "grand_total"), review["total"])

    def test_duplicate_click_cannot_change_reviewed_selection(self):
        name, _, _ = self.receive()
        review = orders.review_cart(name, **self.selected)
        orders.create_order(name, review["review_token"], "one", **self.selected)
        other_customer = frappe.get_doc({"doctype": "Customer", "customer_name": self.prefix + "-other",
            "customer_type": "Individual", "customer_group": self.customer_group, "territory": "All Territories"}).insert().name
        with self.assertRaises(frappe.ValidationError):
            orders.create_order(name, review["review_token"], "two", **dict(self.selected, customer=other_customer))

    def test_changed_price_and_disallowed_warehouse_require_new_review(self):
        name, _, _ = self.receive()
        review = orders.review_cart(name, **self.selected)
        self.price.price_list_rate = 12
        self.price.save()
        with self.assertRaises(frappe.TimestampMismatchError):
            orders.create_order(name, review["review_token"], "stale", **self.selected)
        bad = dict(self.selected, company="_Test Company")
        with self.assertRaises(frappe.ValidationError):
            orders.review_cart(name, **bad)
        fresh = orders.review_cart(name, **self.selected)
        self.assertIn("price_changed", fresh["issues"])

    def test_ambiguous_price_and_stock_are_explicit_blockers_without_losing_lines(self):
        self.payload["product_items"] *= 2
        name, _, _ = self.receive()
        duplicate = frappe.get_doc({"doctype": "Item Price", "item_code": self.item, "price_list": self.price_list,
            "price_list_rate": 12, "currency": "USD", "selling": 1, "uom": "Nos", "valid_from": "2020-01-01"})
        # Deliberate corrupt/ambiguous upstream data: bypass only the fixture's duplicate guard.
        duplicate.name = "ambiguous-" + uuid4().hex
        duplicate.db_insert()
        review = orders.review_cart(name, **self.selected)
        self.assertFalse(review["can_create"])
        self.assertIn("ambiguous_price", review["issues"])
        self.assertEqual(len(review["lines"]), 2)
        frappe.db.delete("Item Price", duplicate.name)
        frappe.db.set_value("Item", self.item, "is_stock_item", 1)
        review = orders.review_cart(name, **self.selected)
        self.assertIn("insufficient_stock", review["issues"])
        self.assertFalse(review["can_create"])

    def test_private_log_generic_reads_and_shares_do_not_lend_conversation_scope(self):
        name, _, _ = self.receive()
        user = self.prefix + "@example.invalid"
        frappe.get_doc({"doctype": "User", "email": user, "first_name": "Unassigned reviewer",
            "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}).insert()
        # Test a pre-existing or administratively inserted share, not permission to share a private table.
        frappe.get_doc({"doctype": "DocShare", "share_doctype": orders.DOCTYPE, "share_name": name,
                       "user": user, "read": 1, "everyone": 1}).insert(ignore_permissions=True)
        frappe.set_user(user)
        self.assertEqual(frappe.share.get_shared(orders.DOCTYPE), [])
        self.assertFalse(frappe.has_permission(orders.DOCTYPE, "read", doc=name))
        with self.assertRaises(frappe.PermissionError):
            orders.get_cart(name)
        with self.assertRaises(frappe.PermissionError):
            orders.review_cart(name, **self.selected)
