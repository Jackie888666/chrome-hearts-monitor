import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor


SAMPLE_HTML = """
<html><body>
  <div class="product productType-standard" data-pid="SKU001">
    <span class="product-metadata d-none" data-pid="SKU001"
      data-name="NEW CROSS TEE" data-price="$550.00"
      data-category="Clothing"></span>
    <div class="product-tile">
      <a class="pdp-link-image hover" href="/clothing/new-cross-tee/SKU001.html">
        <img class="tile-image" src="/dw/image/catalog/SKU001.png?sw=800&amp;sh=1000"
          alt="NEW CROSS TEE">
      </a>
      <a class="soldout">OUT OF STOCK</a>
    </div>
  </div>
  <div class="product productType-master" data-pid="SKU002">
    <span class="product-metadata d-none" data-pid="SKU002"
      data-name="BOXER BRIEF" data-price="" data-category="Underwear"></span>
    <div class="product-tile">
      <a class="pdp-link-image" href="/underwear/boxer/SKU002.html?size=S">
        <img class="tile-image" src="https://cdn.example/SKU002.png" alt="BOXER BRIEF">
      </a>
      <button class="swatch-attribute" data-swatchid="BLK"></button>
      <button class="swatch-attribute" data-swatchid="WHT"></button>
      <div class="price"><span class="range">$85 - $110</span></div>
    </div>
  </div>
</body></html>
"""


class ParserTests(unittest.TestCase):
    def test_extracts_products(self):
        products = monitor.parse_products("https://www.chromehearts.com/clothing", SAMPLE_HTML)
        self.assertEqual([item["id"] for item in products], ["SKU001", "SKU002"])
        self.assertEqual(products[0]["name"], "NEW CROSS TEE")
        self.assertEqual(products[0]["price"], "$550")
        self.assertFalse(products[0]["available"])
        self.assertEqual(
            products[0]["url"],
            "https://www.chromehearts.com/clothing/new-cross-tee/SKU001.html",
        )
        self.assertIn("sw=600", products[0]["image"])
        self.assertIn("sh=750", products[0]["image"])
        self.assertEqual(products[1]["price"], "$85 - $110")
        self.assertEqual(products[1]["variants"], ["BLK", "WHT"])

    def test_discovers_only_safe_top_level_pages(self):
        page = """
        <a href="/scents">Scents</a>
        <a href="https://www.chromehearts.com/socks/">Socks</a>
        <a href="/locations.html">Locations</a>
        <a href="/cart">Cart</a>
        <a href="/scents/item/SKU.html">Product</a>
        <a href="https://example.com/shop">Other</a>
        """
        self.assertEqual(
            monitor.discover_category_urls(page),
            {
                "https://www.chromehearts.com/scents",
                "https://www.chromehearts.com/socks",
            },
        )


class StateTests(unittest.TestCase):
    def setUp(self):
        parsed = monitor.parse_products("https://www.chromehearts.com/clothing", SAMPLE_HTML)
        self.current = {item["id"]: item for item in parsed}

    def test_new_and_restock_events(self):
        initial = {"SKU001": self.current["SKU001"]}
        state = monitor.build_initial_state(initial, "2026-07-01T00:00:00Z")
        events = monitor.detect_events(state, self.current, notify_restocks=False)
        self.assertEqual([(event["type"], event["product"]["id"]) for event in events], [("new", "SKU002")])

        current = json.loads(json.dumps(self.current))
        current["SKU001"]["available"] = True
        events = monitor.detect_events(state, current, notify_restocks=True)
        self.assertEqual(
            {(event["type"], event["product"]["id"]) for event in events},
            {("new", "SKU002"), ("restock", "SKU001")},
        )

    def test_failed_new_notification_remains_unknown(self):
        initial = {"SKU001": self.current["SKU001"]}
        state = monitor.build_initial_state(initial, "2026-07-01T00:00:00Z")
        monitor.merge_state(
            state,
            self.current,
            accepted_event_keys=set(),
            attempted_event_keys={("new", "SKU002")},
            catalog_complete=True,
            now="2026-07-02T00:00:00Z",
        )
        self.assertNotIn("SKU002", state["products"])

        monitor.merge_state(
            state,
            self.current,
            accepted_event_keys={("new", "SKU002")},
            attempted_event_keys={("new", "SKU002")},
            catalog_complete=True,
            now="2026-07-02T00:01:00Z",
        )
        self.assertIn("SKU002", state["products"])

    def test_restock_state_updates_when_notifications_are_disabled(self):
        initial = {"SKU001": self.current["SKU001"]}
        state = monitor.build_initial_state(initial, "2026-07-01T00:00:00Z")
        current = json.loads(json.dumps(initial))
        current["SKU001"]["available"] = True
        monitor.merge_state(
            state,
            current,
            accepted_event_keys=set(),
            attempted_event_keys=set(),
            catalog_complete=True,
            now="2026-07-02T00:00:00Z",
        )
        self.assertTrue(state["products"]["SKU001"]["available"])

    def test_state_round_trip(self):
        state = monitor.build_initial_state(self.current, "2026-07-01T00:00:00Z")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            monitor.save_state(path, state)
            self.assertEqual(monitor.load_state(path), state)


class BarkTests(unittest.TestCase):
    def test_copied_bark_url_is_parsed(self):
        with patch.dict(os.environ, {"BARK_URL": "https://api.day.app/secret-key/"}, clear=True):
            self.assertEqual(
                monitor.bark_config(),
                ("https://api.day.app/push", "secret-key"),
            )


if __name__ == "__main__":
    unittest.main()
