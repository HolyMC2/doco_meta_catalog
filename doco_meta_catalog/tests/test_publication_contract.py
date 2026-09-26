"""Pure evidence/transport-boundary contracts; no Frappe, SQL or provider calls."""

import unittest

from doco_meta_catalog import publication_contract as contract


class TestPublicationContract(unittest.TestCase):
    def test_acceptance_is_pending_not_ingestion_or_approval(self):
        state, handles, errors, reason = contract.accepted({"handles": ["h1"]})
        self.assertEqual(
            (state, handles, errors, reason),
            ("Pending", ["h1"], [], "awaiting_batch_result"),
        )
        self.assertEqual(contract.accepted({"success": True})[0], "Unknown")

    def test_immediate_and_delayed_errors_remain_rejections(self):
        state = contract.accepted(
            {
                "handles": ["h1"],
                "validation_status": [{"retailer_id": "A", "errors": ["bad image"]}],
            }
        )[0]
        self.assertEqual(state, "Rejected")
        result = contract.batch_result(
            {
                "data": [
                    {
                        "handle": "h1",
                        "status": "finished",
                        "errors_total_count": 1,
                        "ids_of_invalid_requests": ["A"],
                    }
                ]
            },
            "h1",
        )
        self.assertTrue(result["finished"])
        self.assertTrue(result["rejected"])
        self.assertEqual(result["ids_of_invalid_requests"], ["A"])

    def test_batch_identity_and_unknown_status_cannot_finish(self):
        with self.assertRaises(ValueError):
            contract.batch_result(
                {
                    "data": [
                        {
                            "handle": "different",
                            "status": "finished",
                            "errors_total_count": 0,
                        }
                    ]
                },
                "h1",
            )
        result = contract.batch_result(
            {
                "data": [
                    {
                        "handle": "h1",
                        "status": "provider-new-state",
                        "errors_total_count": 0,
                    }
                ]
            },
            "h1",
        )
        self.assertFalse(result["finished"])
        with self.assertRaises(ValueError):
            contract.batch_result(
                {"data": [{"handle": "h1", "status": "finished"}]}, "h1"
            )

    def test_paging_url_is_never_authority_and_cursor_must_progress(self):
        body = {
            "paging": {
                "next": "https://attacker.invalid/?access_token=secret",
                "cursors": {"after": "opaque"},
            }
        }
        self.assertEqual(contract.next_cursor(body, set()), "opaque")
        with self.assertRaises(ValueError):
            contract.next_cursor(body, {"opaque"})
        with self.assertRaises(ValueError):
            contract.next_cursor(
                {"paging": {"next": "https://attacker.invalid"}}, set()
            )

    def test_fixed_origin_rejects_path_and_url_in_graph_version(self):
        self.assertEqual(
            contract.graph_root("v23.0"), "https://graph.facebook.com/v23.0"
        )
        for value in (
            "https://attacker.invalid",
            "v23.0/other",
            "v23.0?token=x",
            "v23.0#x",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                contract.graph_root(value)

    def test_same_count_different_ids_is_drift_not_success(self):
        expected = [
            {
                "method": "UPDATE",
                "data": {
                    "id": "new",
                    "price": "10.00 USD",
                    "availability": "in stock",
                    "visibility": "published",
                },
            }
        ]
        result = contract.drift(expected, [{"retailer_id": "old"}], complete=True)
        self.assertEqual(
            (result["state"], result["missing"], result["unexpected"]),
            ("Drift", ["new"], ["old"]),
        )

    def test_partial_missing_and_absent_fields_are_unknown(self):
        expected = [{"method": "UPDATE", "data": {"id": "A", "price": "10.00 USD"}}]
        self.assertEqual(
            contract.drift(expected, [], complete=False)["state"], "Unknown"
        )
        self.assertEqual(
            contract.drift(expected, [{"retailer_id": "A"}], complete=True)["state"],
            "Unknown",
        )

    def test_price_stock_change_and_explicit_parity(self):
        data = {
            "price": "10.00 USD",
            "availability": "in stock",
            "visibility": "published",
        }
        expected = [{"method": "UPDATE", "data": {"id": "A", **data}}]
        self.assertEqual(
            contract.drift(expected, [{"retailer_id": "A", **data}], complete=True)[
                "state"
            ],
            "Matched",
        )
        result = contract.drift(
            expected,
            [
                {
                    "retailer_id": "A",
                    **data,
                    "price": "20.00 USD",
                    "availability": "out of stock",
                }
            ],
            complete=True,
        )
        self.assertEqual(
            result["changed"],
            [{"retailer_id": "A", "fields": ["price", "availability"]}],
        )

    def test_bounded_frozen_request_preserves_internal_sku_spaces(self):
        row = {"method": "DELETE", "data": {"id": "Cable azul"}}
        self.assertIn("Cable azul", contract.request_body([row]))
        for rows in (
            [],
            [row, row],
            [row] * 1001,
            [{"method": "DELETE", "data": {"id": "A", "title": "override"}}],
        ):
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                contract.request_body(rows)


if __name__ == "__main__":
    unittest.main()
