"""Unit tests for the Item -> Meta Commerce Catalog payload mapping.

These are PURE mapping tests: the storefront source-of-truth helpers (price / stock / image)
and the eligible-leaf query are monkeypatched with synthetic data, so the tests run with no
DB fixtures and assert ONLY that ``_build_payloads`` maps a Frappe Item into the correct Meta
items_batch payload — the four parity fixes (publish gate, Item Price, Bin stock, image guard)
plus variant grouping and the category/condition override.
"""

import unittest
from unittest.mock import patch

from doco_meta_catalog import sync


class FakeSettings:
    """Stand-in for the Meta Catalog Settings Single doc."""

    def __init__(self, **kw):
        self.price_markup_percent = kw.get("price_markup_percent", 0)
        self.default_currency = kw.get("default_currency", "MXN")
        self.default_brand = kw.get("default_brand", "Doco")
        self.default_condition = kw.get("default_condition", "new")
        self.image_url_base = kw.get("image_url_base", "https://shop.example.com")
        self.fallback_image_url = kw.get("fallback_image_url", "")
        self._category_map = kw.get("category_map", [])

    def get(self, key):
        if key == "category_map":
            return self._category_map
        return None


def _leaf(name, **kw):
    return {
        "name": name,
        "item_name": kw.get("item_name", name),
        "description": kw.get("description", ""),
        "item_group": kw.get("item_group", "Products"),
        "image": kw.get("image", "/files/x.png"),
        "brand": kw.get("brand", ""),
        "variant_of": kw.get("variant_of", None),
        "stock_uom": kw.get("stock_uom", "Nos"),
    }


def _run(leaves, prices, levels, settings, image_pass=True):
    """Drive _build_payloads with patched storefront helpers. ``image_pass`` simulates the
    storefront image guard: True → echo the raw path (public), False → None (private/signed)."""
    with patch.object(sync, "_eligible_leaves", return_value=leaves), \
         patch.object(sync.sf, "_selling_price_list", return_value="Standard Selling"), \
         patch.object(sync.sf, "_prices", return_value=prices), \
         patch.object(sync.sf, "_stock_levels", return_value=levels), \
         patch.object(sync.sf, "_image_url", side_effect=lambda raw: raw if image_pass else None):
        return sync._build_payloads(None, settings)


class TestBuildPayloads(unittest.TestCase):
    def test_published_priced_in_stock(self):
        reqs, skipped = _run(
            [_leaf("IT-A", item_name="Cable USB-C", description="<b>Carga rápida</b>", brand="Anker")],
            {"IT-A": 199.0},
            {"IT-A": "in"},
            FakeSettings(),
        )
        self.assertEqual(skipped, [])
        self.assertEqual(len(reqs), 1)
        d = reqs[0]["data"]
        self.assertEqual(reqs[0]["method"], "UPDATE")
        self.assertEqual(d["id"], "IT-A")  # retailer_id == item_code
        self.assertEqual(d["title"], "Cable USB-C")
        self.assertEqual(d["description"], "Carga rápida")  # HTML stripped
        self.assertEqual(d["price"], 19900)  # minor units, from Item Price (NOT standard_rate)
        self.assertEqual(d["currency"], "MXN")
        self.assertEqual(d["availability"], "in stock")
        self.assertEqual(d["condition"], "new")
        self.assertEqual(d["brand"], "Anker")
        self.assertEqual(d["link"], "https://shop.example.com/shop/IT-A")  # Meta field is 'link'
        self.assertEqual(d["image_link"], "https://shop.example.com/files/x.png")
        self.assertNotIn("item_group_id", d)

    def test_low_stock_is_in_stock(self):
        reqs, _ = _run([_leaf("IT-A")], {"IT-A": 10.0}, {"IT-A": "low"}, FakeSettings())
        self.assertEqual(reqs[0]["data"]["availability"], "in stock")

    def test_out_of_stock(self):
        reqs, _ = _run([_leaf("IT-A")], {"IT-A": 10.0}, {"IT-A": "out"}, FakeSettings())
        self.assertEqual(reqs[0]["data"]["availability"], "out of stock")

    def test_unpriced_item_skipped(self):
        reqs, skipped = _run([_leaf("IT-A")], {}, {"IT-A": "in"}, FakeSettings())
        self.assertEqual(reqs, [])
        self.assertEqual(skipped, [{"code": "IT-A", "reason": "no Item Price in selling price list"}])

    def test_private_image_skipped_without_fallback(self):
        reqs, skipped = _run([_leaf("IT-A")], {"IT-A": 10.0}, {"IT-A": "in"}, FakeSettings(), image_pass=False)
        self.assertEqual(reqs, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("no public image", skipped[0]["reason"])

    def test_private_image_uses_fallback(self):
        reqs, skipped = _run(
            [_leaf("IT-A")], {"IT-A": 10.0}, {"IT-A": "in"},
            FakeSettings(fallback_image_url="https://cdn.example.com/ph.png"), image_pass=False,
        )
        self.assertEqual(skipped, [])
        self.assertEqual(reqs[0]["data"]["image_link"], "https://cdn.example.com/ph.png")

    def test_absolute_image_kept(self):
        reqs, _ = _run(
            [_leaf("IT-A", image="https://m.example.com/p.jpg")],
            {"IT-A": 10.0}, {"IT-A": "in"}, FakeSettings(),
        )
        self.assertEqual(reqs[0]["data"]["image_link"], "https://m.example.com/p.jpg")

    def test_variant_grouping(self):
        reqs, _ = _run(
            [_leaf("IT-A-RED", variant_of="IT-A")],
            {"IT-A-RED": 50.0}, {"IT-A-RED": "in"}, FakeSettings(),
        )
        self.assertEqual(reqs[0]["data"]["item_group_id"], "IT-A")

    def test_markup_applied(self):
        reqs, _ = _run([_leaf("IT-A")], {"IT-A": 100.0}, {"IT-A": "in"}, FakeSettings(price_markup_percent=10))
        self.assertEqual(reqs[0]["data"]["price"], 11000)  # 100 * 1.10 * 100

    def test_category_override(self):
        cmap = [{"item_group": "Seminuevos", "condition": "refurbished", "google_product_category": "267"}]
        reqs, _ = _run(
            [_leaf("IT-A", item_group="Seminuevos")],
            {"IT-A": 100.0}, {"IT-A": "in"},
            FakeSettings(category_map=cmap),
        )
        d = reqs[0]["data"]
        self.assertEqual(d["condition"], "refurbished")  # group override beats default 'new'
        self.assertEqual(d["google_product_category"], "267")

    def test_unmapped_group_uses_default_condition(self):
        cmap = [{"item_group": "Seminuevos", "condition": "refurbished"}]
        reqs, _ = _run(
            [_leaf("IT-A", item_group="Accesorios")],
            {"IT-A": 100.0}, {"IT-A": "in"},
            FakeSettings(default_condition="new", category_map=cmap),
        )
        self.assertEqual(reqs[0]["data"]["condition"], "new")
        self.assertNotIn("google_product_category", reqs[0]["data"])


if __name__ == "__main__":
    unittest.main()
