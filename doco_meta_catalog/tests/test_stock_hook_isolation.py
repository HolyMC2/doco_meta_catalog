"""Run the actual stock hooks without a bench, SQL, Redis or provider access.

Only import dependencies are doubled; the queue/commit boundary executes sync.py.
"""

import importlib.util
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


class ValidationError(Exception):
    pass


class CatalogPermissionError(Exception):
    pass


class StockDocument(dict):
    """Native stock documents expose fields through attributes and get()."""

    __getattr__ = dict.__getitem__


class TestStockHookIsolation(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.api.ValidationError = ValidationError
        self.api.PermissionError = CatalogPermissionError
        self.api.whitelist.return_value = lambda fn: fn
        self.api.get_doc.return_value = SimpleNamespace(
            enabled=1, catalog_id="123", whatsapp_account="WA-1"
        )
        self.api.get_all.return_value = []
        self.callbacks = []
        self.api.db.after_commit.add.side_effect = self.callbacks.append
        self.utils = ModuleType("frappe.utils")
        for name in ("flt", "get_url", "strip_html_tags", "today"):
            setattr(self.utils, name, MagicMock())
        self.storefront = ModuleType("doco.docoutils.storefront")
        docoutils = ModuleType("doco.docoutils")
        docoutils.storefront = self.storefront
        outbound = ModuleType("doco_meta_catalog.utils")
        outbound.assert_outbound_allowed = MagicMock()
        self.imports = {
            "frappe": self.api,
            "frappe.utils": self.utils,
            "doco": ModuleType("doco"),
            "doco.docoutils": docoutils,
            "doco.docoutils.storefront": self.storefront,
            "doco_meta_catalog.utils": outbound,
        }
        source = Path(__file__).parents[1] / "sync.py"
        spec = importlib.util.spec_from_file_location("stock_hook_sync", source)
        self.sync = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, self.imports):
            spec.loader.exec_module(self.sync)
        self.doc = StockDocument(
            doctype="Stock Ledger Entry", name="SLE-1", item_code="SKU-1",
            modified="2026-10-02 11:48:31",
        )

    def call_stock_hook(self):
        return self.sync.queue_price_or_stock_sync(self.doc, "after_insert")

    def test_queue_permission_failure_preserves_commit_intent_without_sending(self):
        self.api.enqueue.side_effect = CatalogPermissionError("System Manager required")
        self.assertIsNone(self.call_stock_hook())
        self.api.log_error.assert_called_once_with(
            title="Meta catalog refresh not queued",
            reference_doctype="Stock Ledger Entry", reference_name="SLE-1",
        )
        self.api.cache.hset.assert_not_called()
        self.assertEqual(len(self.callbacks), 1)
        for callback in self.callbacks:
            callback()
        enqueue = self.api.enqueue.call_args.kwargs
        self.api.cache.hset.assert_called_once_with(
            self.sync._DIRTY, enqueue["refresh_key"], 1
        )
        self.assertTrue(enqueue["enqueue_after_commit"])
        self.assertTrue(enqueue["deduplicate"])
        self.imports["doco_meta_catalog.utils"].assert_outbound_allowed.assert_not_called()

    def test_queue_pressure_and_permission_failures_are_nonfatal_if_logging_fails(self):
        for queue_error in (ValidationError, CatalogPermissionError):
            for log_error in (ValidationError, CatalogPermissionError):
                with self.subTest(queue_error=queue_error, log_error=log_error):
                    self.api.enqueue.side_effect = queue_error("Queue unavailable")
                    self.api.log_error.side_effect = log_error("Logging unavailable")
                    self.assertIsNone(self.call_stock_hook())

    def test_rollback_does_not_set_the_dirty_marker(self):
        self.api.enqueue.side_effect = CatalogPermissionError()
        self.call_stock_hook()
        self.callbacks.clear()  # The transaction rolls back instead of running after_commit.
        self.api.cache.hset.assert_not_called()

    def test_database_failure_in_the_hook_still_propagates(self):
        error = sqlite3.OperationalError("Deadlock")
        self.api.enqueue.side_effect = error
        with self.assertRaises(sqlite3.OperationalError) as raised:
            self.call_stock_hook()
        self.assertIs(raised.exception, error)
        self.api.log_error.assert_not_called()

    def test_database_failure_in_error_logging_still_propagates(self):
        self.api.enqueue.side_effect = CatalogPermissionError()
        error = sqlite3.OperationalError("Transaction aborted")
        self.api.log_error.side_effect = error
        with self.assertRaises(sqlite3.OperationalError) as raised:
            self.call_stock_hook()
        self.assertIs(raised.exception, error)

    def test_successful_stock_enqueue_waits_for_commit_and_keeps_scope_deduplication(self):
        for doctype, method in (("Bin", "on_update"), ("Stock Ledger Entry", "after_insert")):
            self.sync.queue_price_or_stock_sync(StockDocument(
                **{**self.doc, "doctype": doctype, "name": doctype}
            ), method)
        jobs = [call.kwargs for call in self.api.enqueue.call_args_list]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["job_id"], jobs[1]["job_id"])
        self.assertTrue(all(job["enqueue_after_commit"] for job in jobs))
        self.api.log_error.assert_not_called()

    def test_disabled_binding_has_no_queue_or_recovery_side_effects(self):
        self.api.get_doc.return_value.enabled = 0
        self.call_stock_hook()
        self.api.enqueue.assert_not_called()
        self.assertEqual(self.callbacks, [])
        self.api.get_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
