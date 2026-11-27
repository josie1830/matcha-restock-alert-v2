#!/usr/bin/env python3
"""Matcha restock watcher with LINE notifications."""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from typing import Callable

import notifier
import schedule_window
from state import StockState
from stores import horii, marukyu
from stores.common import Item, StoreError, make_session

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DEFAULT_CONFIG_PATH = "config.json"

STORES: dict[str, Callable] = {
    "marukyu": marukyu.fetch,
    "horii": horii.fetch,
}

log = logging.getLogger("bot")

DEFAULT_CONFIG = {
    "state_file": "state.json",
    "check_interval_seconds": 300,
    "jitter_seconds": 30,
    "announce_startup": True,
    "report_errors_to_line": True,
    "stores": {
        "marukyu": {
            "enabled": True,
            "request_delay_seconds": 1.5,
            "watchlist": [],
        },
        "horii": {
            "enabled": False,
            "request_delay_seconds": 1.0,
            "watchlist": [],
        },
    },
}


def load_config(path: str) -> dict:
    if os.path.exists(path):
        import json

        with open(path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    else:
        log.info("No config file found; using defaults.")
        config = DEFAULT_CONFIG.copy()

    return config


def build_session(config: dict):
    ua = config.get(
        "user_agent",
        "MatchaRestockWatcher/1.0 (personal restock notifier)",
    )
    return make_session(ua)


def poll_store(name: str, session, config: dict) -> list[Item]:
    store_config = config.get("stores", {}).get(name, {})
    if not store_config.get("enabled", True):
        return []
    return STORES[name](session, store_config)


def run_cycle(
    config: dict,
    state: StockState,
    session,
    dry_run: bool = False,
) -> list[Item]:
    all_items: list[Item] = []
    failures: list[str] = []

    for name in STORES:
        try:
            items = poll_store(name, session, config)
            all_items.extend(items)
        except StoreError as exc:
            log.error("%s: %s", name, exc)
            failures.append(f"{name}: {exc}")
        except Exception as exc:
            log.exception("%s: unexpected error", name)
            failures.append(f"{name}: {exc}")

    if failures and config.get("report_errors_to_line", True) and not dry_run:
        notifier.notify_text(
            "",
            "⚠️ 丸久小山園補貨監控發生問題：\n"
            + "\n".join(f"• {failure}" for failure in failures),
        )

    if dry_run:
        return all_items

    if failures or not all_items:
        log.warning("Incomplete stock data; keeping previous state unchanged.")
        return []

    restocked = state.diff(all_items)

    if restocked:
        log.info("RESTOCK: %d item(s) came back in stock", len(restocked))
        for item in restocked:
            log.info("  -> [%s] %s | %s", item.store, item.label, item.url)

        notifier.notify_restocks("", restocked)
    else:
        log.info("No restocks this cycle (%d items checked)", len(all_items))

    return restocked


def print_stock_table(items: list[Item]) -> None:
    if not items:
        print("No items found.")
        return

    by_store: dict[str, list[Item]] = {}
    for item in items:
        by_store.setdefault(item.store, []).append(item)

    for store, store_items in by_store.items():
        in_stock = [item for item in store_items if item.available]
        print(f"\n=== {store} — {len(in_stock)}/{len(store_items)} in stock ===")

        for item in sorted(
            store_items,
            key=lambda item: (not item.available, item.name),
        ):
            mark = "✅ IN STOCK " if item.available else "❌ sold out "
            price = f" {item.price}" if item.price else ""
            print(f"{mark} {item.label}{price}")

            if item.available:
                print(f"             {item.url}")

        methods = {item.detection for item in store_items}
        print(f"    (detection method: {', '.join(sorted(methods))})")


def dump_raw(store: str, config: dict) -> None:
    import json

    session = build_session(config)
    os.makedirs("dumps", exist_ok=True)

    if store == "marukyu":
        url = marukyu.DEFAULT_CATALOGS[0]
        resp = session.get(url, timeout=30)

        with open("dumps/marukyu_catalog.html", "w", encoding="utf-8") as handle:
            handle.write(resp.text)

        products = marukyu.discover_products(
            session,
            marukyu.DEFAULT_CATALOGS,
            1.5,
        )

        if products:
            product_url, _ = products[0]
            resp = session.get(product_url, timeout=30)

            with open("dumps/marukyu_product.html", "w", encoding="utf-8") as handle:
                handle.write(resp.text)

            print(f"Saved product page: {product_url}")

    elif store == "horii":
        resp = session.get(
            f"{horii.PRODUCTS_JSON}?limit=250&page=1",
            timeout=30,
        )

        with open("dumps/horii_products.json", "w", encoding="utf-8") as handle:
            handle.write(resp.text)

        print("Saved Horii product data.")

    else:
        sys.exit(f"Unknown store '{store}'.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Matcha restock watcher")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--notify-current-stock", action="store_true")
    parser.add_argument("--dump", metavar="STORE")
    parser.add_argument("--ignore-schedule", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
    )

    config = load_config(args.config)

    if args.dump:
        dump_raw(args.dump, config)
        return

    session = build_session(config)
    if args.notify_current_stock:
        items = []
        try:
            for name in STORES:
                items.extend(poll_store(name, session, config))
        except Exception as exc:
            log.exception("Stock check failed.")
            sys.exit(f"Stock check failed: {exc}")

        if not items:
            sys.exit("No product data found; no LINE message sent.")

        print_stock_table(items)
        in_stock = [item for item in items if item.available]

        if in_stock:
            sent = notifier.notify_restocks("", in_stock)
        else:
            sent = notifier.notify_text(
                "",
                "🍵 LINE 通知測試成功！目前沒有偵測到有庫存的商品。",
            )

        if not sent:
            sys.exit("LINE notification failed.")

        return

    if args.test:
        items = run_cycle(
            config,
            StockState(config.get("state_file", "state.json")),
            session,
            dry_run=True,
        )
        print_stock_table(items)
        return
        state = StockState(config.get("state_file", "state.json"))

    if config.get("announce_startup", True) and not state.seeded:
        notifier.notify_text(
            "",
            "🍵 丸久小山園補貨監控已啟動！\n"
            "正在記錄目前庫存，下一次檢查開始偵測補貨。",
        )

    interval = int(config.get("check_interval_seconds", 300))
    jitter = int(config.get("jitter_seconds", 30))

    if args.once:
        allowed, reason = schedule_window.check_window(config)

        if allowed or args.ignore_schedule:
            if not allowed:
                log.info("Running anyway: %s", reason)
            run_cycle(config, state, session)
        else:
            log.info("Skipping this run: %s", reason)

        return

    log.info("Watching every ~%ds (± %ds).", interval, jitter)
    consecutive_failures = 0

    while True:
        started = time.time()

        try:
            allowed, reason = schedule_window.check_window(config)

            if allowed or args.ignore_schedule:
                run_cycle(config, state, session)
            else:
                log.info("Skipping this cycle: %s", reason)

            consecutive_failures = 0

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            return

        except Exception:
            consecutive_failures += 1
            log.exception(
                "Cycle failed (%d in a row)",
                consecutive_failures,
            )

        wait = interval + random.randint(-jitter, jitter)

        if consecutive_failures:
            wait = min(3600, wait * (2 ** consecutive_failures))

        elapsed = time.time() - started
        sleep_for = max(30, wait - elapsed)

        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            return


if __name__ == "__main__":
    main()
