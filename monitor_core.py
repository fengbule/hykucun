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
    "notification_mode": "restock_only",
}

REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

API_REQUEST_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": REQUEST_HEADERS["User-Agent"],
}

VOLATILE_QUERY_KEYS = {
    "aff", "affiliate", "ref", "referer", "promocode", "promo", "coupon",
    "currency", "language", "carttpl", "gid", "fid", "t", "ts", "time",
    "timestamp", "nonce", "session", "sid", "token",
}

DISABLED_CLASS_WORDS = (
    "disabled", "disable", "soldout", "sold-out", "outofstock", "out-of-stock",
    "unavailable", "sellout",
)

PRICE_PATTERNS = (
    r"([$€£¥]\s*\d[\d,.]*(?:\s*[A-Z]{3})?(?:\s*(?:/\s*)?(?:Monthly|Annually|Quarterly|Yearly|年|月|三年))?)",
    r"(\d[\d,.]*\s*(?:USD|CAD|EUR|GBP|CNY|RMB|HKD|JPY)(?:\s*(?:/\s*)?(?:Monthly|Annually|Quarterly|Yearly|年|月|三年))?)",
)

JSON_PRODUCT_PATHS = (
    "data", "data.items", "data.list", "data.products", "data.plans", "data.goods",
    "items", "list", "products", "plans", "goods", "result", "result.items",
    "result.list", "result.products", "result.plans",
)
JSON_TITLE_PATHS = "name|title|product_name|goods_name|plan_name|product.name|goods.name|plan.name"
JSON_STOCK_PATHS = "stock|inventory|qty|quantity|available|available_count|stock_count|count|remain|remaining|num|left"
JSON_PRICE_PATHS = "price|amount|monthly_price|price_monthly|sale_price|product.price|goods.price|plan.price"
JSON_STATUS_PATHS = "status|state|stock_status|available|is_available|button|button_text"
JSON_LINK_PATHS = "url|link|purchase_url|buy_url|order_url|cart_url|href|product.url|goods.url|plan.url"


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


def parse_stock_number(value: str) -> int | None:
    match = re.search(r"\d[\d,]*", value or "")
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def extract_stock(stock_text: str, stock_regex: str) -> int | None:
    if not stock_text:
        return None
    patterns = [
        stock_regex,
        DEFAULT_CONFIG["stock_regex"],
        r"(\d[\d,]*)\s*(?:Available|Left|In\s*Stock|stock)",
        r"(?:Available|Qty|Quantity|Stock|库存|剩余|可用)\s*[:：]?\s*(\d[\d,]*)",
        r"(\d[\d,]*)\s*(?:件|台|个)?\s*(?:库存|剩余|可用)",
        r"库存\s*[:：]?\s*(\d[\d,]*)",
    ]
    for pattern in patterns:
        if not pattern:
            continue
        try:
            matches = list(re.finditer(pattern, stock_text, re.IGNORECASE))
        except re.error:
            continue
        for match in matches:
            candidates = match.groups() or (match.group(0),)
            for candidate in candidates:
                if not candidate:
                    continue
                stock = parse_stock_number(candidate)
                if stock is not None:
                    return stock
    return None


def node_classes(node: Any | None) -> set[str]:
    if node is None:
        return set()
    classes = node.get("class", [])
    return {str(item) for item in classes}


def node_is_disabled(node: Any | None) -> bool:
    if node is None:
        return False
    if node.has_attr("disabled"):
        return True
    if str(node.get("aria-disabled", "")).lower() == "true":
        return True
    class_text = " ".join({item.lower() for item in node_classes(node)})
    return any(word in class_text for word in DISABLED_CLASS_WORDS)


def extract_link(node: Any | None) -> str:
    if node is None:
        return ""
    for attr in ("href", "data-url", "data-href", "data-link", "data-target"):
        value = node.get(attr)
        if value:
            value_text = str(value).strip()
            if value_text and not value_text.lower().startswith(("javascript:", "#")):
                return value_text
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
        return template.replace("{encoded_url}", encoded_url).replace("{raw_url}", url).replace("{url}", url)
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


def normalize_product_url(url: str) -> str:
    value = clean_text(url)
    if not value:
        return ""
    split = urlsplit(value)
    query_pairs: list[tuple[str, str]] = []
    for key, item_value in parse_qsl(split.query, keep_blank_values=True):
        lowered_key = key.lower()
        if lowered_key in VOLATILE_QUERY_KEYS or lowered_key.startswith("utm_"):
            continue
        query_pairs.append((key, item_value))
    normalized_path = re.sub(r"/+$", "", split.path or "/")
    normalized_query = urlencode(query_pairs, doseq=True)
    return urlunsplit((split.scheme.lower(), split.netloc.lower(), normalized_path, normalized_query, ""))


def product_key(title: str, price: str, purchase_url: str, fallback_text: str) -> str:
    title_key = clean_text(title).casefold()
    if title_key in {"未知商品", "unknown product"}:
        title_key = ""
    link_key = normalize_product_url(purchase_url)
    if title_key or link_key:
        raw = "|".join(["product-v3", title_key, link_key])
    else:
        raw = "|".join(["product-v3", clean_text(price).casefold(), clean_text(fallback_text)[:120]])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def parse_product_card(card: Any, config: dict[str, Any]) -> Product:
    title = selected_text(card, config.get("title_selector")) or "未知商品"
    card_text = clean_text(card.get_text(" ", strip=True))
    stock_text = selected_text(card, config.get("stock_selector")) or card_text
    price = selected_text(card, config.get("price_selector")) or extract_price_from_text(card_text)
    button_node = first_selected(card, config.get("button_selector"))
    button_text = clean_text(button_node.get_text(" ", strip=True)) if button_node else ""
    stock = extract_stock(stock_text, config.get("stock_regex") or DEFAULT_CONFIG["stock_regex"])
    out_words = split_words(config.get("out_of_stock_words") or DEFAULT_CONFIG["out_of_stock_words"])
    in_words = split_words(config.get("in_stock_words") or DEFAULT_CONFIG["in_stock_words"])
    status_text = " ".join([card_text, button_text])
    button_says_sold_out = node_is_disabled(button_node) or contains_any(status_text, out_words)
    button_says_buyable = contains_any(button_text, in_words) or (not button_text and contains_any(card_text, in_words))
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
    return Product(product_key(title, price, absolute_link, card_text), title, status, available, stock, price, button_text, purchase_url)


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
    for heading in soup.find_all(["h2", "h3", "h4", "h5", "h6"]):
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
    blocked = {"categories", "actions", "added to cart", "based on your order, we recommend:"}
    if lowered in blocked:
        return False
    if "." in title:
        return True
    strong_tokens = (
        "vps", "vmiss", "vmess", "kvm", "server", "cloud", "bgp", "cn2", "cmin2",
        "iepl", "starter", "basic", "core", "premium", "lite", "三网", "优化", "线路",
        "香港", "日本", "美国",
    )
    if any(token in lowered for token in strong_tokens):
        return True
    return bool(re.search(r"\d+\s*(?:g|gb|tb|m|mb|核|core|vcpu)", lowered, re.IGNORECASE))


def whmcs_product_container(heading: Any) -> Any:
    for parent in heading.parents:
        if parent.name not in {"div", "li", "article", "section"}:
            continue
        if len(parent.find_all(["h2", "h3", "h4", "h5", "h6"])) > 1:
            continue
        if parent.find(["a", "button"]):
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
    button_node = find_link_by_words(container, in_words) or find_purchase_node(container) or container.find("a", href=True)
    button_text = clean_text(button_node.get_text(" ", strip=True)) if button_node else ""
    status_text = " ".join([card_text, button_text])
    button_says_sold_out = node_is_disabled(button_node) or contains_any(status_text, out_words)
    button_says_buyable = contains_any(button_text, in_words) or contains_any(card_text, in_words)
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
    return Product(product_key(title, price, absolute_link, card_text), title, status, available, stock, price, button_text, purchase_url)


def find_link_by_words(container: Any, words: list[str]) -> Any | None:
    for link in container.find_all(["a", "button"]):
        if node_is_disabled(link):
            continue
        if contains_any(clean_text(link.get_text(" ", strip=True)), words):
            return link
    return None


def find_purchase_node(container: Any) -> Any | None:
    for node in container.find_all(["a", "button"]):
        link = extract_link(node)
        if not link or node_is_disabled(node):
            continue
        if any(token in link.lower() for token in ("cart", "add", "pid=", "configure", "order", "buy", "store")):
            return node
    return None


def extract_price_from_text(text: str) -> str:
    for pattern in PRICE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return ""


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
        "cf_chl_opt", "just a moment...", "checking your browser", "verify you are human",
        "complete the security check", "attention required! | cloudflare", "security check",
        "ddos-guard", "cf-browser-verification",
    )
    blocked_markers_zh = ("正在进行安全验证", "验证您不是自动程序", "安全服务", "人机验证")
    if any(marker in lowered for marker in blocked_markers) or any(marker in snippet for marker in blocked_markers_zh):
        raise RuntimeError(
            "Security verification page detected (Cloudflare/CAPTCHA). Switch this monitor to Browser mode. "
            "If still blocked, set a valid Cookie header (for example cf_clearance). If it still fails, "
            "the site requires interactive human verification and cannot be monitored reliably by backend automation."
        )


def fetch_html_with_requests(config: dict[str, Any], timeout: int) -> str:
    headers = dict(REQUEST_HEADERS)
    cookie_header = clean_text(str(config.get("cookie_header") or ""))
    if cookie_header:
        headers["Cookie"] = cookie_header
    response = requests.get(config["url"], headers=headers, timeout=timeout)
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        raise RuntimeError("Cloudflare challenge returned 403. Switch this monitor to Browser mode, or provide a valid Cookie header (for example cf_clearance).")
    if response.status_code in {403, 429, 503}:
        detect_blocked_page(response.text)
    response.raise_for_status()
    return response.text


def is_probably_css_selector(value: str) -> bool:
    value = clean_text(value)
    if not value:
        return False
    return any(token in value for token in (".", "#", " ", ",", ">", "[", ":")) and not value.startswith("$")


def split_path_alternatives(value: str | list[str] | tuple[str, ...], defaults: str = "") -> list[str]:
    raw_items: list[str]
    if isinstance(value, (list, tuple)):
        raw_items = [str(item) for item in value]
    else:
        text = clean_text(str(value or ""))
        raw_items = re.split(r"[|\n]", text) if text and not is_probably_css_selector(text) else []
    if defaults:
        raw_items.extend(re.split(r"[|\n]", defaults))
    result: list[str] = []
    for item in raw_items:
        item = clean_text(item)
        if item and item not in result:
            result.append(item)
    return result


def json_path_tokens(path: str) -> list[str]:
    cleaned = clean_text(path).strip(".")
    if cleaned.startswith("$."):
        cleaned = cleaned[2:]
    elif cleaned == "$":
        return []
    cleaned = cleaned.replace("[*]", ".*").replace("[]", ".*")
    cleaned = re.sub(r"\[(\d+)\]", r".\1", cleaned)
    return [token for token in cleaned.split(".") if token]


def json_path_values(data: Any, path: str) -> list[Any]:
    values = [data]
    for token in json_path_tokens(path):
        next_values: list[Any] = []
        for value in values:
            if token == "*":
                if isinstance(value, list):
                    next_values.extend(value)
                elif isinstance(value, dict):
                    next_values.extend(value.values())
                continue
            if isinstance(value, dict):
                if token in value:
                    next_values.append(value[token])
                else:
                    lowered = token.lower()
                    for key, item in value.items():
                        if str(key).lower() == lowered:
                            next_values.append(item)
                            break
            elif isinstance(value, list) and token.isdigit():
                index = int(token)
                if 0 <= index < len(value):
                    next_values.append(value[index])
        values = next_values
        if not values:
            break
    return values


def first_json_value(data: Any, paths: str | list[str] | tuple[str, ...], defaults: str = "") -> Any | None:
    for path in split_path_alternatives(paths, defaults):
        for value in json_path_values(data, path):
            if value is None:
                continue
            if isinstance(value, str) and not clean_text(value):
                continue
            return value
    return None


def first_json_text(data: Any, paths: str | list[str] | tuple[str, ...], defaults: str = "") -> str:
    value = first_json_value(data, paths, defaults)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return ""
    return clean_text(str(value))


def normalize_json_items(values: list[Any]) -> list[dict[str, Any]]:
    if len(values) == 1 and isinstance(values[0], list):
        values = values[0]
    if len(values) == 1 and isinstance(values[0], dict):
        nested_items = auto_find_product_items(values[0], max_depth=1)
        if nested_items:
            return nested_items
    return [item for item in values if isinstance(item, dict)]


def auto_find_product_items(data: Any, max_depth: int = 4) -> list[dict[str, Any]]:
    best: list[dict[str, Any]] = []
    def score_item(item: dict[str, Any]) -> int:
        keys = {str(key).lower() for key in item.keys()}
        return (
            (3 if keys & {"name", "title", "product_name", "goods_name", "plan_name"} else 0)
            + (3 if keys & {"stock", "inventory", "qty", "quantity", "available", "stock_count", "count"} else 0)
            + (2 if keys & {"price", "amount", "monthly_price", "sale_price"} else 0)
            + (1 if keys & {"url", "link", "purchase_url", "buy_url", "order_url", "href"} else 0)
        )
    def walk(value: Any, depth: int) -> None:
        nonlocal best
        if depth > max_depth:
            return
        if isinstance(value, list):
            dict_items = [item for item in value if isinstance(item, dict)]
            if dict_items and sum(score_item(item) for item in dict_items[:10]) > 0 and len(dict_items) >= len(best):
                best = dict_items
            for item in value[:20]:
                walk(item, depth + 1)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item, depth + 1)
    walk(data, 0)
    return best


def find_json_product_items(data: Any, selector: str | None) -> list[dict[str, Any]]:
    for path in split_path_alternatives(selector or ""):
        items = normalize_json_items(json_path_values(data, path))
        if items:
            return items
    for path in JSON_PRODUCT_PATHS:
        items = normalize_json_items(json_path_values(data, path))
        if items:
            return items
    return auto_find_product_items(data)


def parse_json_stock(value: Any, stock_regex: str) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return extract_stock(clean_text(str(value)), stock_regex)


def parse_api_products(data: Any, config: dict[str, Any]) -> list[Product]:
    items = find_json_product_items(data, config.get("product_selector"))
    products: list[Product] = []
    stock_regex = config.get("stock_regex") or DEFAULT_CONFIG["stock_regex"]
    in_words = split_words(config.get("in_stock_words") or DEFAULT_CONFIG["in_stock_words"])
    out_words = split_words(config.get("out_of_stock_words") or DEFAULT_CONFIG["out_of_stock_words"])
    for index, item in enumerate(items, start=1):
        title = first_json_text(item, config.get("title_selector"), JSON_TITLE_PATHS) or f"API 商品 {index}"
        stock_value = first_json_value(item, config.get("stock_selector"), JSON_STOCK_PATHS)
        stock = parse_json_stock(stock_value, stock_regex)
        price = first_json_text(item, config.get("price_selector"), JSON_PRICE_PATHS) or extract_price_from_text(clean_text(str(item)))
        status_value = first_json_value(item, config.get("button_selector"), JSON_STATUS_PATHS)
        status_text = first_json_text(item, config.get("button_selector"), JSON_STATUS_PATHS)
        raw_link = first_json_text(item, config.get("link_selector"), JSON_LINK_PATHS)
        absolute_link = urljoin(config["url"], raw_link) if raw_link else config["url"]
        purchase_url = apply_aff_template(absolute_link, config.get("aff_template", ""))
        says_sold_out = contains_any(status_text, out_words)
        says_buyable = contains_any(status_text, in_words)
        if isinstance(status_value, bool) and stock is None:
            available = status_value and not says_sold_out
        elif stock is not None:
            available = stock > 0 and not says_sold_out
        elif status_text:
            available = says_buyable and not says_sold_out
        else:
            available = False
        if available:
            status = "in_stock"
        elif stock == 0 or says_sold_out:
            status = "out_of_stock"
        else:
            status = "unknown"
        products.append(Product(product_key(title, price, absolute_link, clean_text(str(item))), title, status, available, stock, price, status_text, purchase_url))
    return dedupe_products(products)


def api_headers_from_cookie_header(value: str) -> dict[str, str]:
    text = str(value or "").strip()
    if not text:
        return {}
    header_lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    if len(header_lines) > 1 or any(":" in line for line in header_lines):
        headers: dict[str, str] = {}
        for line in header_lines:
            if ":" not in line:
                continue
            key, header_value = line.split(":", 1)
            key = key.strip()
            if key:
                headers[key] = header_value.strip()
        if headers:
            return headers
    if text.lower().startswith(("bearer ", "basic ")):
        return {"Authorization": text}
    return {"Cookie": text}


def fetch_products_with_api(config: dict[str, Any], timeout: int) -> list[Product]:
    headers = dict(API_REQUEST_HEADERS)
    headers.update(api_headers_from_cookie_header(str(config.get("cookie_header") or "")))
    response = requests.get(config["url"], headers=headers, timeout=timeout)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("API mode expected JSON but the target did not return valid JSON.") from exc
    products = parse_api_products(data, config)
    if not products:
        raise RuntimeError("API mode did not find product items. Set product path, title path, stock path, price path and link path in the selector fields.")
    return products


def cookie_header_to_playwright_cookies(cookie_header: str, url: str) -> list[dict[str, Any]]:
    header = clean_text(cookie_header)
    if not header:
        return []
    split = urlsplit(url)
    cookies: list[dict[str, Any]] = []
    for part in header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append({"name": name, "value": value.strip(), "domain": split.hostname or "", "path": "/", "secure": split.scheme == "https", "httpOnly": False, "sameSite": "Lax"})
    return cookies


def fetch_html_with_browser(config: dict[str, Any], timeout: int) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Browser mode requires Playwright. Rebuild the Docker image: docker compose up -d --build") from exc
    wait_seconds = max(0, int(config.get("browser_wait_seconds") or 0))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        cookie_header = clean_text(str(config.get("cookie_header") or ""))
        extra_headers = {key: value for key, value in REQUEST_HEADERS.items() if key != "User-Agent"}
        context = browser.new_context(user_agent=REQUEST_HEADERS["User-Agent"], locale="zh-CN", timezone_id="Asia/Shanghai", viewport={"width": 1365, "height": 768}, ignore_https_errors=True, extra_http_headers=extra_headers)
        cookies = cookie_header_to_playwright_cookies(cookie_header, config["url"])
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        try:
            try:
                page.goto(config["url"], wait_until="domcontentloaded", timeout=timeout * 1000)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("Browser mode timed out while loading target page. The site may be under anti-bot challenge or network throttling. Try increasing browser wait seconds, using a valid Cookie header, or running from a residential IP.") from exc
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
    elif backend in {"api", "api_json", "json"}:
        return fetch_products_with_api(config, timeout)
    else:
        html_text = fetch_html_with_requests(config, timeout)
    return parse_products(html_text, config)


def find_restocked_products(products: list[Product], previous_products: dict[str, dict[str, Any]]) -> list[Product]:
    restocked: list[Product] = []
    for product in products:
        if not product.available:
            continue
        previous = find_previous_product_state(product, previous_products)
        if not previous:
            restocked.append(product)
            continue
        if not bool(previous.get("restock_notified", True)) or not bool(previous.get("available")) or previous.get("stock") != product.stock:
            restocked.append(product)
    return restocked


def find_previous_product_state(product: Product, previous_products: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    previous = previous_products.get(product.key)
    if previous:
        return previous
    product_title = clean_text(product.title).casefold()
    product_price = clean_text(product.price).casefold()
    product_url = normalize_product_url(product.purchase_url)
    for item in previous_products.values():
        previous_title = clean_text(str(item.get("title") or "")).casefold()
        previous_price = clean_text(str(item.get("price") or "")).casefold()
        previous_url = normalize_product_url(str(item.get("purchase_url") or ""))
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


def telegram_product_card(monitor_name: str, product: Product, stock_transition: str | None = None) -> str:
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
