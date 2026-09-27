"""Unsupported cores and bad cart evidence never roll back the customer's message."""
import unittest
from unittest.mock import MagicMock, patch

import frappe

from doco_meta_catalog import inbound, orders
from doco_meta_catalog.orders_contract import CartError


class _Doc(dict):
    def __init__(self, **values):
        super().__init__(**values)
        self.name = values.get("name", "WM-test")


class TestInboundDegrades(unittest.TestCase):
    order = _Doc(type="Incoming", content_type="order")

    def test_deterministic_cart_failure_keeps_the_message(self):
        for reason in inbound.DETERMINISTIC_INTAKE_FAILURES:
            with self.subTest(reason=reason), \
                 patch("doco_meta_catalog.orders.intake", side_effect=CartError(reason)), \
                 patch("doco_meta_catalog.orders.frappe.log_error") as logged:
                result = inbound.on_whatsapp_message(self.order)
            self.assertEqual(result, {"state": "Unavailable", "reason_code": reason})
            logged.assert_called_once()

    def test_claim_failure_stays_transient(self):
        with patch("doco_meta_catalog.orders.intake", side_effect=CartError("receipt_claim_required")):
            with self.assertRaises(CartError):
                inbound.on_whatsapp_message(self.order)

    def test_unsupported_core_records_unavailable_intake_without_raising(self):
        with patch.object(frappe, "flags", frappe._dict(meta_webhook_receipt="receipt-1")), \
             patch.object(orders, "shared_scope_supported", return_value=False), \
             patch("doco_meta_catalog.orders.frappe.log_error") as logged, \
             patch.object(frappe, "db", MagicMock()) as db:
            result = orders.intake(self.order)
        self.assertEqual(result, {"state": "Unavailable", "reason_code": "shared_scope_unsupported"})
        logged.assert_called_once()
        db.get_value.assert_not_called()

    def test_review_actions_still_fail_closed(self):
        with patch.object(orders, "shared_scope_supported", return_value=False):
            with self.assertRaises(frappe.PermissionError):
                orders._ready()
