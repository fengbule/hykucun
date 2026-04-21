from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup


DEFAULT_URL = "https://www.heyunidc.cn/cart?fid=49&gid=97"

DEFAULT_CONFIG: dict[str, Any] = {
    "name": "核云周年庆特惠",
    "url": DEFAULT_URL,
    "product_selector": ".product-card",
    "title_selector": ".product-card-header h5, h5",
    "stock_selector": ".stock-info",
    "price_selector": ".pricing-info",
    "button_selector": ".buy-now-button",
    "link_selector": ".buy-now-button[href], a[href]",
    "stock_regex": r"库存\s*[:：]?\s*(\d+)",
    "in_stock_words": "立即购买,加入购物车,购买,开通,下单",
    "out_of_stock_words": "产品已售罄,已售罄,售罄,缺货,无货,暂无库存",
    "aff_template": "",
}

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
    try:
        match = re.search(stock_regex, stock_text)
    except re.error:
        match = re.search(DEFAULT_CONFIG["stock_regex"], stock_text)
    return int(match.group(1)) if match else None


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
        joiner = "&" if "?" in url else "?"
        return f"{url}{joiner}{template.lstrip('?&')}"

    return f"{template}{url}"


def product_key(title: str, price: str, purchase_url: str, fallback_text: str) -> str:
    raw = "|".join([title, price, purchase_url, fallback_text[:120]])
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
    button_says_sold_out = "SellOut" in node_classes(button_node) or any(
        word in button_text for word in out_words
    )
    button_says_buyable = any(word in button_text for word in in_words)

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
    return [parse_product_card(card, config) for card in cards]


def fetch_products(config: dict[str, Any], timeout: int = 15) -> list[Product]:
    response = requests.get(
        config["url"],
        headers=REQUEST_HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_products(response.text, config)


def find_restocked_products(
    products: list[Product], previous_products: dict[str, dict[str, Any]]
) -> list[Product]:
    restocked: list[Product] = []
    for product in products:
        if not product.available:
            continue

        previous = previous_products.get(product.key)
        if not previous:
            restocked.append(product)
            continue

        previous_stock = previous.get("stock")
        stock_increased = (
            isinstance(previous_stock, int)
            and product.stock is not None
            and product.stock > previous_stock
        )
        if not previous.get("available") or stock_increased:
            restocked.append(product)
    return restocked


def stock_label(product: Product) -> str:
    return "未知" if product.stock is None else str(product.stock)


def telegram_product_card(monitor_name: str, product: Product) -> str:
    lines = [
        f"<b>{html.escape(monitor_name)}</b>",
        "",
        f"<b>{html.escape(product.title)}</b>",
        f"库存：{html.escape(stock_label(product))}",
        f"价格：{html.escape(product.price or '未知')}",
        f"状态：{html.escape(product.button or product.status)}",
        "",
        f'<a href="{html.escape(product.purchase_url, quote=True)}">购买链接</a>',
    ]
    return "\n".join(lines)
