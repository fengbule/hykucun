from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import upsert_products
from monitor_core import Product


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


if __name__ == "__main__":
    unittest.main()
