"""Marukyu Koyamaen English shop stock scraper."""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .common import Item, StoreError, matches_watchlist, polite_get

log = logging.getLogger(__name__)

STORE = "Marukyu Koyamaen"
BASE = "https://www.marukyu-koyamaen.co.jp"

DEFAULT_CATALOGS = [
    f"{BASE}/english/shop/products/catalog/matcha?viewall=1",
]

DEFAULT_SEASONAL_CATALOGS = [
    f"{BASE}/english/shop/products/catalog/seasonal?viewall=1",
]

PRODUCT_PATH = re.compile(
    r"^/english/shop/products/(?!catalog/)([0-9a-z]{6,14})/?$",
    re.I,
)

OUT_OF_STOCK_TEXT = re.compile(
    r"out\s+of\s+stock\s+and\s+unavailable", re.I
)

EXCLUDE_KEYWORDS = (
    "sweetened", "matcha mix", "mix powder", "latte",
    "genmaicha", "hojicha", "sencha", "gyokuro",
    "tea bag", "tea bags", "green tea",
    "玄米茶", "ほうじ茶", "煎茶", "玉露",
    "ミックス", "ラテ",
)

SEASONAL_KEYWORDS = (
    "seasonal", "limited", "christmas", "xmas",
    "winter", "holiday", "seiya", "聖夜",
    "限定", "季節", "クリスマス", "冬",
)


def is_excluded(name: str) -> bool:
    lowered = name.lower()
    return any(
        word in lowered or word in name
        for word in EXCLUDE_KEYWORDS
    )


def is_seasonal(name: str, url: str) -> bool:
    text = f"{name} {url}".lower()
    return any(word in text for word in SEASONAL_KEYWORDS)


def discover_products(
    session,
    catalogs: Iterable[str],
    delay: float,
) -> list[tuple[str, str]]:
    found: dict[str, str] = {}

    for catalog_url in catalogs:
        resp = polite_get(session, catalog_url, delay=delay)
        soup = BeautifulSoup(resp.text, "html.parser")

        for anchor in soup.find_all("a", href=True):
            absolute = urljoin(BASE, anchor["href"])
            path = absolute.replace(BASE, "").split("?")[0]

            if not PRODUCT_PATH.match(path):
                continue

            name = (
                anchor.get("title")
                or anchor.get_text(" ", strip=True)
                or ""
            ).strip()

            if absolute not in found or (
                name and len(name) < len(found[absolute])
            ):
                found[absolute] = name

    if not found:
        raise StoreError(
            "No product links found on the catalog page. "
            "The page may have changed or blocked the request."
        )

    return sorted(found.items())


def _variants_from_form_json(
    soup: BeautifulSoup,
) -> Optional[list[dict]]:
    form = soup.find(attrs={"data-product_variations": True})
    if not form:
        return None

    raw = form.get("data-product_variations")
    if not raw or raw in ("false", "[]"):
        return None

    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None

    return data if isinstance(data, list) and data else None


def _variants_from_dom(
    soup: BeautifulSoup,
) -> Optional[list[dict]]:
    results: list[dict] = []

    for node in soup.find_all(
        class_=re.compile(r"(variation|product-sku|item-sku|spec)", re.I)
    ):
        text = node.get_text(" ", strip=True)
        sku_match = re.search(r"\b([0-9A-Z]{9,12})\b", text)
        if not sku_match:
            continue

        stock_node = node.find(
            class_=re.compile(r"(out-of-stock|in-stock|stock)", re.I)
        )
        if stock_node is None:
            continue

        classes = " ".join(stock_node.get("class", []))
        stock_text = stock_node.get_text(" ", strip=True)
        out = (
            "out-of-stock" in classes.lower()
            or bool(OUT_OF_STOCK_TEXT.search(stock_text))
        )

        size_match = re.search(r"(\d+\s*g[^,\n]*)", text, re.I)

        results.append({
            "sku": sku_match.group(1),
            "is_in_stock": not out,
            "variant": size_match.group(1).strip() if size_match else None,
        })

    return results or None


def _price_for(soup: BeautifulSoup) -> Optional[str]:
    node = soup.find(class_=re.compile(r"price", re.I))
    if not node:
        return None

    text = node.get_text(" ", strip=True)
    match = re.search(r"[¥￥]\s?[\d,]+", text)
    return match.group(0) if match else (text[:40] or None)


def scrape_product(
    session,
    url: str,
    fallback_name: str,
    delay: float,
) -> list[Item]:
    resp = polite_get(session, url, delay=delay)
    soup = BeautifulSoup(resp.text, "html.parser")

    heading = soup.find("h1")
    name = (
        fallback_name
        or (heading.get_text(strip=True) if heading else url.rsplit("/", 1)[-1])
    )

    price = _price_for(soup)
    variations = _variants_from_form_json(soup)

    if variations:
        items = []
        for var in variations:
            sku = str(var.get("sku") or var.get("variation_id") or "")
            attrs = var.get("attributes") or {}
            variant = next(
                (str(v) for v in attrs.values() if v), None
            ) or sku

            items.append(Item(
                key=f"marukyu:{sku or variant}",
                store=STORE,
                name=name,
                variant=variant,
                url=url,
                price=(
                    f"¥{var['display_price']:,}"
                    if var.get("display_price") is not None
                    else price
                ),
                available=bool(var.get("is_in_stock")),
                detection="variations-json",
            ))
        return items

    dom_variants = _variants_from_dom(soup)
    if dom_variants:
        return [
            Item(
                key=f"marukyu:{var['sku']}",
                store=STORE,
                name=name,
                variant=var.get("variant") or var["sku"],
                url=url,
                price=price,
                available=bool(var["is_in_stock"]),
                detection="dom-variant",
            )
            for var in dom_variants
        ]

    page_text = soup.get_text(" ", strip=True)
    out_of_stock = bool(OUT_OF_STOCK_TEXT.search(page_text))
    slug = url.rstrip("/").rsplit("/", 1)[-1]

    return [
        Item(
            key=f"marukyu:{slug}",
            store=STORE,
            name=name,
            variant=None,
            url=url,
            price=price,
            available=not out_of_stock,
            detection="page-text",
        )
    ]


def fetch(session, config: dict) -> list[Item]:
    delay = float(config.get("request_delay_seconds", 1.5))
    regular_catalogs = config.get("catalog_urls") or DEFAULT_CATALOGS
    seasonal_catalogs = (
        config.get("seasonal_catalog_urls")
        or DEFAULT_SEASONAL_CATALOGS
    )

    regular = discover_products(session, regular_catalogs, delay)
    seasonal = discover_products(session, seasonal_catalogs, delay)

    products: dict[str, tuple[str, bool]] = {}

    for url, name in regular:
        products[url] = (name, False)

    for url, name in seasonal:
        old_name, _ = products.get(url, (name, False))
        products[url] = (name or old_name, True)

    watchlist = config.get("watchlist") or []
    items: list[Item] = []

    for url, (name, from_seasonal_catalog) in sorted(products.items()):
        if is_excluded(name):
            continue

        # The regular matcha catalog itself defines the matcha category.
        # Seasonal products must identify themselves as matcha by name,
        # except the known Seiya no Shiro Christmas matcha.
        if from_seasonal_catalog and not (
            "matcha" in name.lower()
            or "抹茶" in name
            or "seiya" in name.lower()
            or "聖夜" in name
        ):
            continue

        if not matches_watchlist(name or url, watchlist):
            continue

        try:
            product_items = scrape_product(session, url, name, delay)

            priority = (
                from_seasonal_catalog
                or is_seasonal(name, url)
            )

            for item in product_items:
                if priority:
                    item.extra["priority"] = "seasonal"

            items.extend(product_items)

        except StoreError as exc:
            log.warning("Marukyu: skipping %s (%s)", url, exc)

    log.info("Marukyu: read stock for %d variants", len(items))
    return items
