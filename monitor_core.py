from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


DEFAULT_URL = "https://www.heyunidc.cn/cart?fid=49&gid=97"

DEFAULT_CONFIG: dict[str, Any] = {
    "name": "核云周年庆特惠",
    "url": DEFAULT_URL,
    "request_backend": "requests",
    "browser_wait_seconds": 8,
    "cookie_header": "",
    "product_selector": ".product-card",
    "title_selector": ".product-card-header h5, h5",
    "stock_selector": ".stock-info",
    "price_selector": ".pricing-info",
    "button_selector": ".buy-now-button",
    "link_selector": ".buy-now-button[href], a[href]",
    "stock_regex": r"库存\s*[:：]?\s*(\d+)",
    "in_stock_words": "立即购买,加入购物车,购买,开通,下单,Order Now,Buy Now,Available,Configure",
    "out_of_stock_words": "产品已售罄,已售罄,售罄,缺货,无货,暂无库存,Out of Stock,Sold Out,Unavailable",
    "aff_template": "",
}

RESTOCK_STOCK_DELTA_THRESHOLD = 3

REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


@dataclass(frozen=True)
class Product:
    key: str
    title: str
    status: str
    available: bool
    stock: int | None
    price: str
    button: str
    purchase_url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def split_words(value: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [clean_text(str(item)) for item in value if clean_text(str(item))]
    return [clean_text(item) for item in re.split(r"[,，\n]", value or "") if clean_text(item)]


def first_selected(root: Any, selector: str | None) -> Any | None:
    if not selector:
        return None
    try:
        return root.select_one(selector)
    except Exception:
        return None


def selected_text(root: Any, selector: str | None) -> str:
    node = first_selected(root, selector)
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def extract_stock(stock_text: str, stock_regex: str) -> int | None:
    if not stock_text:
        return None
    patterns = [
        stock_regex,
        DEFAULT_CONFIG["stock_regex"],
        r"(\d+)\s*Available",
        r"Available\s*[:：]?\s*(\d+)",
        r"库存\s*[:：]?\s*(\d+)",
    ]
    for pattern in patterns:
        if not pattern:
            continue
        try:
            match = re.search(pattern, stock_text, re.IGNORECASE)
        except re.error:
            continue
        if not match:
            continue
        for group in match.groups():
            if group and group.isdigit():
                return int(group)
    return None


def node_classes(node: Any | None) -> set[str]:
    if node is None:
        return set()
    classes = node.get("class", [])
    return {str(item) for item in classes}


def extract_link(node: Any | None) -> str:
    if node is None:
        return ""

    for attr in ("href", "data-url", "data-href", "data-link"):
        value = node.get(attr)
        if value:
            return str(value).strip()

    onclick = node.get("onclick") or ""
    match = re.search(r"""(?:location\.href|window\.location)\s*=\s*['"]([^'"]+)['"]""", onclick)
    if match:
        return match.group(1)
    match = re.search(r"""['"](https?://[^'"]+|/[^'"]+)['"]""", onclick)
    return match.group(1) if match else ""


def apply_aff_template(url: str, aff_template: str) -> str:
    template = clean_text(aff_template)
    if not template:
        return url

    encoded_url = quote(url, safe="")
    if "{encoded_url}" in template or "{url}" in template or "{raw_url}" in template:
        return (
            template.replace("{encoded_url}", encoded_url)
            .replace("{raw_url}", url)
            .replace("{url}", url)
        )

    if template.startswith("?") or template.startswith("&"):
        extra_query = template.lstrip("?&")
        if not extra_query:
            return url
        split = urlsplit(url)
        existing_pairs = parse_qsl(split.query, keep_blank_values=True)
        extra_pairs = parse_qsl(extra_query, keep_blank_values=True)
        if extra_pairs and existing_pairs:
            merged_pairs = existing_pairs[:1] + extra_pairs + existing_pairs[1:]
            new_query = urlencode(merged_pairs, doseq=True)
            return urlunsplit((split.scheme, split.netloc, split.path, new_query, split.fragment))

        joiner = "&" if "?" in url else "?"
        return f"{url}{joiner}{extra_query}"

    return f"{template}{url}"


def product_key(title: str, price: str, purchase_url: str, fallback_text: str) -> str:
    title_key = clean_text(title).casefold()
    if title_key in {"未知商品", "unknown product"}:
        title_key = ""
    link_key = clean_text(purchase_url)
    if title_key or link_key:
        raw = "|".join(["product-v2", title_key, link_key])
    else:
        raw = "|".join(["product-v2", clean_text(price).casefold(), clean_text(fallback_text)[:120]])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def parse_product_card(card: Any, config: dict[str, Any]) -> Product:
    title = selected_text(card, config.get("title_selector")) or "未知商品"
    stock_text = selected_text(card, config.get("stock_selector"))
    price = selected_text(card, config.get("price_selector"))
    button_node = first_selected(card, config.get("button_selector"))
    button_text = clean_text(button_node.get_text(" ", strip=True)) if button_node else ""
    stock = extract_stock(stock_text, config.get("stock_regex") or DEFAULT_CONFIG["stock_regex"])

    out_words = split_words(config.get("out_of_stock_words") or DEFAULT_CONFIG["out_of_stock_words"])
    in_words = split_words(config.get("in_stock_words") or DEFAULT_CONFIG["in_stock_words"])
    button_says_sold_out = "SellOut" in node_classes(button_node) or contains_any(
        button_text, out_words
    )
    button_says_buyable = contains_any(button_text, in_words)

    if stock is not None:
        available = stock > 0 and not button_says_sold_out
    elif button_node:
        available = button_says_buyable and not button_says_sold_out
    else:
        available = False

    if available:
        status = "in_stock"
    elif stock == 0 or button_says_sold_out:
        status = "out_of_stock"
    else:
        status = "unknown"

    link_node = first_selected(card, config.get("link_selector")) or button_node
    raw_link = extract_link(link_node) or extract_link(button_node)
    absolute_link = urljoin(config["url"], raw_link) if raw_link else config["url"]
    purchase_url = apply_aff_template(absolute_link, config.get("aff_template", ""))
    fallback_text = clean_text(card.get_text(" ", strip=True))

    return Product(
        key=product_key(title, price, absolute_link, fallback_text),
        title=title,
        status=status,
        available=available,
        stock=stock,
        price=price,
        button=button_text,
        purchase_url=purchase_url,
    )


def parse_products(html_text: str, config: dict[str, Any]) -> list[Product]:
    soup = BeautifulSoup(html_text, "html.parser")
    selector = config.get("product_selector") or DEFAULT_CONFIG["product_selector"]
    try:
        cards = soup.select(selector)
    except Exception:
        cards = []
    products = [parse_product_card(card, config) for card in cards]
    if products:
        return products

    products = parse_whmcs_products(soup, config)
    if products:
        return products

    detect_blocked_page(html_text)
    return []


def contains_any(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in words if word)


def parse_whmcs_products(soup: BeautifulSoup, config: dict[str, Any]) -> list[Product]:
    products: list[Product] = []
    headings = soup.find_all(["h2", "h3", "h4", "h5", "h6"])
    for heading in headings:
        title = clean_text(heading.get_text(" ", strip=True))
        if not looks_like_product_heading(title):
            continue

        container = whmcs_product_container(heading)
        products.append(parse_whmcs_product_section(title, container, config))
    return dedupe_products(products)


def looks_like_product_heading(title: str) -> bool:
    if not title:
        return False
    lowered = title.lower()
    blocked = {
        "categories",
        "actions",
        "added to cart",
        "based on your order, we recommend:",
    }
    if lowered in blocked:
        return False

    if "." in title:
        return True

    strong_tokens = (
        "vps",
        "vmiss",
        "vmess",
        "kvm",
        "server",
        "cloud",
        "bgp",
        "cn2",
        "iepl",
        "三网",
        "优化",
        "线路",
        "香港",
        "日本",
        "美国",
    )
    if any(token in lowered for token in strong_tokens):
        return True

    return bool(re.search(r"\d+\s*(?:g|gb|tb|m|mb|核|core|vcpu)", lowered, re.IGNORECASE))


def whmcs_product_container(heading: Any) -> Any:
    for parent in heading.parents:
        if parent.name not in {"div", "li", "article", "section"}:
            continue
        if len(parent.find_all(["h3", "h4"])) > 1:
            continue
        if parent.find("a"):
            return parent
    return whmcs_section_fragment(heading)


def whmcs_section_fragment(heading: Any) -> BeautifulSoup:
    parts = [str(heading)]
    for sibling in heading.next_siblings:
        if getattr(sibling, "name", None) in {"h2", "h3", "h4"}:
            break
        parts.append(str(sibling))
    return BeautifulSoup("".join(parts), "html.parser")


def parse_whmcs_product_section(title: str, container: Any, config: dict[str, Any]) -> Product:
    card_text = clean_text(container.get_text(" ", strip=True))
    stock = extract_stock(card_text, config.get("stock_regex") or DEFAULT_CONFIG["stock_regex"])
    price = extract_price_from_text(card_text)

    in_words = split_words(config.get("in_stock_words") or DEFAULT_CONFIG["in_stock_words"])
    out_words = split_words(config.get("out_of_stock_words") or DEFAULT_CONFIG["out_of_stock_words"])
    button_node = find_link_by_words(container, in_words) or container.find("a", href=True)
    button_text = clean_text(button_node.get_text(" ", strip=True)) if button_node else ""
    button_says_sold_out = contains_any(card_text, out_words) or contains_any(button_text, out_words)
    button_says_buyable = contains_any(button_text, in_words)

    if stock is not None:
        available = stock > 0 and not button_says_sold_out
    elif button_node:
        available = button_says_buyable and not button_says_sold_out
    else:
        available = False

    if available:
        status = "in_stock"
    elif stock == 0 or button_says_sold_out:
        status = "out_of_stock"
    else:
        status = "unknown"

    raw_link = extract_link(button_node)
    absolute_link = urljoin(config["url"], raw_link) if raw_link else config["url"]
    purchase_url = apply_aff_template(absolute_link, config.get("aff_template", ""))

    return Product(
        key=product_key(title, price, absolute_link, card_text),
        title=title,
        status=status,
        available=available,
        stock=stock,
        price=price,
        button=button_text,
        purchase_url=purchase_url,
    )


def find_link_by_words(container: Any, words: list[str]) -> Any | None:
    for link in container.find_all("a", href=True):
        if contains_any(clean_text(link.get_text(" ", strip=True)), words):
            return link
    return None


def extract_price_from_text(text: str) -> str:
    match = re.search(
        r"([$€£¥]\s*\d[\d,.]*(?:\s*[A-Z]{3})?(?:\s*(?:Monthly|Annually|Quarterly|Yearly|年|月|三年))?)",
        text,
        re.IGNORECASE,
    )
    return clean_text(match.group(1)) if match else ""


def dedupe_products(products: list[Product]) -> list[Product]:
    seen: set[str] = set()
    deduped: list[Product] = []
    for product in products:
        if product.key in seen:
            continue
        seen.add(product.key)
        deduped.append(product)
    return deduped


def detect_blocked_page(html_text: str) -> None:
    snippet = html_text[:20000]
    lowered = snippet.lower()
    blocked_markers = (
        "cf_chl_opt",
        "just a moment...",
        "checking your browser",
        "verify you are human",
        "complete the security check",
        "attention required! | cloudflare",
        "security check",
        "ddos-guard",
    )
    blocked_markers_zh = (
        "正在进行安全验证",
        "验证您不是自动程序",
        "安全服务",
        "人机验证",
    )
    if any(marker in lowered for marker in blocked_markers) or any(
        marker in snippet for marker in blocked_markers_zh
    ):
        raise RuntimeError(
            "Security verification page detected (Cloudflare/CAPTCHA). "
            "Switch this monitor to Browser mode. "
            "If still blocked, set a valid Cookie header (for example cf_clearance). "
            "If it still fails, the site requires interactive human verification "
            "and cannot be monitored reliably by backend automation."
        )


def fetch_html_with_requests(config: dict[str, Any], timeout: int) -> str:
    headers = dict(REQUEST_HEADERS)
    cookie_header = clean_text(str(config.get("cookie_header") or ""))
    if cookie_header:
        headers["Cookie"] = cookie_header

    response = requests.get(
        config["url"],
        headers=headers,
        timeout=timeout,
    )
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        raise RuntimeError(
            "Cloudflare challenge returned 403. Switch this monitor to Browser mode, "
            "or provide a valid Cookie header (for example cf_clearance)."
        )
    response.raise_for_status()
    return response.text


def fetch_html_with_browser(config: dict[str, Any], timeout: int) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Browser mode requires Playwright. Rebuild the Docker image: "
            "docker compose up -d --build"
        ) from exc

    wait_seconds = max(0, int(config.get("browser_wait_seconds") or 0))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
        )
        cookie_header = clean_text(str(config.get("cookie_header") or ""))
        extra_headers = {"Cookie": cookie_header} if cookie_header else None
        context = browser.new_context(extra_http_headers=extra_headers)
        page = context.new_page()
        try:
            try:
                page.goto(config["url"], wait_until="domcontentloaded", timeout=timeout * 1000)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    "Browser mode timed out while loading target page. "
                    "The site may be under anti-bot challenge or network throttling. "
                    "Try increasing browser wait seconds, using a valid Cookie header, "
                    "or running from a residential IP."
                ) from exc
            if wait_seconds:
                page.wait_for_timeout(wait_seconds * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            html_text = page.content()
        finally:
            context.close()
            browser.close()
    return html_text


def fetch_products(config: dict[str, Any], timeout: int = 15) -> list[Product]:
    backend = (config.get("request_backend") or "requests").lower()
    if backend == "browser":
        html_text = fetch_html_with_browser(config, timeout)
    else:
        html_text = fetch_html_with_requests(config, timeout)
    return parse_products(html_text, config)


def find_restocked_products(
    products: list[Product],
    previous_products: dict[str, dict[str, Any]],
    alert_untracked_available_products: bool = False,
) -> list[Product]:
    restocked: list[Product] = []
    for product in products:
        if not product.available:
            continue

        previous = find_previous_product_state(product, previous_products)
        if not previous:
            if (
                alert_untracked_available_products
                or product.stock is None
                or stock_reached_delta_threshold(product.stock)
            ):
                restocked.append(product)
            continue

        was_restocked = not previous.get("available") and not previous.get("restock_notified")
        stock_increased = bool(previous.get("available")) and stock_increased_by_delta(
            product.stock, previous.get("stock")
        )
        if was_restocked or stock_increased:
            restocked.append(product)
    return restocked


def stock_reached_delta_threshold(stock: Any) -> bool:
    try:
        return int(stock) >= RESTOCK_STOCK_DELTA_THRESHOLD
    except (TypeError, ValueError):
        return False


def stock_increased_by_delta(current_stock: Any, previous_stock: Any) -> bool:
    try:
        current = int(current_stock)
        previous = int(previous_stock)
    except (TypeError, ValueError):
        return False
    return current - previous >= RESTOCK_STOCK_DELTA_THRESHOLD


def find_previous_product_state(
    product: Product, previous_products: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    previous = previous_products.get(product.key)
    if previous:
        return previous

    product_title = clean_text(product.title).casefold()
    product_price = clean_text(product.price).casefold()
    product_url = clean_text(product.purchase_url)
    for item in previous_products.values():
        previous_title = clean_text(str(item.get("title") or "")).casefold()
        previous_price = clean_text(str(item.get("price") or "")).casefold()
        previous_url = clean_text(str(item.get("purchase_url") or ""))
        if product_url and previous_url and product_url == previous_url:
            return item
        if product_title and previous_title == product_title:
            if product_price and previous_price and product_price == previous_price:
                return item
            if not product_url or not previous_url or product_url == previous_url:
                return item
    return None


def stock_label(product: Product) -> str:
    return "未知" if product.stock is None else str(product.stock)


def telegram_product_card(
    monitor_name: str, product: Product, stock_transition: str | None = None
) -> str:
    stock_line = stock_transition or stock_label(product)
    lines = [
        f"<b>{html.escape(monitor_name)}</b>",
        "",
        f"<b>{html.escape(product.title)}</b>",
        f"库存：{html.escape(stock_line)}",
        f"价格：{html.escape(product.price or '未知')}",
        f"状态：{html.escape(product.button or product.status)}",
        "",
        f'<a href="{html.escape(product.purchase_url, quote=True)}">购买链接</a>',
    ]
    return "\n".join(lines)
