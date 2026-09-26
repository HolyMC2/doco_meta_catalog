"""Pure evidence and lossless discrepancy tests; runnable without a bench."""
import copy
import hashlib
import unittest

from doco_meta_catalog.orders_contract import CartError, canonical, digest, evaluate_lines, receipt_order


class TestOrderContract(unittest.TestCase):
    def setUp(self):
        self.order = {"catalog_id": "catalog", "product_items": [
            {"product_retailer_id": "A", "quantity": 2, "item_price": "10.00", "currency": "MXN"}]}
        self.items = {"A": {"item_name": "A", "published": True, "stock_tracked": True, "whole_number": True}}
        self.prices = {"A": {"rate": 10}}

    def evaluate(self, order=None, **kwargs):
        return evaluate_lines(order or self.order, "catalog", "MXN", kwargs.get("items", self.items),
                              kwargs.get("prices", self.prices), kwargs.get("stock", {"A": 5}))

    def test_exact_review_and_duplicate_lines_stay_separate(self):
        self.order["product_items"] *= 2
        result = self.evaluate()
        self.assertTrue(result["can_create"])
        self.assertEqual([row["index"] for row in result["lines"]], [0, 1])
        self.assertEqual([row["quantity"] for row in result["lines"]], [2, 2])
        self.assertIn("duplicate_line", result["issues"])
        self.assertEqual(result["total"], 40)
        self.assertFalse(self.evaluate(stock={"A": 3})["can_create"])

    def test_every_line_and_original_value_survives_limits_and_malformed_data(self):
        self.order["product_items"] = [{"product_retailer_id": "A", "quantity": 1000,
                                       "item_price": "9", "currency": "USD"}] * 51 + [None]
        result = self.evaluate()
        self.assertEqual(len(result["lines"]), 52)
        self.assertEqual(result["lines"][0]["requested_quantity"], 1000)
        for code in ("line_limit", "quantity_limit", "currency_mismatch", "unknown_item", "invalid_quantity"):
            self.assertIn(code, result["issues"])
        self.assertFalse(result["can_create"])

    def test_price_change_is_reviewable_but_ambiguous_unpublished_unknown_are_blockers(self):
        self.assertTrue(self.evaluate(prices={"A": {"rate": 12}})["can_create"])
        self.assertIn("price_changed", self.evaluate(prices={"A": {"rate": 12}})["issues"])
        for values, code in (({"prices": {"A": {"issue": "ambiguous_price"}}}, "ambiguous_price"),
                             ({"items": {}}, "unknown_item"),
                             ({"items": {"A": {"published": False}}}, "unpublished_item")):
            result = self.evaluate(**values)
            self.assertFalse(result["can_create"])
            self.assertIn(code, result["issues"])

    def test_bad_numeric_values_are_lossless_finite_and_blocked(self):
        for qty in (0, -1, "1e10000", "NaN", "Infinity", True, "garbage", 1.5):
            self.order["product_items"][0]["quantity"] = qty
            result = self.evaluate()
            self.assertFalse(result["can_create"])
            self.assertEqual(result["lines"][0]["requested_quantity"], qty)
            canonical(result)

    def test_malformed_item_identity_is_preserved_without_unhashable_crash(self):
        for code in (["A"], {"item": "A"}, None, 42):
            self.order["product_items"][0]["product_retailer_id"] = code
            result = self.evaluate()
            self.assertEqual(result["lines"][0]["item_code"], code)
            self.assertIn("unknown_item", result["issues"])
            canonical(result)

    def evidence(self, account_id="123", message_id="same-message"):
        account = {"name": "account-" + account_id, "phone_id": account_id, "app_id": "app",
                   "business_id": "business", "status": "Active", "mode": "Live"}
        atom = {"id": message_id, "from": "5215550001111", "type": "order", "order": self.order}
        payload = canonical({"business_id": "business", "change": {"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": account_id}, "messages": [atom]}}})
        receipt = {"provider": "WhatsApp", "app_id": "app", "account_id": account_id,
                   "event_type": "message", "event_id": message_id, "payload": payload,
                   "payload_hash": hashlib.sha256(payload.encode()).hexdigest()}
        receipt["name"] = receipt["event_key"] = digest([receipt[k] for k in ("provider", "app_id", "account_id", "event_type", "event_id")])
        message = {"message_id": message_id, "from": atom["from"], "type": "Incoming", "content_type": "order",
                   "whatsapp_account": account["name"], "product_catalog_json": canonical(self.order)}
        return receipt, message, account

    def test_same_message_id_different_accounts_has_distinct_authenticated_identity(self):
        one, two = self.evidence("123"), self.evidence("456")
        self.assertNotEqual(one[0]["name"], two[0]["name"])
        self.assertEqual(receipt_order(*one)[0], self.order)
        self.assertEqual(receipt_order(*two)[0], self.order)

    def test_identity_payload_sender_and_account_tampering_rejected(self):
        for target, key, value in ((0, "payload_hash", "bad"), (0, "name", "bad"),
                                   (1, "from", "5215559999999"), (1, "product_catalog_json", "{}"),
                                   (2, "app_id", "other"), (2, "mode", "Test")):
            evidence = copy.deepcopy(self.evidence())
            evidence[target][key] = value
            with self.assertRaises(CartError):
                receipt_order(*evidence)
