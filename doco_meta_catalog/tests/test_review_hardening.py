"""Bench-free guards: catalog publication never breaks unrelated business transactions."""
import unittest
from unittest.mock import MagicMock, patch

import frappe

from doco_meta_catalog import catalog_pricing, publication, sync


class _Doc(dict):
    def __init__(self, **values):
        super().__init__(**values)
        self.name = values.get("name", "WM-test")
        self.doctype = values.get("doctype", "WhatsApp Message")
        self.modified = values.get("modified", "2026-01-01 00:00:00")
        self.item_code = values.get("item_code")


class TestStockEventsNeverBlock(unittest.TestCase):
    settings = frappe._dict(enabled=1, catalog_id="not-numeric", whatsapp_account="WA-1")

    def test_invalid_binding_is_not_scoped_in_the_business_transaction(self):
        event = _Doc(doctype="Bin", name="BIN-1", item_code="SKU-1")
        with patch.object(sync, "_get_settings", return_value=self.settings), \
             patch.object(publication, "scope", side_effect=ValueError("catalog_id_invalid")) as scope, \
             patch("doco_meta_catalog.sync.frappe") as api:
            api.ValidationError = frappe.ValidationError
            api.get_all.return_value = []
            sync.queue_price_or_stock_sync(event, "on_update")
        scope.assert_not_called()
        api.enqueue.assert_called_once()

    def test_refresh_jobs_coalesce_per_item_and_binding(self):
        jobs = []
        with patch.object(sync, "_get_settings", return_value=self.settings), \
             patch("doco_meta_catalog.sync.frappe") as api:
            api.get_all.return_value = []
            api.enqueue.side_effect = lambda *a, **kw: jobs.append(kw)
            for doctype, method in (("Bin", "on_update"), ("Stock Ledger Entry", "after_insert")):
                sync.queue_price_or_stock_sync(_Doc(doctype=doctype, name=doctype, item_code="SKU-1"), method)
        self.assertEqual(len({job["job_id"] for job in jobs}), 1)
        self.assertTrue(all(job["deduplicate"] and job["enqueue_after_commit"] for job in jobs))
        self.assertNotIn("expected_scope", jobs[0])

    def test_hook_errors_are_logged_not_raised(self):
        with patch.object(sync, "_get_settings", side_effect=ValueError("bad settings")), \
             patch("doco_meta_catalog.sync.frappe.log_error") as logged:
            sync.queue_document_stock_sync(_Doc(doctype="Sales Order", name="SO-1"), "on_submit")
        logged.assert_called_once()

    def test_bin_has_one_refresh_event(self):
        from doco_meta_catalog import hooks

        self.assertEqual(set(hooks.doc_events["Bin"]), {"on_update"})


class TestWorkerOutcomes(unittest.TestCase):
    settings = frappe._dict(enabled=1, catalog_id="123", whatsapp_account="WA-1", default_currency="USD")

    def test_never_published_item_is_not_deleted(self):
        with patch.object(sync, "_get_settings", return_value=self.settings), \
             patch.object(sync, "_build_payloads", return_value=([], [])), \
             patch.object(publication, "known_items", return_value=["OTHER"]), \
             patch.object(sync, "_post_items_batch") as post:
            result = sync.push_one("SKU-NEW")
        self.assertEqual(result["reason_code"], "never_published")
        post.assert_not_called()

    def test_published_item_that_lost_eligibility_is_deleted(self):
        with patch.object(sync, "_get_settings", return_value=self.settings), \
             patch.object(sync, "_build_payloads", return_value=([], [])), \
             patch.object(publication, "known_items", return_value=["SKU-1"]), \
             patch.object(sync, "_post_items_batch", return_value={"state": "Queued"}) as post:
            sync.push_one("SKU-1")
        self.assertEqual(post.call_args.args[1], [{"method": "DELETE", "data": {"id": "SKU-1"}}])

    def test_price_blocker_is_a_result_not_a_failed_job(self):
        with patch.object(sync, "_get_settings", return_value=self.settings), \
             patch.object(sync, "_build_payloads", side_effect=frappe.ValidationError("Automatic pricing rules")), \
             patch.object(sync, "_post_items_batch") as post:
            result = sync.push_one("SKU-1")
        self.assertEqual(result["state"], "Blocked")
        post.assert_not_called()

    def test_sweep_does_not_let_waiting_rows_starve_ready_ones(self):
        calls = []

        def get_all(doctype, filters=None, **kwargs):
            calls.append(filters)
            if filters.get("reason_code") == "prior_publication_pending":
                return [frappe._dict(name="waiting")]
            if "reason_code" in filters:
                return [frappe._dict(name="ready")]
            return []

        with patch.object(publication, "_worker_only"), \
             patch("doco_meta_catalog.publication.frappe.get_all", side_effect=get_all), \
             patch.object(publication, "submit") as submit, patch.object(publication, "poll"):
            publication.process_pending()
        self.assertEqual([c.args[0] for c in submit.call_args_list], ["ready", "waiting"])


class TestScopedPricingRules(unittest.TestCase):
    def run_rules(self, rule, children):
        api = MagicMock()
        api.db.get_values.side_effect = lambda doctype, **kw: {
            "Pricing Rule": [frappe._dict(name="PR-1", valid_from=None, valid_upto=None, **rule)],
            "Item": [frappe._dict(name="SKU-1", variant_of="TPL-1", item_group="Phones", brand="B1")],
        }[doctype]
        api.db.get_value.return_value = frappe._dict(lft=5, rgt=6)
        api.get_all.side_effect = lambda doctype, **kw: (
            ["Phones", "All Item Groups"] if doctype == "Item Group" else children)
        with patch.object(catalog_pricing, "frappe", api):
            return catalog_pricing._applicable_rules(["SKU-1"], "2026-01-01")

    def test_unrelated_item_rule_does_not_block_catalog(self):
        self.assertEqual(self.run_rules({"apply_on": "Item Code"}, ["OTHER"]), [])
        self.assertEqual(self.run_rules({"apply_on": "Brand"}, ["B2"]), [])

    def test_rules_on_item_template_group_ancestor_or_transaction_apply(self):
        self.assertEqual(self.run_rules({"apply_on": "Item Code"}, ["TPL-1"]), ["PR-1"])
        self.assertEqual(self.run_rules({"apply_on": "Item Group"}, ["All Item Groups"]), ["PR-1"])
        self.assertEqual(self.run_rules({"apply_on": "Transaction"}, []), ["PR-1"])

    def test_missing_catalog_currency_is_explicit(self):
        with patch("doco_meta_catalog.catalog_pricing.frappe.throw", side_effect=frappe.ValidationError) as throw:
            with self.assertRaises(frappe.ValidationError):
                catalog_pricing.catalog_currency(frappe._dict(default_currency=None))
        self.assertIn("currency", throw.call_args.args[0])
