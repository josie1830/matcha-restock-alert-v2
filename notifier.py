"""LINE notifications for the matcha restock watcher."""

from __future__ import annotations

import logging
import os
from typing import Iterable

import requests

from stores.common import Item

log = logging.getLogger(__name__)

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
MAX_TEXT_LENGTH = 4500


def _credentials() -> tuple[str, str]:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("LINE_USER_ID", "").strip()

    if not token or not user_id:
        raise RuntimeError(
            "Missing LINE_CHANNEL_ACCESS_TOKEN or LINE_USER_ID environment variable."
        )

    return token, user_id


def _send_text(message: str) -> bool:
    token, user_id = _credentials()

    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": message[:MAX_TEXT_LENGTH]}],
    }

    try:
        response = requests.post(
            LINE_PUSH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        log.info("LINE notification sent successfully.")
        return True
    except requests.RequestException as exc:
        log.error("LINE notification failed: %s", exc)
        if getattr(exc, "response", None) is not None:
            log.error("LINE API response: %s", exc.response.text[:1000])
        return False


def _is_priority(item: Item) -> bool:
    if item.extra.get("priority"):
        return True

    text = f"{item.name} {item.label} {item.url}".lower()
    keywords = (
        "seasonal",
        "limited",
        "christmas",
        "xmas",
        "winter",
        "holiday",
        "seiya",
        "聖夜",
        "限定",
        "季節",
        "クリスマス",
        "冬",
    )
    return any(word in text for word in keywords)


def notify_text(webhook_url: str, message: str) -> bool:
    """Send a plain-text LINE notification."""
    return _send_text(message)


def notify_restocks(
    webhook_url: str,
    items: Iterable[Item],
    mention: str = "",
) -> bool:
    """Send restocked products to LINE, with seasonal items first."""
    items = sorted(
        items,
        key=lambda item: (
            not _is_priority(item),
            item.name.lower(),
            item.variant or "",
        ),
    )

    if not items:
        return True

    lines = ["🍵 丸久小山園補貨通知", ""]

    for item in items:
        if _is_priority(item):
            lines.append("🎄【季節／限定優先】")

        lines.append(f"✅ {item.label}")

        if item.price:
            lines.append(f"價格：{item.price}")

        lines.append(item.url)
        lines.append("")

    message = "\n".join(lines).strip()

    if len(message) <= MAX_TEXT_LENGTH:
        return _send_text(message)

    chunks: list[str] = []
    current = "🍵 丸久小山園補貨通知\n\n"

    for line in lines[2:]:
        addition = line + "\n"

        if len(current) + len(addition) > MAX_TEXT_LENGTH:
            chunks.append(current.strip())
            current = "🍵 丸久小山園補貨通知（續）\n\n"

        current += addition

    if current.strip():
        chunks.append(current.strip())

    success = True

    for chunk in chunks:
        if not _send_text(chunk):
            success = False

    return success
