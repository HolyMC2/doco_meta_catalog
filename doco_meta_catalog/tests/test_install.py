"""Pure lifecycle decisions; native schema installation runs in integration CI."""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from doco_meta_catalog import hooks
from doco_meta_catalog.install import ensure_catalog_schema


class TestCatalogSchema(unittest.TestCase):
    def setUp(self):
        self.field = None
        self.column = False
        self.api = Mock()
        self.api.get_meta.return_value.get_field.side_effect = lambda name: self.field
        self.api.db.has_column.side_effect = lambda doctype, name: self.column
        self.api.throw.side_effect = RuntimeError
        self.owner_patch = Mock(side_effect=self.install_field)
        self.api.get_attr.return_value = self.owner_patch
        self.stub = patch.dict(sys.modules, {"frappe": self.api})
        self.stub.start()
        self.addCleanup(self.stub.stop)

    def install_field(self):
        self.field = SimpleNamespace(fieldtype="Check")
        self.column = True

    def test_missing_gate_uses_only_canonical_patch_and_is_idempotent(self):
        ensure_catalog_schema()
        ensure_catalog_schema()
        self.api.get_attr.assert_called_once_with(
            "doco.patches.v0_0.add_publish_on_web_to_item.execute"
        )
        self.owner_patch.assert_called_once_with()
        self.api.db.commit.assert_not_called()
        self.api.db.set_value.assert_not_called()
        self.api.db.set_single_value.assert_not_called()

    def test_existing_gate_never_rewrites_configuration(self):
        self.install_field()
        ensure_catalog_schema()
        self.api.get_attr.assert_not_called()
        self.api.clear_cache.assert_not_called()

    def test_incompatible_existing_field_fails_without_replacement(self):
        self.field = SimpleNamespace(fieldtype="Data")
        self.column = True
        with self.assertRaises(RuntimeError):
            ensure_catalog_schema()
        self.api.get_attr.assert_not_called()

    def test_missing_physical_column_must_be_verified_after_owner_patch(self):
        self.field = SimpleNamespace(fieldtype="Check")
        self.owner_patch.side_effect = None
        with self.assertRaises(RuntimeError):
            ensure_catalog_schema()
        self.owner_patch.assert_called_once_with()

    def test_owner_patch_failure_is_not_hidden(self):
        self.owner_patch.side_effect = ValueError("schema migration failed")
        with self.assertRaisesRegex(ValueError, "schema migration failed"):
            ensure_catalog_schema()

    def test_both_native_schema_lifecycle_hooks_are_registered(self):
        self.assertEqual(
            hooks.after_install, "doco_meta_catalog.install.ensure_catalog_schema"
        )
        self.assertEqual(hooks.after_migrate, hooks.after_install)
        self.assertNotIn(hooks.after_install, hooks.before_request)
