from __future__ import annotations

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

from monitor_core import DEFAULT_CONFIG, Product, fetch_products, find_restocked_products
from monitor_core import telegram_product_card


DB_LOCK = threading.Lock()
SCHEDULER_STARTED = False
INSECURE_SECRET_KEYS = {"change-this-secret"}
INSECURE_WEBUI_PASSWORDS = {"change-this-password"}


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
    conn = sqlite3.connect(database_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with DB_LOCK, connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                interval_seconds INTEGER NOT NULL DEFAULT 60,
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
                in_stock_words TEXT NOT NULL DEFAULT '立即购买,加入购物车,购买,开通,下单',
                out_of_stock_words TEXT NOT NULL DEFAULT '产品已售罄,已售罄,售罄,缺货,无货,暂无库存',
                title_filter TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT,
                last_status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
        ensure_column(conn, "products", "unavailable_since", "TEXT")

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
            request_backend, browser_wait_seconds, cookie_header,
            product_selector, title_selector, stock_selector, price_selector,
            button_selector, link_selector, stock_regex, in_stock_words,
            out_of_stock_words, created_at, updated_at
        ) VALUES (?, ?, 1, 60, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            values["name"],
            values["url"],
            values["aff_template"],
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


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    return {
        "telegram_bot_token": settings.get(
            "telegram_bot_token", os.getenv("TELEGRAM_BOT_TOKEN", "")
        ),
        "telegram_chat_id": settings.get(
            "telegram_chat_id", os.getenv("TELEGRAM_CHAT_ID", "")
        ),
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


def monitor_config(row: sqlite3.Row) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    for key in (
        "name",
        "url",
        "request_backend",
        "browser_wait_seconds",
        "cookie_header",
        "aff_template",
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


def classify_monitor_failure(
    error_text: str,
    config: dict[str, Any],
    previous_status: str | None,
) -> tuple[str, str]:
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
        "SELECT * FROM products WHERE monitor_id = ?",
        (monitor_id,),
    ).fetchall()
    return {
        row["product_key"]: {
            "available": bool(row["available"]),
            "stock": row["stock"],
            "title": row["title"],
            "unavailable_since": row["unavailable_since"],
        }
        for row in rows
    }


def upsert_products(conn: sqlite3.Connection, monitor_id: int, products: list[Product]) -> None:
    timestamp = now_str()
    existing_rows = conn.execute(
        "SELECT product_key, available, unavailable_since FROM products WHERE monitor_id = ?",
        (monitor_id,),
    ).fetchall()
    existing = {row["product_key"]: row for row in existing_rows}

    for product in products:
        previous = existing.get(product.key)
        if product.available:
            unavailable_since = None
        elif previous and not bool(previous["available"]) and previous["unavailable_since"]:
            unavailable_since = previous["unavailable_since"]
        else:
            unavailable_since = timestamp

        conn.execute(
            """
            INSERT INTO products (
                monitor_id, product_key, title, status, available, stock,
                price, purchase_url, unavailable_since, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(monitor_id, product_key) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                available = excluded.available,
                stock = excluded.stock,
                price = excluded.price,
                purchase_url = excluded.purchase_url,
                unavailable_since = excluded.unavailable_since,
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
                timestamp,
            ),
        )


def log_event(
    conn: sqlite3.Connection, monitor_id: int | None, level: str, message: str
) -> None:
    conn.execute(
        "INSERT INTO events (monitor_id, level, message, created_at) VALUES (?, ?, ?, ?)",
        (monitor_id, level, message[:1000], now_str()),
    )


def send_telegram_product(
    settings: dict[str, str],
    monitor_name: str,
    product: Product,
    stock_transition: str | None = None,
) -> tuple[bool, str]:
    bot_token = settings.get("telegram_bot_token", "").strip()
    chat_id = settings.get("telegram_chat_id", "").strip()
    if not bot_token or not chat_id:
        return False, "Telegram token 或 chat id 未配置"

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": telegram_product_card(monitor_name, product, stock_transition),
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if product.purchase_url.startswith(("http://", "https://")):
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "打开购买链接", "url": product.purchase_url}]]
        }

    thread_id = settings.get("telegram_message_thread_id", "").strip()
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            pass

    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=payload,
        timeout=15,
    )
    if response.ok:
        return True, "Telegram 通知已发送"
    return False, f"Telegram 返回 HTTP {response.status_code}: {response.text[:300]}"


def send_telegram_text(settings: dict[str, str], text: str) -> tuple[bool, str]:
    bot_token = settings.get("telegram_bot_token", "").strip()
    chat_id = settings.get("telegram_chat_id", "").strip()
    if not bot_token or not chat_id:
        return False, "Telegram token 或 chat id 未配置"

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    thread_id = settings.get("telegram_message_thread_id", "").strip()
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            pass
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
        previous = previous_product_state(conn, monitor_id)
        settings = get_settings(conn)

    try:
        products = fetch_products(config)
        products = [product for product in products if title_matches(product, title_filter)]
        restocked = find_restocked_products(products, previous)
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
        available_count = 0
        status, error = classify_monitor_failure(str(exc), config, row["last_status"])

    with DB_LOCK, connect_db() as conn:
        if products:
            upsert_products(conn, monitor_id, products)

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

        if status in {
            "no_products",
            "cookie_required",
            "cookie_expiring",
            "cookie_expired",
        }:
            log_event(conn, monitor_id, "warning", error)
            return False, error

        message = f"检测 {len(products)} 个商品，可购买 {available_count} 个，新补货 {len(restocked)} 个"
        log_event(conn, monitor_id, "info", message)

    sent = 0
    failed = 0
    now = datetime.now()
    for product in restocked:
        previous_item = previous.get(product.key)
        stock_transition: str | None = None
        if previous_item:
            previous_stock = previous_item.get("stock")
            previous_stock_text = "未知" if previous_stock is None else str(previous_stock)
            current_stock_text = "未知" if product.stock is None else str(product.stock)
            stock_transition = f"{previous_stock_text} -> {current_stock_text} Available"
            unavailable_since = parse_dt(previous_item.get("unavailable_since"))
            if unavailable_since:
                elapsed = int((now - unavailable_since).total_seconds())
                stock_transition = f"{stock_transition}（{format_duration(elapsed)}）"

        ok, telegram_message = send_telegram_product(
            settings,
            config["name"],
            product,
            stock_transition,
        )
        sent += 1 if ok else 0
        failed += 0 if ok else 1
        with DB_LOCK, connect_db() as conn:
            level = "info" if ok else "warning"
            log_event(conn, monitor_id, level, f"{product.title}: {telegram_message}")

    suffix = ""
    if restocked:
        suffix = f"，Telegram 成功 {sent} 条，失败 {failed} 条"
    return True, f"{message}{suffix}"


def due_monitors() -> list[int]:
    with DB_LOCK, connect_db() as conn:
        rows = conn.execute("SELECT * FROM monitors WHERE enabled = 1").fetchall()
    due: list[int] = []
    now = datetime.now()
    for row in rows:
        last_checked = parse_dt(row["last_checked_at"])
        interval = max(10, int(row["interval_seconds"] or 60))
        if last_checked is None or last_checked + timedelta(seconds=interval) <= now:
            due.append(int(row["id"]))
    return due


def scheduler_loop() -> None:
    while True:
        try:
            for monitor_id in due_monitors():
                check_monitor_once(monitor_id)
        except Exception:
            pass
        time.sleep(int(os.getenv("SCHEDULER_TICK_SECONDS", "5")))


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
            "SECRET_KEY is using a public placeholder. Set a unique random value "
            "in .env. If .env already looks correct, remove stale SECRET_KEY "
            "entries from docker-compose.override.yml and verify with "
            "`docker compose config`."
        )
    if webui_password in INSECURE_WEBUI_PASSWORDS:
        raise RuntimeError(
            "WEBUI_PASSWORD is using a public placeholder. Set a private password "
            "in .env, or leave it empty only on a trusted private network. If .env "
            "already looks correct, remove stale WEBUI_PASSWORD entries from "
            "docker-compose.override.yml and verify with `docker compose config`."
        )


def create_app() -> Flask:
    validate_runtime_secrets()
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY") or os.urandom(24)
    init_db()
    require_login(app)

    global SCHEDULER_STARTED
    if not SCHEDULER_STARTED and os.getenv("DISABLE_SCHEDULER", "0") != "1":
        thread = threading.Thread(target=scheduler_loop, daemon=True)
        thread.start()
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
        with DB_LOCK, connect_db() as conn:
            monitors = conn.execute("SELECT * FROM monitors ORDER BY id DESC").fetchall()
            settings = get_settings(conn)
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
            edit_monitor = None
            if edit_id:
                edit_monitor = conn.execute(
                    "SELECT * FROM monitors WHERE id = ?",
                    (edit_id,),
                ).fetchone()

        form_monitor = edit_monitor or DEFAULT_CONFIG
        return render_template(
            "index.html",
            monitors=monitors,
            settings=settings,
            events=events,
            products_by_monitor=products_by_monitor,
            edit_monitor=edit_monitor,
            form_monitor=form_monitor,
            default_config=DEFAULT_CONFIG,
            auth_enabled=bool(os.getenv("WEBUI_PASSWORD", "")),
        )

    @app.post("/settings")
    def update_settings() -> Any:
        with DB_LOCK, connect_db() as conn:
            save_settings(conn, request.form)
        flash("Telegram 设置已保存", "success")
        return redirect(url_for("index"))

    @app.post("/settings/test")
    def test_telegram() -> Any:
        with DB_LOCK, connect_db() as conn:
            settings = get_settings(conn)
        ok, message = send_telegram_text(settings, f"库存监控 WebUI 测试通知：{now_str()}")
        flash(message, "success" if ok else "error")
        return redirect(url_for("index"))

    @app.post("/monitors")
    def save_monitor() -> Any:
        monitor_id = request.form.get("id", type=int)
        timestamp = now_str()
        payload = {
            "name": request.form.get("name", "").strip() or "未命名监控",
            "url": request.form.get("url", "").strip(),
            "enabled": 1 if request.form.get("enabled") == "on" else 0,
            "interval_seconds": max(10, request.form.get("interval_seconds", type=int) or 60),
            "request_backend": request.form.get("request_backend", "requests").strip()
            if request.form.get("request_backend") in {"requests", "browser"}
            else "requests",
            "browser_wait_seconds": max(
                0, request.form.get("browser_wait_seconds", type=int) or 0
            ),
            "cookie_header": request.form.get("cookie_header", "").strip(),
            "aff_template": request.form.get("aff_template", "").strip(),
            "product_selector": request.form.get("product_selector", "").strip()
            or DEFAULT_CONFIG["product_selector"],
            "title_selector": request.form.get("title_selector", "").strip()
            or DEFAULT_CONFIG["title_selector"],
            "stock_selector": request.form.get("stock_selector", "").strip()
            or DEFAULT_CONFIG["stock_selector"],
            "price_selector": request.form.get("price_selector", "").strip()
            or DEFAULT_CONFIG["price_selector"],
            "button_selector": request.form.get("button_selector", "").strip()
            or DEFAULT_CONFIG["button_selector"],
            "link_selector": request.form.get("link_selector", "").strip()
            or DEFAULT_CONFIG["link_selector"],
            "stock_regex": request.form.get("stock_regex", "").strip()
            or DEFAULT_CONFIG["stock_regex"],
            "in_stock_words": request.form.get("in_stock_words", "").strip()
            or DEFAULT_CONFIG["in_stock_words"],
            "out_of_stock_words": request.form.get("out_of_stock_words", "").strip()
            or DEFAULT_CONFIG["out_of_stock_words"],
            "title_filter": request.form.get("title_filter", "").strip(),
        }
        if not payload["url"]:
            flash("URL 不能为空", "error")
            return redirect(url_for("index", edit=monitor_id) if monitor_id else url_for("index"))

        with DB_LOCK, connect_db() as conn:
            if monitor_id:
                conn.execute(
                    """
                    UPDATE monitors SET
                        name = ?, url = ?, enabled = ?, interval_seconds = ?,
                        request_backend = ?, browser_wait_seconds = ?,
                        cookie_header = ?,
                        aff_template = ?, product_selector = ?, title_selector = ?,
                        stock_selector = ?, price_selector = ?, button_selector = ?,
                        link_selector = ?, stock_regex = ?, in_stock_words = ?,
                        out_of_stock_words = ?, title_filter = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        payload["name"],
                        payload["url"],
                        payload["enabled"],
                        payload["interval_seconds"],
                        payload["request_backend"],
                        payload["browser_wait_seconds"],
                        payload["cookie_header"],
                        payload["aff_template"],
                        payload["product_selector"],
                        payload["title_selector"],
                        payload["stock_selector"],
                        payload["price_selector"],
                        payload["button_selector"],
                        payload["link_selector"],
                        payload["stock_regex"],
                        payload["in_stock_words"],
                        payload["out_of_stock_words"],
                        payload["title_filter"],
                        timestamp,
                        monitor_id,
                    ),
                )
                flash("监控项已更新", "success")
            else:
                conn.execute(
                    """
                    INSERT INTO monitors (
                        name, url, enabled, interval_seconds,
                        request_backend, browser_wait_seconds, cookie_header, aff_template,
                        product_selector, title_selector, stock_selector,
                        price_selector, button_selector, link_selector,
                        stock_regex, in_stock_words, out_of_stock_words,
                        title_filter, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["name"],
                        payload["url"],
                        payload["enabled"],
                        payload["interval_seconds"],
                        payload["request_backend"],
                        payload["browser_wait_seconds"],
                        payload["cookie_header"],
                        payload["aff_template"],
                        payload["product_selector"],
                        payload["title_selector"],
                        payload["stock_selector"],
                        payload["price_selector"],
                        payload["button_selector"],
                        payload["link_selector"],
                        payload["stock_regex"],
                        payload["in_stock_words"],
                        payload["out_of_stock_words"],
                        payload["title_filter"],
                        timestamp,
                        timestamp,
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
