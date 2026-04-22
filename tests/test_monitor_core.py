from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor_core import DEFAULT_CONFIG, find_restocked_products, parse_products


class ParseProductsTests(unittest.TestCase):
    def config(self) -> dict:
        config = DEFAULT_CONFIG.copy()
        config["url"] = "https://app.vmiss.com/store/us-los-angeles-tri"
        return config

    def whmcs_product_html(self, stock_text: str) -> str:
        return f"""
        <html>
          <body>
            <div class="package">
              <h3>US.LA.TRI.Basic</h3>
              <span>$5.00 CAD Monthly</span>
              <span>{stock_text}</span>
              <a href="/store/us-los-angeles-tri/basic">Order Now</a>
            </div>
          </body>
        </html>
        """

    def test_whmcs_page_with_recaptcha_script_is_not_blocked(self) -> None:
        html = """
        <html>
          <head>
            <script>
              var whmcsBaseUrl = "";
              var recaptchaSiteKey = "";
            </script>
          </head>
          <body>
            <div class="package">
              <h3>US.LA.TRI.Basic</h3>
              <span>$5.00 CAD Monthly</span>
              <span>0 Available</span>
              <a href="/store/us-los-angeles-tri/basic">Order Now</a>
            </div>
            <div class="package">
              <h3>US.LA.TRI.Ultra</h3>
              <span>$60.00 CAD Monthly</span>
              <a href="/store/us-los-angeles-tri/ultra">Order Now</a>
            </div>
          </body>
        </html>
        """

        products = parse_products(html, self.config())

        self.assertEqual(len(products), 2)
        self.assertEqual(products[0].title, "US.LA.TRI.Basic")
        self.assertEqual(products[0].stock, 0)
        self.assertFalse(products[0].available)
        self.assertEqual(products[1].title, "US.LA.TRI.Ultra")
        self.assertTrue(products[1].available)

    def test_security_challenge_without_products_still_raises(self) -> None:
        html = """
        <html>
          <head><title>Just a moment...</title></head>
          <body>Checking your browser before accessing the site.</body>
        </html>
        """

        with self.assertRaisesRegex(RuntimeError, "Security verification page detected"):
            parse_products(html, self.config())

    def test_plain_recaptcha_variable_without_products_returns_empty_list(self) -> None:
        html = """
        <html>
          <head><script>var recaptchaSiteKey = "";</script></head>
          <body>No products here.</body>
        </html>
        """

        self.assertEqual(parse_products(html, self.config()), [])

    def test_product_key_is_stable_when_stock_changes(self) -> None:
        out_of_stock = parse_products(self.whmcs_product_html("0 Available"), self.config())[0]
        in_stock = parse_products(self.whmcs_product_html("10 Available"), self.config())[0]

        self.assertEqual(out_of_stock.key, in_stock.key)

    def test_restock_alerts_for_new_untracked_product_when_stock_reaches_threshold(self) -> None:
        low_stock_product = parse_products(self.whmcs_product_html("2 Available"), self.config())[0]
        threshold_stock_product = parse_products(self.whmcs_product_html("3 Available"), self.config())[0]

        self.assertEqual(find_restocked_products([low_stock_product], {}), [])
        self.assertEqual(find_restocked_products([threshold_stock_product], {}), [threshold_stock_product])

    def test_restock_alerts_for_untracked_available_product_after_blank_page(self) -> None:
        product = parse_products(self.whmcs_product_html("2 Available"), self.config())[0]

        self.assertEqual(
            find_restocked_products(
                [product],
                {},
                alert_untracked_available_products=True,
            ),
            [product],
        )

    def test_restock_alerts_for_new_untracked_product_with_unknown_stock(self) -> None:
        product = parse_products(self.whmcs_product_html("Available"), self.config())[0]

        self.assertIsNone(product.stock)
        self.assertEqual(find_restocked_products([product], {}), [product])

    def test_restock_alert_when_previous_state_was_unavailable_or_stock_increases(self) -> None:
        product = parse_products(self.whmcs_product_html("10 Available"), self.config())[0]

        self.assertEqual(
            find_restocked_products(
                [product],
                {
                    product.key: {
                        "available": True,
                        "stock": 9,
                        "title": product.title,
                        "price": product.price,
                        "purchase_url": product.purchase_url,
                        "restock_notified": True,
                    }
                },
            ),
            [],
        )
        self.assertEqual(
            find_restocked_products(
                [product],
                {
                    product.key: {
                        "available": True,
                        "stock": 7,
                        "title": product.title,
                        "price": product.price,
                        "purchase_url": product.purchase_url,
                        "restock_notified": True,
                    }
                },
            ),
            [product],
        )
        self.assertEqual(
            find_restocked_products(
                [product],
                {
                    product.key: {
                        "available": False,
                        "stock": 0,
                        "title": product.title,
                        "price": product.price,
                        "purchase_url": product.purchase_url,
                        "restock_notified": False,
                    }
                },
            ),
            [product],
        )

        self.assertEqual(
            find_restocked_products(
                [product],
                {
                    product.key: {
                        "available": False,
                        "stock": 0,
                        "title": product.title,
                        "price": product.price,
                        "purchase_url": product.purchase_url,
                        "restock_notified": True,
                    }
                },
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
