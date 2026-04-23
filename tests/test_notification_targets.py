import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


class NotificationTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "monitor.db"
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ["SEED_DEFAULT_MONITOR"] = "0"
        os.environ["DISABLE_SCHEDULER"] = "1"
        app.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def test_init_db_creates_notification_target_schema(self):
        with self.connect() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("notification_targets", tables)
            monitor_cols = {row[1] for row in conn.execute("PRAGMA table_info(monitors)")}
            self.assertIn("notification_target_id", monitor_cols)
            self.assertIn("notification_mode", monitor_cols)

    def test_resolve_monitor_notification_settings_uses_custom_target(self):
        with self.connect() as conn:
            app.save_settings(conn, {
                "telegram_bot_token": "default-token",
                "telegram_chat_id": "@default",
                "telegram_message_thread_id": "",
            })
            target_id, _ = app.save_notification_target(conn, None, {
                "name": "香港库存频道",
                "bot_token": "custom-token",
                "chat_id": "@hkstock",
                "message_thread_id": "12",
                "enabled": "on",
            })
            monitor = {
                "notification_target_id": target_id,
            }
            settings, meta = app.resolve_monitor_notification_settings(conn, monitor)
        self.assertEqual(settings["telegram_bot_token"], "custom-token")
        self.assertEqual(settings["telegram_chat_id"], "@hkstock")
        self.assertEqual(settings["telegram_message_thread_id"], "12")
        self.assertEqual(meta["name"], "香港库存频道")
        self.assertFalse(meta["is_default"])

    def test_resolve_monitor_notification_settings_falls_back_to_default(self):
        with self.connect() as conn:
            app.save_settings(conn, {
                "telegram_bot_token": "default-token",
                "telegram_chat_id": "@default",
                "telegram_message_thread_id": "8",
            })
            target_id, _ = app.save_notification_target(conn, None, {
                "name": "测试频道",
                "bot_token": "custom-token",
                "chat_id": "@custom",
                "message_thread_id": "2",
            })
            conn.execute("UPDATE notification_targets SET enabled = 0 WHERE id = ?", (target_id,))
            settings, meta = app.resolve_monitor_notification_settings(conn, {"notification_target_id": target_id})
        self.assertEqual(settings["telegram_bot_token"], "default-token")
        self.assertEqual(settings["telegram_chat_id"], "@default")
        self.assertEqual(settings["telegram_message_thread_id"], "8")
        self.assertTrue(meta["is_default"])

    def test_delete_notification_target_reassigns_monitors_to_default(self):
        with self.connect() as conn:
            target_id, _ = app.save_notification_target(conn, None, {
                "name": "备用机器人",
                "bot_token": "custom-token",
                "chat_id": "@backup",
                "message_thread_id": "",
                "enabled": "on",
            })
            timestamp = app.now_str()
            conn.execute(
                """
                INSERT INTO monitors (
                    name, url, enabled, interval_seconds, aff_template,
                    request_backend, browser_wait_seconds, cookie_header,
                    product_selector, title_selector, stock_selector, price_selector,
                    button_selector, link_selector, stock_regex, in_stock_words,
                    out_of_stock_words, title_filter, notification_target_id,
                    created_at, updated_at
                ) VALUES (?, ?, 1, 60, '', 'requests', 8, '', '.product-card', 'h5',
                    '.stock-info', '.pricing-info', '.buy-now-button', 'a[href]',
                    '库存\\s*[:：]?\\s*(\\d+)', 'Available', 'Sold Out', '',
                    ?, ?, ?)
                """,
                ("测试监控", "https://example.com", target_id, timestamp, timestamp),
            )

            deleted, reassigned = app.delete_notification_target_record(conn, target_id)
            row = conn.execute("SELECT notification_target_id FROM monitors").fetchone()

        self.assertTrue(deleted)
        self.assertEqual(reassigned, 1)
        self.assertIsNone(row["notification_target_id"])

    @patch("app.requests.post")
    def test_send_telegram_text_uses_selected_settings(self, mock_post):
        class Resp:
            ok = True
            status_code = 200
            text = "ok"
        mock_post.return_value = Resp()
        ok, message = app.send_telegram_text(
            {
                "telegram_bot_token": "bot-1",
                "telegram_chat_id": "@mychannel",
                "telegram_message_thread_id": "77",
            },
            "hello",
        )
        self.assertTrue(ok)
        self.assertIn("已发送", message)
        _, kwargs = mock_post.call_args
        self.assertIn("bot-1/sendMessage", mock_post.call_args.args[0])
        self.assertEqual(kwargs["json"]["chat_id"], "@mychannel")
        self.assertEqual(kwargs["json"]["message_thread_id"], 77)


if __name__ == "__main__":
    unittest.main()
