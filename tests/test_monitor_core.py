from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor_core import (
    DEFAULT_CONFIG,
    api_headers_from_cookie_header,
    find_restocked_products,
    normalize_product_url,
    parse_api_products,
    parse_products,
)


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

    def test_restock_alerts_for_new_untracked_available_product(self) -> None:
        product = parse_products(self.whmcs_product_html("2 Available"), self.config())[0]

        self.assertEqual(find_restocked_products([product], {}), [product])

    def test_restock_alerts_for_new_untracked_product_with_unknown_stock(self) -> None:
        product = parse_products(self.whmcs_product_html("Available"), self.config())[0]

        self.assertIsNone(product.stock)
        self.assertEqual(find_restocked_products([product], {}), [product])

    def test_restock_alert_when_previous_state_was_unavailable_or_stock_changes(self) -> None:
        product = parse_products(self.whmcs_product_html("10 Available"), self.config())[0]

        self.assertEqual(
            find_restocked_products(
                [product],
                {
                    product.key: {
                        "available": True,
                        "stock": 10,
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
                        "stock": 9,
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

    def test_vmiss_aff_links_keep_same_product_identity(self) -> None:
        self.assertEqual(
            normalize_product_url("https://app.vmiss.com/aff.php?aff=2762&pid=7&utm_source=tg"),
            "https://app.vmiss.com/aff.php?pid=7",
        )

    def test_stock_parses_common_vmiss_and_cn_formats(self) -> None:
        cases = ["1 Available", "Available: 2", "库存：3 台", "剩余 4", "Quantity: 5"]
        for index, stock_text in enumerate(cases, start=1):
            with self.subTest(stock_text=stock_text):
                product = parse_products(self.whmcs_product_html(stock_text), self.config())[0]
                self.assertEqual(product.stock, index)
                self.assertTrue(product.available)

    def test_disabled_or_sold_out_button_is_not_available(self) -> None:
        html = """
        <html><body>
          <div class="package">
            <h3>US.LA.CN2.Basic</h3>
            <span>1 Available</span>
            <a class="btn disabled" href="/cart.php?a=add&pid=7">Order Now</a>
            <span>Sold Out</span>
          </div>
        </body></html>
        """

        product = parse_products(html, self.config())[0]

        self.assertEqual(product.stock, 1)
        self.assertFalse(product.available)
        self.assertEqual(product.status, "out_of_stock")

    def test_api_products_parse_default_paths(self) -> None:
        config = self.config()
        config.update({"url": "https://akile.ai/api/products", "request_backend": "api"})
        data = {
            "data": {
                "items": [
                    {"name": "Akile HK Starter", "stock": 0, "price": "10 USD", "buy_url": "/order/1"},
                    {"name": "Akile JP Basic", "inventory": 3, "amount": "20 USD", "status": "Available", "buy_url": "/order/2"},
                ]
            }
        }

        products = parse_api_products(data, config)

        self.assertEqual(len(products), 2)
        self.assertEqual(products[0].title, "Akile HK Starter")
        self.assertEqual(products[0].stock, 0)
        self.assertFalse(products[0].available)
        self.assertEqual(products[1].title, "Akile JP Basic")
        self.assertEqual(products[1].stock, 3)
        self.assertTrue(products[1].available)
        self.assertEqual(products[1].purchase_url, "https://akile.ai/order/2")

    def test_api_products_parse_custom_paths(self) -> None:
        config = self.config()
        config.update(
            {
                "url": "https://akile.ai/api/v1/plans",
                "request_backend": "api",
                "product_selector": "result.rows",
                "title_selector": "product.title",
                "stock_selector": "meta.left",
                "price_selector": "billing.monthly",
                "button_selector": "enabled",
                "link_selector": "links.checkout",
            }
        )
        data = {
            "result": {
                "rows": [
                    {
                        "product": {"title": "Akile SG NAT"},
                        "meta": {"left": 2},
                        "billing": {"monthly": "$6.00"},
                        "enabled": True,
                        "links": {"checkout": "https://akile.ai/order/sg-nat"},
                    }
                ]
            }
        }

        product = parse_api_products(data, config)[0]

        self.assertEqual(product.title, "Akile SG NAT")
        self.assertEqual(product.stock, 2)
        self.assertEqual(product.price, "$6.00")
        self.assertTrue(product.available)
        self.assertEqual(product.purchase_url, "https://akile.ai/order/sg-nat")

    def test_api_header_parsing_accepts_bearer_or_header_lines(self) -> None:
        self.assertEqual(
            api_headers_from_cookie_header("Bearer token-123"),
            {"Authorization": "Bearer token-123"},
        )
        self.assertEqual(
            api_headers_from_cookie_header("Authorization: Bearer token-123\nX-Test: yes"),
            {"Authorization": "Bearer token-123", "X-Test": "yes"},
        )


if __name__ == "__main__":
    unittest.main()
