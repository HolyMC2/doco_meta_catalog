"""Legacy jobs are harmless; signed-receipt order intake is synchronous."""
import unittest
from unittest.mock import patch

from doco_meta_catalog import inbound, wa_helpers


class _Doc:
    def __init__(self, **values):
        self.values = values
        self.name = values.get("name", "WM-test")

    def get(self, key):
        return self.values.get(key)


class TestInboundPickoff(unittest.TestCase):
    def test_order_runs_inside_the_inserting_transaction(self):
        doc = _Doc(type="Incoming", content_type="order")
        with patch("doco_meta_catalog.orders.intake", return_value={"state": "Needs Review"}) as intake, \
             patch.object(inbound.frappe, "enqueue") as enqueue:
            self.assertEqual(inbound.on_whatsapp_message(doc), {"state": "Needs Review"})
            intake.assert_called_once_with(doc)
            enqueue.assert_not_called()

    def test_outgoing_and_unrelated_messages_do_not_intake(self):
        with patch("doco_meta_catalog.orders.intake") as intake, patch.object(inbound.frappe, "enqueue") as enqueue:
            inbound.on_whatsapp_message(_Doc(type="Outgoing", content_type="order"))
            inbound.on_whatsapp_message(_Doc(type="Incoming", content_type="text"))
            intake.assert_not_called()
            enqueue.assert_not_called()

    def test_intake_failure_propagates_to_receipt_transaction(self):
        with patch("doco_meta_catalog.orders.intake", side_effect=ValueError("bad receipt")):
            with self.assertRaisesRegex(ValueError, "bad receipt"):
                inbound.on_whatsapp_message(_Doc(type="Incoming", content_type="order"))

    def test_old_jobs_never_write_elevate_commit_or_send(self):
        with patch.object(inbound.frappe, "set_user") as elevate, \
             patch.object(inbound.frappe, "get_doc") as get_doc, \
             patch.object(inbound.frappe, "new_doc") as new_doc, \
             patch.object(inbound.frappe.db, "commit") as commit:
            self.assertEqual(inbound.process_order("old-message")["reason_code"], "receipt_transaction_required")
            self.assertIsNone(wa_helpers.handle_order_message({"id": "old-message"}, trusted=True))
            self.assertFalse(wa_helpers._claim_order("old-message"))
            for method in (elevate, get_doc, new_doc, commit):
                method.assert_not_called()
