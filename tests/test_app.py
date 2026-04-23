from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
from app import (
    NOTIFICATION_MODE_REALTIME,
    NOTIFICATION_MODE_RESTOCK_ONLY,
    check_monitor_once,
    filter_restocked_products,
    normalize_notification_mode,
    save_settings,
    telegram_products_to_edit,
    telegram_text_hash,
    upsert_products,
)
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

    def test_upsert_keeps_pending_notification_unnotified_until_send_succeeds(self) -> None:
        product = Product(
            key="stable-key",
            title="US.LA.TRI.Basic",
            status="in_stock",
            available=True,
            stock=10,
            price="$5.00 CAD Monthly",
            purchase_url="https://app.vmiss.com/store/us-los-angeles-tri/basic",
            button="Order Now",
        )

        upsert_products(self.conn, 1, [product], pending_notification_keys={product.key})

        row = self.conn.execute("SELECT * FROM products").fetchone()
        self.assertEqual(row["stock"], 10)
        self.assertEqual(row["restock_notified"], 0)

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


class NotificationModeTests(unittest.TestCase):
    def test_normalize_notification_mode_defaults_to_restock_only(self) -> None:
        self.assertEqual(normalize_notification_mode(None), NOTIFICATION_MODE_RESTOCK_ONLY)
        self.assertEqual(normalize_notification_mode(""), NOTIFICATION_MODE_RESTOCK_ONLY)
        self.assertEqual(normalize_notification_mode("unknown"), NOTIFICATION_MODE_RESTOCK_ONLY)

    def test_filter_restocked_products_in_restock_only_mode(self) -> None:
        new_available = Product(
            key="new-1",
            title="A",
            status="in_stock",
            available=True,
            stock=5,
            price="$1",
            purchase_url="https://a",
            button="Buy Now",
        )
        restocked_again = Product(
            key="old-1",
            title="B",
            status="in_stock",
            available=True,
            stock=8,
            price="$2",
            purchase_url="https://b",
            button="Buy Now",
        )
        stock_changed_only = Product(
            key="old-2",
            title="C",
            status="in_stock",
            available=True,
            stock=9,
            price="$3",
            purchase_url="https://c",
            button="Buy Now",
        )
        previous = {
            "old-1": {"available": False, "stock": 0},
            "old-2": {"available": True, "stock": 10},
        }

        filtered = filter_restocked_products(
            [new_available, restocked_again, stock_changed_only],
            previous,
            NOTIFICATION_MODE_RESTOCK_ONLY,
        )

        self.assertEqual([item.key for item in filtered], ["new-1", "old-1"])

    def test_filter_restocked_products_in_realtime_mode_keeps_all(self) -> None:
        products = [
            Product(
                key="old-1",
                title="B",
                status="in_stock",
                available=True,
                stock=8,
                price="$2",
                purchase_url="https://b",
                button="Buy Now",
            ),
            Product(
                key="old-2",
                title="C",
                status="in_stock",
                available=True,
                stock=9,
                price="$3",
                purchase_url="https://c",
                button="Buy Now",
            ),
        ]

        filtered = filter_restocked_products(products, {"old-1": {"available": False}}, NOTIFICATION_MODE_REALTIME)
        self.assertEqual(filtered, products)

    def test_filter_restocked_products_retries_pending_notifications_in_restock_only_mode(self) -> None:
        product = Product(
            key="old-1",
            title="B",
            status="in_stock",
            available=True,
            stock=8,
            price="$2",
            purchase_url="https://b",
            button="Buy Now",
        )

        filtered = filter_restocked_products(
            [product],
            {"old-1": {"available": True, "stock": 8, "restock_notified": False}},
            NOTIFICATION_MODE_RESTOCK_ONLY,
        )
        self.assertEqual(filtered, [product])


class CheckMonitorOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "monitor.db"
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ["SEED_DEFAULT_MONITOR"] = "0"
        os.environ["DISABLE_SCHEDULER"] = "1"
        app.init_db()
        self.product = Product(
            key="stable-key",
            title="US.LA.TRI.Basic",
            status="in_stock",
            available=True,
            stock=10,
            price="$5.00 CAD Monthly",
            purchase_url="https://app.vmiss.com/store/us-los-angeles-tri/basic",
            button="Order Now",
        )
        with self.connect() as conn:
            save_settings(
                conn,
                {
                    "telegram_bot_token": "default-token",
                    "telegram_chat_id": "@stock",
                    "telegram_message_thread_id": "",
                },
            )
            timestamp = "2026-04-23 09:00:00"
            conn.execute(
                """
                INSERT INTO monitors (
                    name, url, enabled, interval_seconds, notification_mode,
                    request_backend, browser_wait_seconds, cookie_header, aff_template,
                    product_selector, title_selector, stock_selector, price_selector,
                    button_selector, link_selector, stock_regex, in_stock_words,
                    out_of_stock_words, title_filter, notification_target_id,
                    last_checked_at, last_status, last_error, created_at, updated_at
                ) VALUES (
                    ?, ?, 1, 60, ?, 'requests', 8, '', '',
                    '.product-card', 'h5', '.stock-info', '.pricing-info',
                    '.buy-now-button', 'a[href]', '库存\\s*[:：]?\\s*(\\d+)',
                    'Available', 'Sold Out', '', NULL,
                    NULL, 'ok', '', ?, ?
                )
                """,
                ("测试监控", "https://example.com", NOTIFICATION_MODE_RESTOCK_ONLY, timestamp, timestamp),
            )
            self.monitor_id = conn.execute("SELECT id FROM monitors").fetchone()[0]
            conn.execute(
                """
                INSERT INTO products (
                    monitor_id, product_key, title, status, available, stock,
                    price, purchase_url, unavailable_since, restock_notified,
                    telegram_chat_id, telegram_message_id, telegram_text_hash,
                    last_seen_at
                ) VALUES (?, ?, ?, 'out_of_stock', 0, 0, ?, ?, ?, 0, '', NULL, '', ?)
                """,
                (
                    self.monitor_id,
                    self.product.key,
                    self.product.title,
                    self.product.price,
                    self.product.purchase_url,
                    timestamp,
                    timestamp,
                ),
            )
            conn.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        for key in ("DATABASE_PATH", "SEED_DEFAULT_MONITOR", "DISABLE_SCHEDULER"):
            os.environ.pop(key, None)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def test_check_monitor_once_retries_failed_quiet_notification_without_losing_it(self) -> None:
        with patch("app.fetch_products", return_value=[self.product]), patch(
            "app.send_telegram_product",
            side_effect=[
                (False, "Telegram 返回 HTTP 500", None),
                (True, "Telegram 通知已发送", 99),
            ],
        ):
            ok, message = check_monitor_once(self.monitor_id)
        self.assertTrue(ok)
        self.assertIn("失败 1 条", message)

        with self.connect() as conn:
            row = conn.execute(
                "SELECT stock, restock_notified, telegram_message_id FROM products WHERE monitor_id = ?",
                (self.monitor_id,),
            ).fetchone()
        self.assertEqual(row["stock"], 10)
        self.assertEqual(row["restock_notified"], 0)
        self.assertIsNone(row["telegram_message_id"])

        with patch("app.fetch_products", return_value=[self.product]), patch(
            "app.send_telegram_product",
            return_value=(True, "Telegram 通知已发送", 99),
        ):
            ok, message = check_monitor_once(self.monitor_id)
        self.assertTrue(ok)
        self.assertIn("成功 1 条", message)

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT stock, restock_notified, telegram_chat_id, telegram_message_id
                FROM products WHERE monitor_id = ?
                """,
                (self.monitor_id,),
            ).fetchone()
        self.assertEqual(row["stock"], 10)
        self.assertEqual(row["restock_notified"], 1)
        self.assertEqual(row["telegram_chat_id"], "@stock")
        self.assertEqual(row["telegram_message_id"], 99)


if __name__ == "__main__":
    unittest.main()
