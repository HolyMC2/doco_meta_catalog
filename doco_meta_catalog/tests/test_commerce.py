"""Native catalog eligibility. Real Item/Item Price/Settings; no provider effects.

Run only on an isolated full-graph site with ERPNext, Doco, CRM and WhatsApp.
CatalogFixture has no tests: the CRM SQL suite reuses setup without inheriting
or rerunning unrelated test suites. Transaction commits and HTTP are blocked.
"""

import json
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import frappe

from doco_meta_catalog import commerce, wa_helpers


class CatalogFixture:
    def setUp(self):
        super().setUp()
        self.point = "catalog_" + uuid4().hex
        frappe.db.savepoint(self.point)
        self.addCleanup(frappe.db.rollback, save_point=self.point)
        self.addCleanup(frappe.set_user, frappe.session.user)
        frappe.set_user("Administrator")
        for target in (
            "requests.sessions.Session.request",
            "frappe_whatsapp.transport.raw",
            "frappe_whatsapp.transport.api",
            "frappe.sendmail",
        ):
            self.enterContext(
                patch(target, side_effect=AssertionError("No catalog provider effects"))
            )
        self.enterContext(patch("frappe.enqueue"))
        self.enterContext(patch("frappe.publish_realtime"))
        self.enterContext(patch.object(frappe.db, "commit"))
        self.enterContext(patch.object(frappe, "request", None))
        self.enterContext(patch.object(frappe.local, "conf", frappe._dict(frappe.conf)))
        frappe.conf.maintenance_mode = 0
        frappe.conf.meta_catalog_block_outbound = 0
        self.key = uuid4().hex[:12]
        self.account = self.new_account()
        self.peer = "5215550100999"
        # Keep item hooks disabled until real local source records exist.
        frappe.db.set_single_value("Meta Catalog Settings", "enabled", 0)
        self.price_list = frappe.get_doc(
            {
                "doctype": "Price List",
                "price_list_name": "Catalog " + self.key,
                "selling": 1,
                "enabled": 1,
                "currency": "USD",
            }
        ).insert()
        frappe.db.set_single_value(
            "Selling Settings", "selling_price_list", self.price_list.name
        )
        self.item = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": "CAT " + self.key,
                "item_name": "Catalog fixture " + self.key,
                "item_group": "All Item Groups",
                "stock_uom": "Nos",
                "is_stock_item": 0,
                "is_sales_item": 1,
                "publish_on_web": 1,
                "image": "https://example.invalid/catalog.jpg",
            }
        ).insert()
        self.price = frappe.get_doc(
            {
                "doctype": "Item Price",
                "item_code": self.item.name,
                "price_list": self.price_list.name,
                "price_list_rate": 125,
            }
        ).insert()
        frappe.db.set_single_value(
            "Meta Catalog Settings",
            {
                "enabled": 1,
                "catalog_id": "9900012345",
                "whatsapp_account": self.account.name,
                "default_visibility": "published",
                "default_currency": "USD",
                "default_condition": "new",
                "price_markup_percent": 0,
                "image_url_base": "https://example.invalid",
                "fallback_image_url": "",
                "variant_group_attribute": "",
                "variant_color_attribute": "",
            },
        )
        from crm.api import conversations

        self.conversation = conversations.get_or_create(
            "WhatsApp", self.account.phone_id, self.peer
        )
        conversations.apply_control(self.conversation.name, "take", 1, uuid4().hex)
        self.inbound()

    def new_account(self):
        return frappe.get_doc(
            {
                "doctype": "WhatsApp Account",
                "account_name": "catalog-" + uuid4().hex,
                "phone_id": "98" + str(int(uuid4().hex[:12], 16)),
                "app_id": "9900101",
                "business_id": "9900102",
                "status": "Active",
                "mode": "Live",
                "version": "v23.0",
                "url": "https://graph.facebook.com",
                "token": "fictional-never-used",
                "is_default_outgoing": 0,
                "is_default_incoming": 0,
            }
        ).insert(ignore_permissions=True)

    def inbound(self, timestamp=None):
        from frappe_whatsapp.webhook_receipts import record_events

        name = record_events(
            [
                {
                    "provider": "WhatsApp",
                    "account_id": self.account.phone_id,
                    "app_id": self.account.app_id,
                    "event_type": "message",
                    "event_id": uuid4().hex,
                    "payload": {
                        "change": {
                            "value": {
                                "messages": [
                                    {
                                        "from": self.peer,
                                        "timestamp": str(
                                            int(time.time()) - 5
                                            if timestamp is None
                                            else timestamp
                                        ),
                                    }
                                ]
                            }
                        }
                    },
                }
            ]
        )[0]
        frappe.db.set_value("Meta Webhook Receipt", name, "state", "Processed")
        self.inbound_name = name
        return name

    def selection(self, **changes):
        return {
            "kind": "product",
            "products": [self.item.name],
            "body": "Requested product",
            "header": "",
            "footer": "",
            **changes,
        }

    def frozen(self, **changes):
        return commerce.freeze(self.selection(**changes), self.conversation)

    def intent(self):
        payload, source = self.frozen()
        return frappe._dict(
            source_action=source[2], actor_user="Administrator", payload=payload
        )


class TestCatalogCommerce(CatalogFixture, unittest.TestCase):
    def test_real_source_price_publication_and_wire_identity(self):
        rows = commerce.get_products(self.conversation, query=self.key)
        self.assertEqual(
            [(row["item_code"], row["rate"], row["currency"]) for row in rows["items"]],
            [(self.item.name, 125, "USD")],
        )
        payload, source = self.frozen()
        action = json.loads(payload)["interactive"]["action"]
        self.assertEqual(
            action, {"catalog_id": "9900012345", "product_retailer_id": self.item.name}
        )
        self.assertEqual(source[:2], ("WhatsApp Account", self.account.name))
        self.assertLessEqual(len(source[2]), 140)
        self.assertIsNone(
            commerce.dispatch_reason(self.intent(), self.account, json.loads(payload))
        )

    def test_current_unpublished_item_and_staging_catalog_rejected(self):
        for field, value in (("publish_on_web", 0), ("disabled", 1)):
            with self.subTest(field=field):
                frappe.db.set_value("Item", self.item.name, field, value)
                with self.assertRaises(frappe.ValidationError):
                    self.frozen()
                frappe.db.set_value(
                    "Item", self.item.name, field, 1 if field == "publish_on_web" else 0
                )
        frappe.db.set_single_value(
            "Meta Catalog Settings", "default_visibility", "staging"
        )
        with self.assertRaises(frappe.ValidationError):
            self.frozen()

    def test_changed_price_invalidates_frozen_product(self):
        intent = self.intent()
        frappe.db.set_value("Item Price", self.price.name, "price_list_rate", 150)
        self.assertEqual(
            commerce.dispatch_reason(intent, self.account, json.loads(intent.payload)),
            "catalog_configuration_changed",
        )

    def test_wrong_binding_never_chooses_default_account(self):
        other = self.new_account()
        frappe.db.set_value("WhatsApp Account", other.name, "is_default_outgoing", 1)
        for binding in (other.name, ""):
            with self.subTest(binding=binding):
                frappe.db.set_single_value(
                    "Meta Catalog Settings", "whatsapp_account", binding
                )
                with patch.object(
                    wa_helpers,
                    "_outgoing_account",
                    side_effect=AssertionError("Default used"),
                ):
                    with self.assertRaises(frappe.ValidationError):
                        self.frozen()

    def test_changed_catalog_and_account_binding_block_dispatch(self):
        intent = self.intent()
        frappe.db.set_single_value("Meta Catalog Settings", "catalog_id", "9900099999")
        self.assertEqual(
            commerce.dispatch_reason(intent, self.account, json.loads(intent.payload)),
            "catalog_configuration_changed",
        )
        frappe.db.set_single_value(
            "Meta Catalog Settings", "whatsapp_account", self.new_account().name
        )
        self.assertIsNotNone(
            commerce.dispatch_reason(intent, self.account, json.loads(intent.payload))
        )

    def test_selected_item_must_still_be_published_at_dispatch(self):
        intent = self.intent()
        frappe.db.set_value("Item", self.item.name, "publish_on_web", 0)
        self.assertEqual(
            commerce.dispatch_reason(intent, self.account, json.loads(intent.payload)),
            "catalog_item_unavailable",
        )

    def test_legacy_sender_choke_point_fails_closed_without_http(self):
        with self.assertRaises(frappe.PermissionError):
            wa_helpers._post_message(self.account, {"type": "text", "to": self.peer})
