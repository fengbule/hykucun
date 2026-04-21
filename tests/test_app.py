from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import telegram_products_to_edit, telegram_text_hash, upsert_products
from monitor_core import Product, telegram_product_card


class UpsertProductsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE products (
                monitor_id INTEGER NOT NULL,
                product_key TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                available INTEGER NOT NULL,
                stock INTEGER,
                price TEXT NOT NULL DEFAULT '',
                purchase_url TEXT NOT NULL DEFAULT '',
                unavailable_since TEXT,
                restock_notified INTEGER NOT NULL DEFAULT 0,
                telegram_chat_id TEXT NOT NULL DEFAULT '',
                telegram_message_id INTEGER,
                telegram_text_hash TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (monitor_id, product_key)
            )
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_upsert_merges_old_dynamic_key_by_purchase_url(self) -> None:
        self.conn.execute(
            """
            INSERT INTO products (
                monitor_id, product_key, title, status, available, stock,
                price, purchase_url, unavailable_since, last_seen_at
            ) VALUES (1, 'old-dynamic-key', 'US.LA.TRI.Basic', 'out_of_stock', 0, 0,
                '$5.00 CAD Monthly', 'https://app.vmiss.com/store/us-los-angeles-tri/basic',
                '2026-04-21 20:00:00', '2026-04-21 20:00:00')
            """
        )
        product = Product(
            key="stable-key",
            title="US.LA.TRI.Basic",
            status="out_of_stock",
            available=False,
            stock=0,
            price="$5.00 CAD Monthly",
            purchase_url="https://app.vmiss.com/store/us-los-angeles-tri/basic",
            button="Order Now",
        )

        upsert_products(self.conn, 1, [product])

        rows = self.conn.execute("SELECT * FROM products").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_key"], "stable-key")
        self.assertEqual(rows[0]["unavailable_since"], "2026-04-21 20:00:00")
        self.assertEqual(rows[0]["restock_notified"], 0)

    def test_upsert_marks_available_stage_and_resets_when_unavailable(self) -> None:
        available_product = Product(
            key="stable-key",
            title="US.LA.TRI.Basic",
            status="in_stock",
            available=True,
            stock=10,
            price="$5.00 CAD Monthly",
            purchase_url="https://app.vmiss.com/store/us-los-angeles-tri/basic",
            button="Order Now",
        )
        unavailable_product = Product(
            key="stable-key",
            title="US.LA.TRI.Basic",
            status="out_of_stock",
            available=False,
            stock=0,
            price="$5.00 CAD Monthly",
            purchase_url="https://app.vmiss.com/store/us-los-angeles-tri/basic",
            button="Order Now",
        )

        upsert_products(self.conn, 1, [available_product])

        row = self.conn.execute("SELECT * FROM products").fetchone()
        self.assertEqual(row["restock_notified"], 1)
        self.assertIsNone(row["unavailable_since"])

        upsert_products(self.conn, 1, [unavailable_product])

        row = self.conn.execute("SELECT * FROM products").fetchone()
        self.assertEqual(row["restock_notified"], 0)
        self.assertIsNotNone(row["unavailable_since"])

    def test_upsert_keeps_telegram_message_reference(self) -> None:
        self.conn.execute(
            """
            INSERT INTO products (
                monitor_id, product_key, title, status, available, stock,
                price, purchase_url, unavailable_since, restock_notified,
                telegram_chat_id, telegram_message_id, telegram_text_hash,
                last_seen_at
            ) VALUES (1, 'stable-key', 'US.LA.TRI.Basic', 'in_stock', 1, 10,
                '$5.00 CAD Monthly', 'https://app.vmiss.com/store/us-los-angeles-tri/basic',
                NULL, 1, '@stock', 42, 'old-hash', '2026-04-21 20:00:00')
            """
        )
        product = Product(
            key="stable-key",
            title="US.LA.TRI.Basic",
            status="in_stock",
            available=True,
            stock=9,
            price="$5.00 CAD Monthly",
            purchase_url="https://app.vmiss.com/store/us-los-angeles-tri/basic",
            button="Order Now",
        )

        upsert_products(self.conn, 1, [product])

        row = self.conn.execute("SELECT * FROM products").fetchone()
        self.assertEqual(row["telegram_chat_id"], "@stock")
        self.assertEqual(row["telegram_message_id"], 42)
        self.assertEqual(row["telegram_text_hash"], "old-hash")

    def test_telegram_edit_detects_stock_display_change(self) -> None:
        monitor_name = "库存监控"
        old_product = Product(
            key="stable-key",
            title="US.LA.TRI.Basic",
            status="in_stock",
            available=True,
            stock=10,
            price="$5.00 CAD Monthly",
            purchase_url="https://app.vmiss.com/store/us-los-angeles-tri/basic",
            button="Order Now",
        )
        new_product = Product(
            key="stable-key",
            title="US.LA.TRI.Basic",
            status="in_stock",
            available=True,
            stock=9,
            price="$5.00 CAD Monthly",
            purchase_url="https://app.vmiss.com/store/us-los-angeles-tri/basic",
            button="Order Now",
        )
        previous = {
            old_product.key: {
                "available": old_product.available,
                "stock": old_product.stock,
                "title": old_product.title,
                "status": old_product.status,
                "price": old_product.price,
                "purchase_url": old_product.purchase_url,
                "telegram_chat_id": "@stock",
                "telegram_message_id": 42,
                "telegram_text_hash": telegram_text_hash(
                    telegram_product_card(monitor_name, old_product)
                ),
            }
        }

        edits = telegram_products_to_edit(
            [new_product],
            previous,
            [],
            {"telegram_chat_id": "@stock"},
            monitor_name,
        )

        self.assertEqual(len(edits), 1)
        self.assertIn("库存：9", edits[0][2])


if __name__ == "__main__":
    unittest.main()
