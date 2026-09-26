"""Provider fixtures establish distinctions, never certify real delivery."""

import unittest
from unittest.mock import patch

from doco_meta_catalog.connection import classify, latest


class TestCatalogConnection(unittest.TestCase):
    def observed(self, catalogs=None, phone=None, commerce=None):
        return classify(
            "111",
            {"data": [{"id": "111"}]} if catalogs is None else catalogs,
            "222",
            {"id": "222", "status": "CONNECTED"} if phone is None else phone,
            {"data": [{"is_catalog_visible": True, "is_cart_enabled": True}]}
            if commerce is None
            else commerce,
        )

    def test_empty_edge_is_unconfirmed_even_when_phone_is_connected(self):
        self.assertEqual(
            self.observed(catalogs={"data": []}),
            {"state": "Unknown", "reason_code": "catalog_link_not_returned"},
        )

    def test_different_catalog_or_phone_is_not_binding_proof(self):
        self.assertEqual(
            self.observed(catalogs={"data": [{"id": "333"}]})["state"], "Unknown"
        )
        self.assertEqual(
            self.observed(phone={"id": "333", "status": "CONNECTED"})["state"],
            "Unknown",
        )

    def test_visibility_and_cart_are_separate(self):
        hidden = self.observed(
            commerce={"data": [{"is_catalog_visible": False, "is_cart_enabled": True}]}
        )
        self.assertEqual(hidden["state"], "Unavailable")
        result = self.observed(
            commerce={"data": [{"is_catalog_visible": True, "is_cart_enabled": False}]}
        )
        self.assertEqual(result["state"], "Observed")
        self.assertFalse(result["cart_enabled"])

    def test_unknown_shapes_do_not_become_success(self):
        self.assertEqual(self.observed(commerce={"data": []})["state"], "Unknown")
        self.assertEqual(self.observed(catalogs={"data": {}})["state"], "Unknown")
        self.assertEqual(
            self.observed(phone={"id": "222", "status": "DISCONNECTED"})["state"],
            "Unavailable",
        )
        self.assertEqual(self.observed()["state"], "Observed")

    def test_observation_is_scoped_and_expires(self):
        import frappe

        row = frappe._dict(
            name="fixture",
            checked_at=frappe.utils.get_datetime("2026-01-01 00:00:00"),
            summary='{"connection":{"state":"Observed","reason_code":""}}',
        )
        with (
            patch(
                "doco_meta_catalog.connection.publication.scope",
                return_value={"scope_revision": "exact-scope"},
            ),
            patch(
                "doco_meta_catalog.connection.frappe.db.get_value", return_value=row
            ) as read,
            patch(
                "doco_meta_catalog.connection.now_datetime",
                return_value=frappe.utils.get_datetime("2026-01-03 00:00:00"),
            ),
        ):
            self.assertEqual(latest(None)["state"], "Stale")
            self.assertEqual(read.call_args.args[1], {"scope_revision": "exact-scope"})
