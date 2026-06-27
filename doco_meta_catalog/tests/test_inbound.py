"""Tests for inbound WhatsApp order -> draft Sales Order.

Two parts: (1) the async pickoff (doco_meta_catalog.inbound) — only INBOUND `order` rows enqueue,
and the worker rebuilds the Meta payload + calls the handler trusted; (2) handle_order_message —
server re-pricing (never the buyer price), publish_on_web sellable gate, qty/line caps, atomic
claim+SO idempotency, phone canon, DRAFT-only. Frappe writes + storefront helpers are mocked.
"""

import unittest
from unittest.mock import patch

from doco_meta_catalog import inbound, wa_helpers


# ---------------- async pickoff ----------------


class _Doc:
    def __init__(self, **kw):
        self._d = kw
        self.name = kw.get("name", "WM-1")

    def get(self, k):
        return self._d.get(k)


class TestInboundPickoff(unittest.TestCase):
    def test_enqueues_for_incoming_order(self):
        with patch.object(inbound.frappe, "enqueue") as eq:
            inbound.on_whatsapp_message(_Doc(content_type="order", type="Incoming"))
            eq.assert_called_once()

    def test_skips_non_order(self):
        with patch.object(inbound.frappe, "enqueue") as eq:
            inbound.on_whatsapp_message(_Doc(content_type="text", type="Incoming"))
            eq.assert_not_called()

    def test_skips_outgoing_echo(self):
        with patch.object(inbound.frappe, "enqueue") as eq:
            inbound.on_whatsapp_message(_Doc(content_type="order", type="Outgoing"))
            eq.assert_not_called()

    def test_process_order_builds_payload_and_calls_handler_trusted(self):
        row = {
            "content_type": "order",
            "message_id": "wamid.Z",
            "from": "+5216691234567",
            "product_catalog_json": '{"product_items":[{"product_retailer_id":"A","quantity":1}],"text":"hola"}',
        }
        with patch.object(inbound.frappe, "set_user"), \
             patch.object(inbound.frappe.db, "get_value", return_value=row), \
             patch.object(wa_helpers, "handle_order_message") as h:
            inbound.process_order("WM-1")
            args, kw = h.call_args
            self.assertTrue(kw.get("trusted"))
            self.assertEqual(args[0]["id"], "wamid.Z")
            self.assertEqual(args[0]["order"]["product_items"][0]["product_retailer_id"], "A")


# ---------------- order -> SO ----------------


def _order_msg(items, frm="+5216691234567", mid="wamid.X", text=None):
    o = {"product_items": items}
    if text:
        o["text"] = text
    return {"id": mid, "from": frm, "order": o}


class FakeSO:
    def __init__(self):
        self.items = []
        self.name = "SO-NEW"
        self.comments = []

    def append(self, table, row):
        self.items.append(row)

    def insert(self, **k):
        pass

    def add_comment(self, *a, **k):
        self.comments.append(k.get("text"))


class TestHandleOrder(unittest.TestCase):
    def _run(self, msg, eligible, prices, dup=False, trusted=True):
        fake = FakeSO()
        with patch.object(wa_helpers.sync, "_eligible_leaves", return_value=[{"name": c} for c in eligible]), \
             patch.object(wa_helpers._sf, "_selling_price_list", return_value="PL"), \
             patch.object(wa_helpers._sf, "_prices", return_value=prices), \
             patch.object(wa_helpers, "_find_or_create_customer", return_value="CUST-1") as fc, \
             patch.object(wa_helpers, "_claim_order", return_value=(not dup)), \
             patch.object(wa_helpers.frappe.db, "commit"), \
             patch.object(wa_helpers.frappe.db, "rollback"), \
             patch.object(wa_helpers.frappe.db, "set_value"), \
             patch.object(wa_helpers.frappe, "new_doc", return_value=fake):
            res = wa_helpers.handle_order_message(msg, "Acct", trusted=trusted)
        return res, fake, fc

    def test_untrusted_raises(self):
        with self.assertRaises(Exception):
            wa_helpers.handle_order_message(
                _order_msg([{"product_retailer_id": "A", "quantity": 1, "item_price": 1}]),
                trusted=False,
            )

    def test_reprice_ignores_payload_price(self):
        msg = _order_msg([{"product_retailer_id": "A", "quantity": 2, "item_price": 1}])  # buyer claims 1
        res, fake, _ = self._run(msg, eligible=["A"], prices={"A": 100.0})
        self.assertEqual(res, "SO-NEW")
        self.assertEqual(len(fake.items), 1)
        self.assertEqual(fake.items[0]["item_code"], "A")
        self.assertEqual(fake.items[0]["qty"], 2)
        self.assertEqual(fake.items[0]["rate"], 100.0)  # SERVER price, not the payload's 1
        self.assertEqual(fake.docstatus, 0)  # DRAFT

    def test_non_sellable_skipped(self):
        msg = _order_msg([{"product_retailer_id": "A", "quantity": 1}, {"product_retailer_id": "B", "quantity": 1}])
        _, fake, _ = self._run(msg, eligible=["A"], prices={"A": 100.0})  # B not published/sellable
        self.assertEqual([i["item_code"] for i in fake.items], ["A"])

    def test_unpriced_skipped_no_customer(self):
        msg = _order_msg([{"product_retailer_id": "A", "quantity": 1}])
        res, _, fc = self._run(msg, eligible=["A"], prices={})  # eligible but unpriced
        self.assertIsNone(res)
        fc.assert_not_called()

    def test_qty_over_cap_capped(self):
        msg = _order_msg([{"product_retailer_id": "A", "quantity": 100000}])
        _, fake, _ = self._run(msg, eligible=["A"], prices={"A": 10.0})
        self.assertEqual(fake.items[0]["qty"], 999)  # summed qty capped at _MAX_QTY, not dropped

    def test_idempotent_replay(self):
        # the claim-insert hits a duplicate (UNIQUE wa_msg_id) → already ingested → no SO
        msg = _order_msg([{"product_retailer_id": "A", "quantity": 1}])
        res, _, fc = self._run(msg, eligible=["A"], prices={"A": 10.0}, dup=True)
        self.assertIsNone(res)
        fc.assert_not_called()

    def test_phone_canonicalized(self):
        msg = _order_msg([{"product_retailer_id": "A", "quantity": 1}], frm="+52 (669) 153-0561")
        res, _, fc = self._run(msg, eligible=["A"], prices={"A": 10.0})
        self.assertEqual(res, "SO-NEW")
        self.assertEqual(fc.call_args[0][0], "+526691530561")  # digits-only canonical form

    def test_bad_from_rejected(self):
        msg = _order_msg([{"product_retailer_id": "A", "quantity": 1}], frm="not-a-phone")
        res, _, fc = self._run(msg, eligible=["A"], prices={"A": 10.0})
        self.assertIsNone(res)
        fc.assert_not_called()

    def test_empty_order_no_customer(self):
        res, _, fc = self._run(_order_msg([]), eligible=["A"], prices={"A": 10.0})
        self.assertIsNone(res)
        fc.assert_not_called()

    def test_blank_msg_id_rejected(self):
        msg = _order_msg([{"product_retailer_id": "A", "quantity": 1}], mid="")
        res, _, fc = self._run(msg, eligible=["A"], prices={"A": 10.0})
        self.assertIsNone(res)
        fc.assert_not_called()

    def test_duplicate_lines_merged_and_capped(self):
        msg = _order_msg([
            {"product_retailer_id": "A", "quantity": 600},
            {"product_retailer_id": "A", "quantity": 600},
        ])
        _, fake, _ = self._run(msg, eligible=["A"], prices={"A": 10.0})
        self.assertEqual(len(fake.items), 1)          # merged into one line
        self.assertEqual(fake.items[0]["qty"], 999)   # 1200 capped to 999

    def test_infinity_qty_skips_line_not_order(self):
        msg = _order_msg([
            {"product_retailer_id": "A", "quantity": float("inf")},
            {"product_retailer_id": "B", "quantity": 1},
        ])
        _, fake, _ = self._run(msg, eligible=["A", "B"], prices={"A": 10.0, "B": 5.0})
        self.assertEqual([i["item_code"] for i in fake.items], ["B"])  # bad line dropped, order survives


if __name__ == "__main__":
    unittest.main()
