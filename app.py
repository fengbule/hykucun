from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from monitor_core import (
    DEFAULT_CONFIG,
    Product,
    fetch_products,
    find_previous_product_state,
    find_restocked_products,
    telegram_product_card,
)

DB_LOCK = threading.Lock()
SCHEDULER_STARTED = False
INSECURE_SECRET_KEYS = {"change-this-secret"}
INSECURE_WEBUI_PASSWORDS = {"change-this-password"}
MIN_INTERVAL_SECONDS = 1
DEFAULT_SCHEDULER_TICK_SECONDS = 1.0
DEFAULT_NOTIFICATION_TARGET_NAME = "默认 Telegram"
NOTIFICATION_MODE_RESTOCK_ONLY = "restock_only"
NOTIFICATION_MODE_REALTIME = "realtime"


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def format_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, sec = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{sec}秒"
    if minutes:
        return f"{minutes}分{sec}秒"
    return f"{sec}秒"


def database_path() -> Path:
    path = Path(os.getenv("DATABASE_PATH", "/data/monitor.db"))
    if os.name == "nt" and str(path).startswith("\\data"):
        path = Path("data/monitor.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(database_path(), timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db() -> None:
    with DB_LOCK, connect_db() as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS notification_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                bot_token TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL DEFAULT '',
                message_thread_id TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                interval_seconds INTEGER NOT NULL DEFAULT 60,
                notification_mode TEXT NOT NULL DEFAULT 'restock_only',
                request_backend TEXT NOT NULL DEFAULT 'requests',
                browser_wait_seconds INTEGER NOT NULL DEFAULT 8,
                cookie_header TEXT NOT NULL DEFAULT '',
                aff_template TEXT NOT NULL DEFAULT '',
                product_selector TEXT NOT NULL DEFAULT '.product-card',
                title_selector TEXT NOT NULL DEFAULT '.product-card-header h5, h5',
                stock_selector TEXT NOT NULL DEFAULT '.stock-info',
                price_selector TEXT NOT NULL DEFAULT '.pricing-info',
                button_selector TEXT NOT NULL DEFAULT '.buy-now-button',
                link_selector TEXT NOT NULL DEFAULT '.buy-now-button[href], a[href]',
                stock_regex TEXT NOT NULL DEFAULT '库存\\s*[:：]?\\s*(\\d+)',
                in_stock_words TEXT NOT NULL DEFAULT '立即购买,加入购物车,购买,开通,下单,Order Now,Buy Now,Available,Configure',
                out_of_stock_words TEXT NOT NULL DEFAULT '产品已售罄,已售罄,售罄,缺货,无货,暂无库存,Out of Stock,Sold Out,Unavailable',
                title_filter TEXT NOT NULL DEFAULT '',
                notification_target_id INTEGER,
                last_checked_at TEXT,
                last_status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (notification_target_id) REFERENCES notification_targets(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS products (
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
                PRIMARY KEY (monitor_id, product_key),
                FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

        ensure_column(conn, "monitors", "request_backend", "TEXT NOT NULL DEFAULT 'requests'")
        ensure_column(conn, "monitors", "browser_wait_seconds", "INTEGER NOT NULL DEFAULT 8")
        ensure_column(conn, "monitors", "cookie_header", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "monitors", "title_filter", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "monitors", "notification_target_id", "INTEGER")
        ensure_column(
            conn,
            "monitors",
            "notification_mode",
            "TEXT NOT NULL DEFAULT 'restock_only'",
        )
        ensure_column(conn, "products", "unavailable_since", "TEXT")
        ensure_column(conn, "products", "restock_notified", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "products", "telegram_chat_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "products", "telegram_message_id", "INTEGER")
        ensure_column(conn, "products", "telegram_text_hash", "TEXT NOT NULL DEFAULT ''")

        count = conn.execute("SELECT COUNT(*) FROM monitors").fetchone()[0]
        if count == 0 and os.getenv("SEED_DEFAULT_MONITOR", "1") != "0":
            insert_default_monitor(conn)


def insert_default_monitor(conn: sqlite3.Connection) -> None:
    values = DEFAULT_CONFIG.copy()
    timestamp = now_str()
    conn.execute(
        """
        INSERT INTO monitors (
            name, url, enabled, interval_seconds, aff_template,
            notification_mode, request_backend, browser_wait_seconds, cookie_header,
            product_selector, title_selector, stock_selector, price_selector,
            button_selector, link_selector, stock_regex, in_stock_words,
            out_of_stock_words, title_filter, notification_target_id,
            created_at, updated_at
        ) VALUES (?, ?, 1, 60, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, ?, ?)
        """,
        (
            values["name"],
            values["url"],
            values["aff_template"],
            values.get("notification_mode", NOTIFICATION_MODE_RESTOCK_ONLY),
            values["request_backend"],
            values["browser_wait_seconds"],
            values.get("cookie_header", ""),
            values["product_selector"],
            values["title_selector"],
            values["stock_selector"],
            values["price_selector"],
            values["button_selector"],
            values["link_selector"],
            values["stock_regex"],
            values["in_stock_words"],
            values["out_of_stock_words"],
            timestamp,
            timestamp,
        ),
    )


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    return {
        "telegram_bot_token": settings.get("telegram_bot_token", os.getenv("TELEGRAM_BOT_TOKEN", "")),
        "telegram_chat_id": settings.get("telegram_chat_id", os.getenv("TELEGRAM_CHAT_ID", "")),
        "telegram_message_thread_id": settings.get(
            "telegram_message_thread_id", os.getenv("TELEGRAM_MESSAGE_THREAD_ID", "")
        ),
    }


def save_settings(conn: sqlite3.Connection, form: dict[str, str]) -> None:
    for key in ("telegram_bot_token", "telegram_chat_id", "telegram_message_thread_id"):
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, form.get(key, "").strip()),
        )


def default_notification_target(settings: dict[str, str]) -> dict[str, Any]:
    return {
        "id": None,
        "name": DEFAULT_NOTIFICATION_TARGET_NAME,
        "bot_token": settings.get("telegram_bot_token", ""),
        "chat_id": settings.get("telegram_chat_id", ""),
        "message_thread_id": settings.get("telegram_message_thread_id", ""),
        "enabled": 1,
        "is_default": True,
        "display_label": f"{DEFAULT_NOTIFICATION_TARGET_NAME}（全局）",
    }


def get_notification_targets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notification_targets ORDER BY enabled DESC, id ASC"
    ).fetchall()


def get_notification_target(conn: sqlite3.Connection, target_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM notification_targets WHERE id = ?",
        (target_id,),
    ).fetchone()


def normalize_notification_target_id(raw_value: str | None) -> int | None:
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        target_id = int(value)
    except ValueError:
        return None
    return target_id if target_id > 0 else None


def save_notification_target(conn: sqlite3.Connection, target_id: int | None, form: dict[str, str]) -> tuple[int, str]:
    timestamp = now_str()
    name = form.get("name", "").strip() or "未命名通道"
    bot_token = form.get("bot_token", "").strip()
    chat_id = form.get("chat_id", "").strip()
    thread_id = form.get("message_thread_id", "").strip()
    enabled = 1 if form.get("enabled") == "on" else 0

    if target_id:
        conn.execute(
            """
            UPDATE notification_targets
            SET name = ?, bot_token = ?, chat_id = ?, message_thread_id = ?, enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, bot_token, chat_id, thread_id, enabled, timestamp, target_id),
        )
        return target_id, "通知通道已更新"

    cursor = conn.execute(
        """
        INSERT INTO notification_targets (
            name, bot_token, chat_id, message_thread_id, enabled, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, bot_token, chat_id, thread_id, enabled, timestamp, timestamp),
    )
    return int(cursor.lastrowid), "通知通道已添加"


def delete_notification_target_record(conn: sqlite3.Connection, target_id: int) -> tuple[bool, int]:
    timestamp = now_str()
    reassigned = conn.execute(
        """
        UPDATE monitors
        SET notification_target_id = NULL, updated_at = ?
        WHERE notification_target_id = ?
        """,
        (timestamp, target_id),
    ).rowcount
    deleted = conn.execute(
        "DELETE FROM notification_targets WHERE id = ?",
        (target_id,),
    ).rowcount
    return bool(deleted), int(reassigned or 0)


def resolve_monitor_notification_settings(
    conn: sqlite3.Connection,
    monitor_row: sqlite3.Row | dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any]]:
    settings = get_settings(conn)
    selected_id = None
    try:
        selected_id = monitor_row["notification_target_id"]
    except (TypeError, KeyError, IndexError):
        selected_id = monitor_row.get("notification_target_id") if isinstance(monitor_row, dict) else None

    if selected_id:
        target = get_notification_target(conn, int(selected_id))
        if target and target["enabled"]:
            return (
                {
                    "telegram_bot_token": target["bot_token"],
                    "telegram_chat_id": target["chat_id"],
                    "telegram_message_thread_id": target["message_thread_id"],
                },
                {
                    "id": int(target["id"]),
                    "name": target["name"],
                    "is_default": False,
                    "display_label": target["name"],
                },
            )
    default_target = default_notification_target(settings)
    return settings, default_target


def monitor_config(row: sqlite3.Row) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    for key in (
        "name",
        "url",
        "request_backend",
        "browser_wait_seconds",
        "cookie_header",
        "aff_template",
        "notification_mode",
        "product_selector",
        "title_selector",
        "stock_selector",
        "price_selector",
        "button_selector",
        "link_selector",
        "stock_regex",
        "in_stock_words",
        "out_of_stock_words",
    ):
        config[key] = row[key]
    return config


def normalize_notification_mode(raw_value: str | None) -> str:
    value = (raw_value or "").strip().lower()
    if value == NOTIFICATION_MODE_REALTIME:
        return NOTIFICATION_MODE_REALTIME
    return NOTIFICATION_MODE_RESTOCK_ONLY


def filter_restocked_products(
    restocked: list[Product],
    previous_products: dict[str, dict[str, Any]],
    notification_mode: str,
) -> list[Product]:
    if normalize_notification_mode(notification_mode) == NOTIFICATION_MODE_REALTIME:
        return restocked
    filtered: list[Product] = []
    for product in restocked:
        previous = find_previous_product_state(product, previous_products)
        if (
            not previous
            or not bool(previous.get("available"))
            or not bool(previous.get("restock_notified", True))
        ):
            filtered.append(product)
    return filtered


def title_matches(product: Product, title_filter: str) -> bool:
    filters = [item.strip().lower() for item in title_filter.replace("，", ",").split(",")]
    filters = [item for item in filters if item]
    if not filters:
        return True

    def normalize(value: str) -> str:
        lowered = value.lower()
        return re.sub(r"[\s\-_/|·•,，;；:：()（）\[\]【】]+", "", lowered)

    title = product.title.lower()
    normalized_title = normalize(product.title)
    for item in filters:
        if item in title:
            return True
        normalized_item = normalize(item)
        if normalized_item and normalized_item in normalized_title:
            return True
    return False


def classify_monitor_failure(error_text: str, config: dict[str, Any], previous_status: str | None) -> tuple[str, str]:
    lowered = (error_text or "").lower()
    has_cookie = bool(str(config.get("cookie_header") or "").strip())
    challenge_hit = any(
        marker in lowered
        for marker in (
            "cloudflare challenge returned 403",
            "security verification page detected",
            "captcha",
            "just a moment",
        )
    )
    if not challenge_hit:
        return "error", error_text
    if not has_cookie:
        return (
            "cookie_required",
            "目标站点启用了安全验证。请切换 Browser 模式并配置有效 Cookie（如 cf_clearance）。",
        )
    if previous_status == "ok":
        return (
            "cookie_expiring",
            "检测到安全验证，Cookie 可能即将失效，请尽快更新 cf_clearance。",
        )
    return (
        "cookie_expired",
        "检测到安全验证，Cookie 可能已失效，请更新 Cookie 后重试。",
    )


def previous_product_state(conn: sqlite3.Connection, monitor_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM products WHERE monitor_id = ? ORDER BY last_seen_at DESC",
        (monitor_id,),
    ).fetchall()
    return {
        row["product_key"]: {
            "available": bool(row["available"]),
            "stock": row["stock"],
            "price": row["price"],
            "title": row["title"],
            "status": row["status"],
            "purchase_url": row["purchase_url"],
            "unavailable_since": row["unavailable_since"],
            "product_key": row["product_key"],
            "restock_notified": bool(row["restock_notified"]),
            "telegram_chat_id": row["telegram_chat_id"],
            "telegram_message_id": row["telegram_message_id"],
            "telegram_text_hash": row["telegram_text_hash"],
        }
        for row in rows
    }


def upsert_products(
    conn: sqlite3.Connection,
    monitor_id: int,
    products: list[Product],
    pending_notification_keys: set[str] | None = None,
) -> None:
    timestamp = now_str()
    pending_notification_keys = pending_notification_keys or set()
    existing_rows = conn.execute(
        """
        SELECT product_key, title, status, available, stock, price, purchase_url,
               unavailable_since, restock_notified, telegram_chat_id,
               telegram_message_id, telegram_text_hash
        FROM products
        WHERE monitor_id = ?
        ORDER BY last_seen_at DESC
        """,
        (monitor_id,),
    ).fetchall()
    existing = {
        row["product_key"]: {
            "available": bool(row["available"]),
            "stock": row["stock"],
            "price": row["price"],
            "title": row["title"],
            "status": row["status"],
            "purchase_url": row["purchase_url"],
            "unavailable_since": row["unavailable_since"],
            "product_key": row["product_key"],
            "restock_notified": bool(row["restock_notified"]),
            "telegram_chat_id": row["telegram_chat_id"],
            "telegram_message_id": row["telegram_message_id"],
            "telegram_text_hash": row["telegram_text_hash"],
        }
        for row in existing_rows
    }

    for product in products:
        previous = find_previous_product_state(product, existing)
        if product.available:
            unavailable_since = None
            if product.key in pending_notification_keys:
                restock_notified = 0
            elif previous:
                restock_notified = 1 if bool(previous.get("restock_notified", True)) else 0
            else:
                restock_notified = 1
        elif previous and not bool(previous["available"]) and previous.get("unavailable_since"):
            unavailable_since = previous["unavailable_since"]
            restock_notified = 0
        else:
            unavailable_since = timestamp
            restock_notified = 0

        telegram_chat_id = str(previous.get("telegram_chat_id") or "") if previous else ""
        telegram_message_id = previous.get("telegram_message_id") if previous else None
        telegram_text_hash = str(previous.get("telegram_text_hash") or "") if previous else ""
        previous_key = previous.get("product_key") if previous else None
        if previous_key and previous_key != product.key:
            conn.execute(
                "DELETE FROM products WHERE monitor_id = ? AND product_key = ?",
                (monitor_id, previous_key),
            )
        if product.purchase_url:
            conn.execute(
                """
                DELETE FROM products
                WHERE monitor_id = ? AND product_key <> ? AND purchase_url = ?
                """,
                (monitor_id, product.key, product.purchase_url),
            )

        conn.execute(
            """
            INSERT INTO products (
                monitor_id, product_key, title, status, available, stock,
                price, purchase_url, unavailable_since, restock_notified,
                telegram_chat_id, telegram_message_id, telegram_text_hash,
                last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(monitor_id, product_key) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                available = excluded.available,
                stock = excluded.stock,
                price = excluded.price,
                purchase_url = excluded.purchase_url,
                unavailable_since = excluded.unavailable_since,
                restock_notified = excluded.restock_notified,
                telegram_chat_id = excluded.telegram_chat_id,
                telegram_message_id = excluded.telegram_message_id,
                telegram_text_hash = excluded.telegram_text_hash,
                last_seen_at = excluded.last_seen_at
            """,
            (
                monitor_id,
                product.key,
                product.title,
                product.status,
                1 if product.available else 0,
                product.stock,
                product.price,
                product.purchase_url,
                unavailable_since,
                restock_notified,
                telegram_chat_id,
                telegram_message_id,
                telegram_text_hash,
                timestamp,
            ),
        )


def log_event(conn: sqlite3.Connection, monitor_id: int | None, level: str, message: str) -> None:
    conn.execute(
        "INSERT INTO events (monitor_id, level, message, created_at) VALUES (?, ?, ?, ?)",
        (monitor_id, level, message[:1000], now_str()),
    )


def telegram_text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def update_product_telegram_message(
    conn: sqlite3.Connection,
    monitor_id: int,
    product: Product,
    chat_id: str,
    message_id: int | None,
    text_hash: str,
) -> None:
    conn.execute(
        """
        UPDATE products
        SET telegram_chat_id = ?, telegram_message_id = ?, telegram_text_hash = ?, restock_notified = 1
        WHERE monitor_id = ? AND product_key = ?
        """,
        (chat_id, message_id, text_hash, monitor_id, product.key),
    )


def product_display_changed(product: Product, previous: dict[str, Any]) -> bool:
    return any(
        (
            bool(previous.get("available")) != product.available,
            previous.get("stock") != product.stock,
            str(previous.get("status") or "") != product.status,
            str(previous.get("title") or "") != product.title,
            str(previous.get("price") or "") != product.price,
            str(previous.get("purchase_url") or "") != product.purchase_url,
        )
    )


def telegram_products_to_edit(
    products: list[Product],
    previous_products: dict[str, dict[str, Any]],
    restocked: list[Product],
    settings: dict[str, str],
    monitor_name: str,
) -> list[tuple[Product, dict[str, Any], str]]:
    chat_id = settings.get("telegram_chat_id", "").strip()
    if not chat_id:
        return []
    restocked_keys = {product.key for product in restocked}
    edits: list[tuple[Product, dict[str, Any], str]] = []
    for product in products:
        if product.key in restocked_keys:
            continue
        previous = find_previous_product_state(product, previous_products)
        if not previous or not previous.get("telegram_message_id"):
            continue
        previous_chat_id = str(previous.get("telegram_chat_id") or "")
        if previous_chat_id and previous_chat_id != chat_id:
            continue
        text = telegram_product_card(monitor_name, product)
        if previous.get("telegram_text_hash") == telegram_text_hash(text):
            continue
        if product_display_changed(product, previous):
            edits.append((product, previous, text))
    return edits


def _telegram_payload(settings: dict[str, str], text: str) -> tuple[dict[str, Any], str, str]:
    bot_token = settings.get("telegram_bot_token", "").strip()
    chat_id = settings.get("telegram_chat_id", "").strip()
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    thread_id = settings.get("telegram_message_thread_id", "").strip()
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            pass
    return payload, bot_token, chat_id


def send_telegram_product(
    settings: dict[str, str],
    monitor_name: str,
    product: Product,
    stock_transition: str | None = None,
) -> tuple[bool, str, int | None]:
    text = telegram_product_card(monitor_name, product, stock_transition)
    payload, bot_token, chat_id = _telegram_payload(settings, text)
    if not bot_token or not chat_id:
        return False, "Telegram token 或 chat id 未配置", None
    if product.purchase_url.startswith(("http://", "https://")):
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "打开购买链接", "url": product.purchase_url}]]
        }
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=payload,
        timeout=15,
    )
    if response.ok:
        data = response.json()
        message_id = data.get("result", {}).get("message_id")
        return True, "Telegram 通知已发送", message_id
    return False, f"Telegram 返回 HTTP {response.status_code}: {response.text[:300]}", None


def edit_telegram_product(
    settings: dict[str, str],
    product: Product,
    message_id: int,
    text: str,
) -> tuple[bool, str]:
    payload, bot_token, chat_id = _telegram_payload(settings, text)
    if not bot_token or not chat_id:
        return False, "Telegram token 或 chat id 未配置"
    payload["message_id"] = int(message_id)
    if product.purchase_url.startswith(("http://", "https://")):
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "打开购买链接", "url": product.purchase_url}]]
        }
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/editMessageText",
        json=payload,
        timeout=15,
    )
    if response.ok:
        return True, "Telegram 库存消息已更新"
    if response.status_code == 400 and "message is not modified" in response.text.lower():
        return True, "Telegram 库存消息无需更新"
    return False, f"Telegram 返回 HTTP {response.status_code}: {response.text[:300]}"


def send_telegram_text(settings: dict[str, str], text: str) -> tuple[bool, str]:
    payload, bot_token, chat_id = _telegram_payload(settings, text)
    if not bot_token or not chat_id:
        return False, "Telegram token 或 chat id 未配置"
    payload["disable_web_page_preview"] = True
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=payload,
        timeout=15,
    )
    if response.ok:
        return True, "Telegram 测试通知已发送"
    return False, f"Telegram 返回 HTTP {response.status_code}: {response.text[:300]}"


def check_monitor_once(monitor_id: int) -> tuple[bool, str]:
    with DB_LOCK, connect_db() as conn:
        row = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
        if not row:
            return False, "监控项不存在"
        config = monitor_config(row)
        title_filter = row["title_filter"]
        notification_mode = normalize_notification_mode(row["notification_mode"])
        previous = previous_product_state(conn, monitor_id)
        settings, target_meta = resolve_monitor_notification_settings(conn, row)

    try:
        products = fetch_products(config)
        products = [product for product in products if title_matches(product, title_filter)]
        restocked_candidates = find_restocked_products(products, previous)
        restocked = filter_restocked_products(restocked_candidates, previous, notification_mode)
        pending_notification_keys = {product.key for product in restocked}
        telegram_edits = telegram_products_to_edit(products, previous, restocked, settings, config["name"])
        available_count = sum(1 for product in products if product.available)
        if products:
            status = "ok"
            error = ""
        else:
            status = "no_products"
            error = (
                "Page loaded, but no products matched. Check CSS selectors, title filter, "
                "or whether the site uses a different product layout."
            )
    except Exception as exc:
        products = []
        restocked = []
        pending_notification_keys = set()
        telegram_edits = []
        available_count = 0
        status, error = classify_monitor_failure(str(exc), config, row["last_status"])

    with DB_LOCK, connect_db() as conn:
        if products:
            upsert_products(conn, monitor_id, products, pending_notification_keys)
        conn.execute(
            """
            UPDATE monitors
            SET last_checked_at = ?, last_status = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (now_str(), status, error, now_str(), monitor_id),
        )
        if status == "error":
            log_event(conn, monitor_id, "error", error)
            return False, error
        if status in {"no_products", "cookie_required", "cookie_expiring", "cookie_expired"}:
            log_event(conn, monitor_id, "warning", error)
            return False, error
        message = f"检测 {len(products)} 个商品，可购买 {available_count} 个，触发推送 {len(restocked)} 个"
        log_event(conn, monitor_id, "info", f"{message}，通知通道：{target_meta['display_label']}")

    sent = 0
    failed = 0
    now = datetime.now()
    for product in restocked:
        previous_item = find_previous_product_state(product, previous)
        stock_transition: str | None = None
        if previous_item:
            previous_stock = previous_item.get("stock")
            previous_stock_text = "未知" if previous_stock is None else str(previous_stock)
            current_stock_text = "未知" if product.stock is None else str(product.stock)
            stock_transition = f"{previous_stock_text} -> {current_stock_text} 可用"
            unavailable_since = parse_dt(previous_item.get("unavailable_since"))
            if unavailable_since:
                elapsed = int((now - unavailable_since).total_seconds())
                stock_transition = f"{stock_transition}（{format_duration(elapsed)}）"
        sent_text = telegram_product_card(config["name"], product, stock_transition)
        ok, telegram_message, message_id = send_telegram_product(settings, config["name"], product, stock_transition)
        sent += 1 if ok else 0
        failed += 0 if ok else 1
        with DB_LOCK, connect_db() as conn:
            level = "info" if ok else "warning"
            if ok:
                update_product_telegram_message(
                    conn,
                    monitor_id,
                    product,
                    settings.get("telegram_chat_id", "").strip(),
                    int(message_id) if message_id is not None else None,
                    telegram_text_hash(sent_text),
                )
            log_event(conn, monitor_id, level, f"{product.title}: {telegram_message}（{target_meta['display_label']}）")

    updated = 0
    update_failed = 0
    for product, previous_item, text in telegram_edits:
        message_id = previous_item.get("telegram_message_id")
        if not message_id:
            continue
        ok, telegram_message = edit_telegram_product(settings, product, int(message_id), text)
        updated += 1 if ok else 0
        update_failed += 0 if ok else 1
        with DB_LOCK, connect_db() as conn:
            level = "info" if ok else "warning"
            if ok:
                update_product_telegram_message(
                    conn,
                    monitor_id,
                    product,
                    settings.get("telegram_chat_id", "").strip(),
                    int(message_id),
                    telegram_text_hash(text),
                )
            log_event(conn, monitor_id, level, f"{product.title}: {telegram_message}（{target_meta['display_label']}）")

    suffix = ""
    if restocked:
        suffix = f"，Telegram 成功 {sent} 条，失败 {failed} 条"
    if telegram_edits:
        suffix = f"{suffix}，Telegram 更新 {updated} 条，更新失败 {update_failed} 条"
    return True, f"{message}{suffix}"


def due_monitors() -> list[int]:
    with DB_LOCK, connect_db() as conn:
        rows = conn.execute("SELECT * FROM monitors WHERE enabled = 1").fetchall()
    due: list[int] = []
    now = datetime.now()
    for row in rows:
        last_checked = parse_dt(row["last_checked_at"])
        interval = max(MIN_INTERVAL_SECONDS, int(row["interval_seconds"] or 60))
        if last_checked is None or last_checked + timedelta(seconds=interval) <= now:
            due.append(int(row["id"]))
    return due


def scheduler_loop() -> None:
    tick_seconds = max(0.2, float(os.getenv("SCHEDULER_TICK_SECONDS", str(DEFAULT_SCHEDULER_TICK_SECONDS))))
    while True:
        try:
            for monitor_id in due_monitors():
                check_monitor_once(monitor_id)
        except Exception:
            pass
        time.sleep(tick_seconds)


def require_login(app: Flask) -> None:
    password = os.getenv("WEBUI_PASSWORD", "")
    if not password:
        return

    @app.before_request
    def _guard() -> Any:
        if request.endpoint in {"login", "static"}:
            return None
        if session.get("logged_in"):
            return None
        return redirect(url_for("login"))


def validate_runtime_secrets() -> None:
    secret_key = os.getenv("SECRET_KEY", "")
    webui_password = os.getenv("WEBUI_PASSWORD", "")
    if secret_key in INSECURE_SECRET_KEYS:
        raise RuntimeError(
            "SECRET_KEY is using a public placeholder. Set a unique random value in .env. "
            "If .env already looks correct, remove stale SECRET_KEY entries from docker-compose.override.yml."
        )
    if webui_password in INSECURE_WEBUI_PASSWORDS:
        raise RuntimeError(
            "WEBUI_PASSWORD is using a public placeholder. Set a private password in .env."
        )


def create_app() -> Flask:
    validate_runtime_secrets()
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY") or os.urandom(24)
    init_db()
    require_login(app)

    global SCHEDULER_STARTED
    if not SCHEDULER_STARTED and os.getenv("DISABLE_SCHEDULER", "0") != "1":
        threading.Thread(target=scheduler_loop, daemon=True).start()
        SCHEDULER_STARTED = True

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        if request.method == "POST":
            if request.form.get("password") == os.getenv("WEBUI_PASSWORD", ""):
                session["logged_in"] = True
                return redirect(url_for("index"))
            flash("密码不正确", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout() -> Any:
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def index() -> Any:
        edit_id = request.args.get("edit", type=int)
        target_edit_id = request.args.get("target_edit", type=int)
        with DB_LOCK, connect_db() as conn:
            settings = get_settings(conn)
            notification_targets = get_notification_targets(conn)
            monitors = conn.execute(
                """
                SELECT monitors.*,
                       notification_targets.name AS notification_target_name,
                       notification_targets.enabled AS notification_target_enabled
                FROM monitors
                LEFT JOIN notification_targets ON notification_targets.id = monitors.notification_target_id
                ORDER BY monitors.id DESC
                """
            ).fetchall()
            events = conn.execute(
                """
                SELECT events.*, monitors.name AS monitor_name
                FROM events
                LEFT JOIN monitors ON monitors.id = events.monitor_id
                ORDER BY events.id DESC
                LIMIT 20
                """
            ).fetchall()
            products_by_monitor: dict[int, list[sqlite3.Row]] = {}
            for monitor in monitors:
                products_by_monitor[int(monitor["id"])] = conn.execute(
                    """
                    SELECT * FROM products
                    WHERE monitor_id = ?
                    ORDER BY available DESC, title ASC
                    LIMIT 20
                    """,
                    (monitor["id"],),
                ).fetchall()
            edit_monitor = conn.execute("SELECT * FROM monitors WHERE id = ?", (edit_id,)).fetchone() if edit_id else None
            edit_target = get_notification_target(conn, target_edit_id) if target_edit_id else None

        form_monitor = edit_monitor or DEFAULT_CONFIG
        form_target = edit_target or {
            "name": "",
            "bot_token": "",
            "chat_id": "",
            "message_thread_id": "",
            "enabled": 1,
        }
        return render_template(
            "index.html",
            monitors=monitors,
            settings=settings,
            events=events,
            products_by_monitor=products_by_monitor,
            edit_monitor=edit_monitor,
            form_monitor=form_monitor,
            notification_targets=notification_targets,
            edit_target=edit_target,
            form_target=form_target,
            default_target=default_notification_target(settings),
            default_config=DEFAULT_CONFIG,
            auth_enabled=bool(os.getenv("WEBUI_PASSWORD", "")),
        )

    @app.post("/settings")
    def update_settings() -> Any:
        with DB_LOCK, connect_db() as conn:
            save_settings(conn, request.form)
        flash("默认 Telegram 设置已保存", "success")
        return redirect(url_for("index"))

    @app.post("/settings/test")
    def test_telegram() -> Any:
        with DB_LOCK, connect_db() as conn:
            settings = get_settings(conn)
        ok, message = send_telegram_text(settings, f"库存监控 WebUI 默认通道测试通知：{now_str()}")
        flash(message, "success" if ok else "error")
        return redirect(url_for("index"))

    @app.post("/notification-targets")
    def upsert_notification_target() -> Any:
        target_id = request.form.get("id", type=int)
        with DB_LOCK, connect_db() as conn:
            _, message = save_notification_target(conn, target_id, request.form)
        flash(message, "success")
        return redirect(url_for("index"))

    @app.post("/notification-targets/<int:target_id>/test")
    def test_notification_target(target_id: int) -> Any:
        with DB_LOCK, connect_db() as conn:
            row = get_notification_target(conn, target_id)
        if not row:
            flash("通知通道不存在", "error")
            return redirect(url_for("index"))
        settings = {
            "telegram_bot_token": row["bot_token"],
            "telegram_chat_id": row["chat_id"],
            "telegram_message_thread_id": row["message_thread_id"],
        }
        ok, message = send_telegram_text(settings, f"库存监控 WebUI 通道测试通知：{row['name']} {now_str()}")
        flash(message, "success" if ok else "error")
        return redirect(url_for("index", target_edit=target_id))

    @app.post("/notification-targets/<int:target_id>/delete")
    def delete_notification_target(target_id: int) -> Any:
        with DB_LOCK, connect_db() as conn:
            deleted, reassigned = delete_notification_target_record(conn, target_id)
        if not deleted:
            flash("通知通道不存在", "error")
            return redirect(url_for("index"))
        if reassigned:
            flash(f"通知通道已删除，{reassigned} 个监控项已回退到默认 Telegram", "success")
        else:
            flash("通知通道已删除", "success")
        return redirect(url_for("index"))

    @app.post("/monitors")
    def save_monitor() -> Any:
        monitor_id = request.form.get("id", type=int)
        timestamp = now_str()
        payload = {
            "name": request.form.get("name", "").strip() or "未命名监控",
            "url": request.form.get("url", "").strip(),
            "enabled": 1 if request.form.get("enabled") == "on" else 0,
            "interval_seconds": max(MIN_INTERVAL_SECONDS, request.form.get("interval_seconds", type=int) or 60),
            "notification_mode": normalize_notification_mode(request.form.get("notification_mode")),
            "request_backend": request.form.get("request_backend", "requests").strip()
            if request.form.get("request_backend") in {"requests", "browser"}
            else "requests",
            "browser_wait_seconds": max(0, request.form.get("browser_wait_seconds", type=int) or 0),
            "cookie_header": request.form.get("cookie_header", "").strip(),
            "aff_template": request.form.get("aff_template", "").strip(),
            "product_selector": request.form.get("product_selector", "").strip() or DEFAULT_CONFIG["product_selector"],
            "title_selector": request.form.get("title_selector", "").strip() or DEFAULT_CONFIG["title_selector"],
            "stock_selector": request.form.get("stock_selector", "").strip() or DEFAULT_CONFIG["stock_selector"],
            "price_selector": request.form.get("price_selector", "").strip() or DEFAULT_CONFIG["price_selector"],
            "button_selector": request.form.get("button_selector", "").strip() or DEFAULT_CONFIG["button_selector"],
            "link_selector": request.form.get("link_selector", "").strip() or DEFAULT_CONFIG["link_selector"],
            "stock_regex": request.form.get("stock_regex", "").strip() or DEFAULT_CONFIG["stock_regex"],
            "in_stock_words": request.form.get("in_stock_words", "").strip() or DEFAULT_CONFIG["in_stock_words"],
            "out_of_stock_words": request.form.get("out_of_stock_words", "").strip() or DEFAULT_CONFIG["out_of_stock_words"],
            "title_filter": request.form.get("title_filter", "").strip(),
            "notification_target_id": normalize_notification_target_id(request.form.get("notification_target_id")),
        }
        if not payload["url"]:
            flash("URL 不能为空", "error")
            return redirect(url_for("index", edit=monitor_id) if monitor_id else url_for("index"))

        with DB_LOCK, connect_db() as conn:
            if payload["notification_target_id"]:
                target = get_notification_target(conn, payload["notification_target_id"])
                if not target:
                    flash("选择的通知通道不存在", "error")
                    return redirect(url_for("index", edit=monitor_id) if monitor_id else url_for("index"))
            if monitor_id:
                conn.execute(
                    """
                    UPDATE monitors SET
                        name = ?, url = ?, enabled = ?, interval_seconds = ?,
                        notification_mode = ?, request_backend = ?, browser_wait_seconds = ?, cookie_header = ?,
                        aff_template = ?, product_selector = ?, title_selector = ?, stock_selector = ?,
                        price_selector = ?, button_selector = ?, link_selector = ?, stock_regex = ?,
                        in_stock_words = ?, out_of_stock_words = ?, title_filter = ?,
                        notification_target_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        payload["name"], payload["url"], payload["enabled"], payload["interval_seconds"],
                        payload["notification_mode"], payload["request_backend"], payload["browser_wait_seconds"], payload["cookie_header"],
                        payload["aff_template"], payload["product_selector"], payload["title_selector"],
                        payload["stock_selector"], payload["price_selector"], payload["button_selector"],
                        payload["link_selector"], payload["stock_regex"], payload["in_stock_words"],
                        payload["out_of_stock_words"], payload["title_filter"], payload["notification_target_id"],
                        timestamp, monitor_id,
                    ),
                )
                flash("监控项已更新", "success")
            else:
                conn.execute(
                    """
                    INSERT INTO monitors (
                        name, url, enabled, interval_seconds, notification_mode, request_backend,
                        browser_wait_seconds, cookie_header, aff_template,
                        product_selector, title_selector, stock_selector, price_selector,
                        button_selector, link_selector, stock_regex, in_stock_words,
                        out_of_stock_words, title_filter, notification_target_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["name"], payload["url"], payload["enabled"], payload["interval_seconds"],
                        payload["notification_mode"], payload["request_backend"], payload["browser_wait_seconds"], payload["cookie_header"],
                        payload["aff_template"], payload["product_selector"], payload["title_selector"],
                        payload["stock_selector"], payload["price_selector"], payload["button_selector"],
                        payload["link_selector"], payload["stock_regex"], payload["in_stock_words"],
                        payload["out_of_stock_words"], payload["title_filter"], payload["notification_target_id"],
                        timestamp, timestamp,
                    ),
                )
                flash("监控项已添加", "success")
        return redirect(url_for("index"))

    @app.post("/monitors/<int:monitor_id>/toggle")
    def toggle_monitor(monitor_id: int) -> Any:
        with DB_LOCK, connect_db() as conn:
            row = conn.execute("SELECT enabled FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE monitors SET enabled = ?, updated_at = ? WHERE id = ?",
                    (0 if row["enabled"] else 1, now_str(), monitor_id),
                )
        return redirect(url_for("index"))

    @app.post("/monitors/<int:monitor_id>/check")
    def check_monitor(monitor_id: int) -> Any:
        ok, message = check_monitor_once(monitor_id)
        flash(message, "success" if ok else "error")
        return redirect(url_for("index"))

    @app.post("/monitors/<int:monitor_id>/delete")
    def delete_monitor(monitor_id: int) -> Any:
        with DB_LOCK, connect_db() as conn:
            conn.execute("DELETE FROM products WHERE monitor_id = ?", (monitor_id,))
            conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        flash("监控项已删除", "success")
        return redirect(url_for("index"))

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
