"""Tests for the inbound trust boundary: HMAC signature verification + hardened order→SO.

Pure logic tests — frappe doc writes, the sellable-leaf gate, and the price source are mocked,
so these assert ONLY the security-critical behaviour: a bad/missing signature is rejected, an
unverified caller cannot build an order, and every order line is re-priced from Item Price (never
the buyer payload), gated to sellable items, qty/line bounded, idempotent, and left as a DRAFT.
"""

import hashlib
import hmac
import unittest
from unittest.mock import patch

import frappe

from doco_meta_catalog import wa_helpers, webhook


class TestVerifySignature(unittest.TestCase):
    def _sig(self, secret, body):
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_valid(self):
        body = b'{"a":1}'
        self.assertTrue(webhook.verify_signature(body, self._sig("s3cr3t", body), "s3cr3t"))

    def test_tampered_body_rejected(self):
        sig = self._sig("s3cr3t", b'{"a":1}')
        self.assertFalse(webhook.verify_signature(b'{"a":2}', sig, "s3cr3t"))

    def test_wrong_secret_rejected(self):
        body = b'{"a":1}'
        self.assertFalse(webhook.verify_signature(body, self._sig("other", body), "s3cr3t"))

    def test_missing_header_rejected(self):
        self.assertFalse(webhook.verify_signature(b"x", None, "s"))

    def test_missing_secret_rejected(self):
        self.assertFalse(webhook.verify_signature(b"x", "sha256=abc", None))

    def test_bad_prefix_rejected(self):
        self.assertFalse(webhook.verify_signature(b"x", "abc", "s"))

    def test_non_ascii_or_nonhex_header_rejected(self):
        # must return False (never raise TypeError → 500) on a non-hex / non-ASCII digest
        self.assertFalse(webhook.verify_signature(b"x", "sha256=Ã" + "a" * 62, "s"))
        self.assertFalse(webhook.verify_signature(b"x", "sha256=" + "z" * 64, "s"))


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
    def _run(self, msg, eligible, prices, dup=False, verified=True):
        fake = FakeSO()
        with patch.object(wa_helpers.sync, "_eligible_leaves", return_value=[{"name": c} for c in eligible]), \
             patch.object(wa_helpers._sf, "_selling_price_list", return_value="PL"), \
             patch.object(wa_helpers._sf, "_prices", return_value=prices), \
             patch.object(wa_helpers, "_find_or_create_customer", return_value="CUST-1") as fc, \
             patch.object(wa_helpers, "_claim_order", return_value=(not dup)), \
             patch.object(wa_helpers.frappe.db, "commit"), \
             patch.object(wa_helpers.frappe.db, "set_value"), \
             patch.object(wa_helpers.frappe, "new_doc", return_value=fake):
            res = wa_helpers.handle_order_message(msg, "Acct", signature_verified=verified)
        return res, fake, fc

    def test_unverified_raises(self):
        with self.assertRaises(Exception):
            wa_helpers.handle_order_message(
                _order_msg([{"product_retailer_id": "A", "quantity": 1, "item_price": 1}]),
                signature_verified=False,
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
        # same SKU twice; the SUMMED qty is what gets capped at _MAX_QTY (999)
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


# NOTE: the webhook() HTTP entry is exercised LIVE (signed payload → 200, forged → 403), since
# frappe.request is a bound LocalProxy that does not mock cleanly in a unit test. The trust
# boundary is unit-covered by TestVerifySignature (the HMAC logic) above.


if __name__ == "__main__":
    unittest.main()
