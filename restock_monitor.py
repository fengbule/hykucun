#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from monitor_core import (
    DEFAULT_CONFIG,
    fetch_products,
    find_previous_product_state,
    find_restocked_products,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check product stock from the command line.")
    parser.add_argument("--url", default=DEFAULT_CONFIG["url"])
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--state-file", default=".restock_state.json")
    parser.add_argument("--aff-template", default="")
    parser.add_argument("--request-backend", choices=["requests", "browser", "api", "api_json", "json"], default="requests")
    parser.add_argument("--browser-wait-seconds", type=int, default=8)
    parser.add_argument("--cookie-header", default="")
    parser.add_argument("--product-selector", default=DEFAULT_CONFIG["product_selector"])
    parser.add_argument("--title-selector", default=DEFAULT_CONFIG["title_selector"])
    parser.add_argument("--stock-selector", default=DEFAULT_CONFIG["stock_selector"])
    parser.add_argument("--price-selector", default=DEFAULT_CONFIG["price_selector"])
    parser.add_argument("--button-selector", default=DEFAULT_CONFIG["button_selector"])
    parser.add_argument("--link-selector", default=DEFAULT_CONFIG["link_selector"])
    parser.add_argument("--stock-regex", default=DEFAULT_CONFIG["stock_regex"])
    parser.add_argument("--in-stock-words", default=DEFAULT_CONFIG["in_stock_words"])
    parser.add_argument("--out-of-stock-words", default=DEFAULT_CONFIG["out_of_stock_words"])
    parser.add_argument(
        "--notification-mode",
        choices=["restock_only", "realtime"],
        default=DEFAULT_CONFIG.get("notification_mode", "restock_only"),
    )
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: Path, products: dict) -> None:
    path.write_text(json.dumps({"products": products}, ensure_ascii=False, indent=2), encoding="utf-8")


def args_to_config(args: argparse.Namespace) -> dict:
    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "url": args.url,
            "aff_template": args.aff_template,
            "request_backend": args.request_backend,
            "browser_wait_seconds": args.browser_wait_seconds,
            "cookie_header": args.cookie_header,
            "product_selector": args.product_selector,
            "title_selector": args.title_selector,
            "stock_selector": args.stock_selector,
            "price_selector": args.price_selector,
            "button_selector": args.button_selector,
            "link_selector": args.link_selector,
            "stock_regex": args.stock_regex,
            "in_stock_words": args.in_stock_words,
            "out_of_stock_words": args.out_of_stock_words,
            "notification_mode": args.notification_mode,
        }
    )
    return config


def filter_restocked_products(
    restocked: list,
    previous_products: dict,
    notification_mode: str,
) -> list:
    if notification_mode == "realtime":
        return restocked
    filtered = []
    for product in restocked:
        previous = find_previous_product_state(product, previous_products)
        if (
            not previous
            or not bool(previous.get("available"))
            or not bool(previous.get("restock_notified", True))
        ):
            filtered.append(product)
    return filtered


def main() -> None:
    args = parse_args()
    config = args_to_config(args)
    state_file = Path(args.state_file)

    while True:
        state = load_state(state_file)
        products = fetch_products(config)
        previous_products = state.get("products", {})
        restocked_candidates = find_restocked_products(products, previous_products)
        restocked = filter_restocked_products(
            restocked_candidates,
            previous_products,
            config.get("notification_mode", DEFAULT_CONFIG.get("notification_mode", "restock_only")),
        )
        current = {}
        for product in products:
            product_state = product.to_dict()
            product_state["product_key"] = product.key
            product_state["restock_notified"] = bool(product.available)
            current[product.key] = product_state
        available = [product for product in products if product.available]

        print(
            f"checked={len(products)} available={len(available)} "
            f"restocked={len(restocked)}"
        )
        for product in products:
            stock = "未知" if product.stock is None else product.stock
            print(f"- {product.status:12} stock={stock} {product.title}")
            if product.available:
                print(f"  link={product.purchase_url}")

        save_state(state_file, current)
        if args.once:
            return
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
