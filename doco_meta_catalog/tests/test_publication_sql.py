"""Native SQL durability/diagnostic fixtures; provider and commit checkpoints doubled."""

import json
import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from doco_meta_catalog import catalog_pricing, diagnostics, publication, sync
from doco_meta_catalog.tests.test_commerce import CatalogFixture


class TestPublicationSql(CatalogFixture, unittest.TestCase):
    def queue(self, request_id="fixture-request"):
        settings = frappe.get_doc("Meta Catalog Settings")
        rows, _ = sync._build_payloads([self.item.name], settings)
        return publication.queue(settings, rows, request_id=request_id)["publication"]

    def submit(self, name):
        with patch.object(
            publication,
            "graph_request",
            return_value=(200, {"handles": ["batch-fixture"]}),
        ) as send:
            publication.submit(name)
        send.assert_called_once()
        return frappe.get_doc(publication.DOCTYPE, name)

    def test_native_request_identity_and_frozen_transport_bytes(self):
        name = self.queue()
        self.assertEqual(self.queue(), name)
        self.assertEqual(frappe.db.count(publication.DOCTYPE, {"name": name}), 1)
        original = frappe.get_doc(publication.DOCTYPE, name).request_body
        with patch.object(
            publication,
            "graph_request",
            return_value=(200, {"handles": ["batch-fixture"]}),
        ) as send:
            publication.submit(name)
        self.assertEqual(send.call_args.kwargs["body"], original.encode())
        row = frappe.get_doc(publication.DOCTYPE, name)
        self.assertEqual(row.state, "Pending")
        self.assertEqual(json.loads(row.handles), ["batch-fixture"])
        self.assertEqual(
            json.loads(row.scope_json)["account"]["phone_id"], self.account.phone_id
        )

    def test_timeout_and_interrupted_submission_are_unknown_without_resend(self):
        name = self.queue()
        with patch.object(
            publication, "graph_request", side_effect=publication.requests.Timeout()
        ) as send:
            publication.submit(name)
            publication.submit(name)
        send.assert_called_once()
        self.assertEqual(frappe.get_doc(publication.DOCTYPE, name).state, "Unknown")
        interrupted = self.queue(request_id="interrupted")
        frappe.db.set_value(
            publication.DOCTYPE,
            interrupted,
            {
                "state": "Submitting",
                "lease_until": now_datetime() - timedelta(seconds=1),
            },
        )
        with patch.object(publication, "graph_request") as send:
            publication.submit(interrupted)
        send.assert_not_called()
        self.assertEqual(
            frappe.get_doc(publication.DOCTYPE, interrupted).reason_code,
            "submission_interrupted",
        )

    def test_async_rejection_is_persisted_with_handle_and_item(self):
        name = self.queue()
        self.submit(name)
        status = {
            "data": [
                {
                    "handle": "batch-fixture",
                    "status": "finished",
                    "errors_total_count": 1,
                    "ids_of_invalid_requests": [self.item.name],
                    "errors": [{"message": "image rejected"}],
                }
            ]
        }
        with patch.object(publication, "graph_request", return_value=(200, status)):
            publication.poll(name)
        row = frappe.get_doc(publication.DOCTYPE, name)
        self.assertEqual((row.state, row.poll_complete), ("Rejected", 1))
        self.assertIn(self.item.name, row.evidence)

    def test_unknown_fences_later_automatic_refresh_of_same_sku(self):
        first = self.queue()
        frappe.db.set_value(publication.DOCTYPE, first, "state", "Unknown")
        later = self.queue(request_id="nightly-new-request")
        with patch.object(publication, "graph_request") as send:
            publication.submit(later)
        send.assert_not_called()
        row = frappe.get_doc(publication.DOCTYPE, later)
        self.assertEqual(
            (row.state, row.reason_code), ("Blocked", "prior_publication_unknown")
        )

    def test_manager_reconciliation_is_audited_and_replayed_without_changing_unknown(
        self,
    ):
        first = self.queue()
        frappe.db.set_value(publication.DOCTYPE, first, "state", "Unknown")
        result = publication.reconcile_unknown(
            first, "manager-reviewed", "Reviewed provider diagnostics"
        )
        replay = publication.reconcile_unknown(
            first, "manager-reviewed", "Reviewed provider diagnostics"
        )
        self.assertEqual(result["publication"], replay["publication"])
        self.assertEqual(
            frappe.db.count(publication.RESOLUTION, {"prior_publication": first}), 1
        )
        self.assertEqual(frappe.get_doc(publication.DOCTYPE, first).state, "Unknown")
        self.assertEqual(self.submit(result["publication"]).state, "Pending")
        with self.assertRaises(frappe.ValidationError):
            publication.reconcile_unknown(first, "manager-reviewed", "Changed note")

    def test_reconciliation_refuses_catalog_rebinding(self):
        first = self.queue()
        frappe.db.set_value(publication.DOCTYPE, first, "state", "Unknown")
        frappe.db.set_single_value("Meta Catalog Settings", "catalog_id", "9900098765")
        with self.assertRaises(ValueError):
            publication.reconcile_unknown(
                first, "manager-reviewed", "Checked the old catalog"
            )
        self.assertEqual(
            frappe.db.count(publication.RESOLUTION, {"prior_publication": first}), 0
        )

    def test_account_rebinding_blocks_submission_and_preserves_original_scope(self):
        name = self.queue()
        before = frappe.get_doc(publication.DOCTYPE, name).scope_json
        frappe.db.set_single_value(
            "Meta Catalog Settings", "whatsapp_account", self.new_account().name
        )
        with patch.object(publication, "graph_request") as send:
            publication.submit(name)
        send.assert_not_called()
        row = frappe.get_doc(publication.DOCTYPE, name)
        self.assertEqual((row.state, row.scope_json), ("Blocked", before))

    def test_changed_source_cannot_publish_stale_price(self):
        name = self.queue()
        frappe.db.set_value("Item Price", self.price.name, "price_list_rate", 150)
        with patch.object(publication, "graph_request") as send:
            publication.submit(name)
        send.assert_not_called()
        self.assertEqual(frappe.get_doc(publication.DOCTYPE, name).state, "Blocked")

    def test_full_scan_retains_approved_history_and_partial_absence_is_unknown(self):
        observed = {
            "retailer_id": self.item.name,
            "review_status": "approved",
            "price": "125.00 USD",
            "availability": "in stock",
            "visibility": "published",
            "capability_to_review_status": [{"key": "WHATSAPP", "value": "APPROVED"}],
        }
        with patch.object(
            publication, "graph_request", return_value=(200, {"data": [observed]})
        ):
            first = diagnostics.run_diagnostics()
        self.assertTrue(first["complete"])
        self.assertEqual(
            diagnostics.item_diagnostic(self.item.name)["state"], "Observed"
        )
        with patch.object(publication, "graph_request", return_value=(500, {})):
            second = diagnostics.run_diagnostics()
        self.assertFalse(second["complete"])
        self.assertEqual(
            diagnostics.item_diagnostic(self.item.name)["state"], "Unknown"
        )
        self.assertTrue(
            frappe.db.exists(
                "Meta Catalog Diagnostic",
                {
                    "diagnostic_run": first["diagnostic_run"],
                    "retailer_id": self.item.name,
                },
            )
        )
        frappe.db.set_single_value("Meta Catalog Settings", "catalog_id", "9900099999")
        self.assertEqual(
            diagnostics.item_diagnostic(self.item.name)["reason_code"],
            "no_scoped_snapshot",
        )

    def test_paging_uses_fixed_product_edge_and_after_cursor(self):
        with patch.object(
            publication,
            "graph_request",
            side_effect=[
                (
                    200,
                    {
                        "data": [],
                        "paging": {
                            "next": "https://attacker.invalid",
                            "cursors": {"after": "cursor-one"},
                        },
                    },
                ),
                (200, {"data": []}),
            ],
        ) as get:
            result = diagnostics._fetch(frappe.get_doc("Meta Catalog Settings"))
        self.assertTrue(result["complete"])
        self.assertEqual(get.call_args_list[1].args[2], "products")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["after"], "cursor-one")

    def test_unpublish_queues_removal_and_price_bin_events_wait_for_commit(self):
        name = self.queue()
        frappe.db.set_value("Item", self.item.name, "publish_on_web", 0)
        removal = sync.push_one(self.item.name)
        row = frappe.get_doc(publication.DOCTYPE, removal["publication"])
        self.assertEqual(
            json.loads(row.request_body)["requests"],
            [{"method": "DELETE", "data": {"id": self.item.name}}],
        )
        self.assertIn(
            self.item.name,
            publication.known_items(frappe.get_doc("Meta Catalog Settings")),
        )
        self.assertTrue(frappe.db.exists(publication.DOCTYPE, name))
        for doctype in ("Item Price", "Bin"):
            with self.subTest(doctype=doctype), patch("frappe.enqueue") as enqueue:
                sync.queue_price_or_stock_sync(
                    frappe._dict(
                        doctype=doctype,
                        name="source",
                        item_code=self.item.name,
                        modified=now_datetime(),
                    ),
                    "on_update",
                )
            self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

    def test_stock_events_are_inert_without_enabled_binding(self):
        event = frappe._dict(
            doctype="Bin", name="source", item_code=self.item.name, modified=now_datetime()
        )
        order = frappe._dict(doctype="Sales Order", name="source", modified=now_datetime(),
                             items=[frappe._dict(item_code=self.item.name)])
        with (
            patch.object(sync, "_get_settings", return_value=None),
            patch("frappe.enqueue") as enqueue,
            patch("frappe.get_all", side_effect=AssertionError("Bundle lookup ran")),
        ):
            sync.queue_price_or_stock_sync(event, "on_update")
            sync.queue_document_stock_sync(order, "on_submit")
        enqueue.assert_not_called()

    def test_generic_quote_has_no_elevation_document_insert_or_transaction_reset(self):
        actor = frappe.session.user
        with (
            patch.object(
                frappe.db, "rollback", side_effect=AssertionError("Quote rolled back")
            ),
            patch.object(
                frappe.db, "commit", side_effect=AssertionError("Quote committed")
            ),
            patch("frappe.set_user", side_effect=AssertionError("Quote elevated")),
            patch(
                "frappe.new_doc", side_effect=AssertionError("Quote created a document")
            ),
        ):
            quote = catalog_pricing.quote(
                [self.item.name], frappe.get_doc("Meta Catalog Settings"), lock=True
            )
        self.assertEqual(quote["rates"], {self.item.name: 125})
        self.assertEqual(frappe.session.user, actor)

    def test_automatic_rule_never_silently_falls_back_to_base_catalog_price(self):
        original = frappe.db.get_values

        def rules(doctype, *args, **kwargs):
            if doctype == "Pricing Rule":
                return [
                    frappe._dict(
                        name="controlled-rule", valid_from=None, valid_upto=None
                    )
                ]
            return original(doctype, *args, **kwargs)

        with patch.object(frappe.db, "get_values", side_effect=rules):
            with self.assertRaisesRegex(
                frappe.ValidationError, "Automatic pricing rules"
            ):
                sync._build_payloads(
                    [self.item.name], frappe.get_doc("Meta Catalog Settings"), lock=True
                )

    def test_private_fallback_is_not_published(self):
        settings = frappe.get_doc("Meta Catalog Settings")
        settings.fallback_image_url = "https://example.invalid/private.jpg"
        from doco.docoutils.storefront import _common

        with patch.object(
            _common, "_image_urls", return_value={settings.fallback_image_url: None}
        ):
            self.assertIsNone(sync._public_image({"image": ""}, settings, lock=True))
