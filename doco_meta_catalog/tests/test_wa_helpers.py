"""Tests for the outbound WhatsApp send guard (authorization + rate limit + E.164 validation).

The senders are @frappe.whitelist() and reachable by any Desk login; these tests pin that every
send is gated (role check runs FIRST, before any account lookup or network call) and that bad
recipient numbers are rejected.
"""

import unittest
from unittest.mock import patch

import frappe

from doco_meta_catalog import wa_helpers


class TestSendGuard(unittest.TestCase):
    def test_e164_regex(self):
        for n in ["+5216691234567", "5216691234567", "+12025550123"]:
            self.assertTrue(wa_helpers._E164.match(n), n)
        for n in ["", "not-a-phone", "+52-669-123", "12345", "+" + "9" * 20]:
            self.assertFalse(wa_helpers._E164.match(n), n)

    def test_guard_rejects_bad_phone(self):
        with patch.object(wa_helpers.frappe, "only_for"), patch.object(wa_helpers._sf, "_rate_limit"):
            with self.assertRaises(frappe.ValidationError):
                wa_helpers._guard_send("not-a-phone")

    def test_guard_accepts_good_phone(self):
        with patch.object(wa_helpers.frappe, "only_for") as of, patch.object(wa_helpers._sf, "_rate_limit") as rl:
            wa_helpers._guard_send("+5216691234567")  # must not raise
            of.assert_called_once()
            rl.assert_called_once()

    def test_guard_enforces_role(self):
        with patch.object(wa_helpers.frappe, "only_for", side_effect=frappe.PermissionError), \
             patch.object(wa_helpers._sf, "_rate_limit"):
            with self.assertRaises(frappe.PermissionError):
                wa_helpers._guard_send("+5216691234567")

    def test_sender_gates_before_doing_anything(self):
        # send_product_message must call _guard_send BEFORE _outgoing_account / any send
        with patch.object(wa_helpers, "_guard_send", side_effect=frappe.PermissionError) as g, \
             patch.object(wa_helpers, "_outgoing_account") as acct:
            with self.assertRaises(frappe.PermissionError):
                wa_helpers.send_product_message("+5216691234567", "IT-X")
            g.assert_called_once()
            acct.assert_not_called()


if __name__ == "__main__":
    unittest.main()
