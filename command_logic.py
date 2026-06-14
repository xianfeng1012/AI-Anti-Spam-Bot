from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence
from urllib.parse import urlparse

from database import Advertisement


@dataclass(frozen=True)
class CommandInputError(ValueError):
    code: str

    def __str__(self) -> str:
        return self.code


# Telegram inline 按钮允许的 URL 协议
# 注意：不放行 tg:// 协议——tg://resolve?domain= 等可指向任意 bot/频道/用户，
# 一旦 bot owner 账号被盗，攻击者就能塞钓鱼链接。仅允许 http(s)，用户可用 https://t.me/ 链接到任何目标。
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _validate_ad_url(url: str) -> None:
    """校验广告 URL 协议，拒绝 javascript: 等危险 scheme。"""
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        raise CommandInputError("invalid_url")
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise CommandInputError("invalid_url")


def parse_add_ad_payload(payload: str) -> Advertisement:
    parts = [part.strip() for part in payload.split("|")]
    if len(parts) != 4:
        raise CommandInputError("format")

    title, url, validity_str, sort_str = parts
    if not title or not url:
        raise CommandInputError("format")

    _validate_ad_url(url)

    try:
        validity = datetime.strptime(validity_str, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise CommandInputError("invalid_validity") from exc

    try:
        sort = int(sort_str)
    except ValueError as exc:
        raise CommandInputError("invalid_sort") from exc

    return Advertisement(
        title=title,
        url=url,
        sort=sort,
        validity_period=validity,
    )


def parse_delete_ad_args(args: Sequence[str]) -> int:
    if len(args) != 1:
        raise CommandInputError("usage")

    try:
        return int(args[0])
    except ValueError as exc:
        raise CommandInputError("invalid_id") from exc


def parse_unban_callback_data(callback_data: str) -> Optional[int]:
    if not callback_data or not callback_data.startswith("unban_"):
        return None

    try:
        return int(callback_data.split("_", 1)[1])
    except ValueError:
        return None


def resolve_unban_target(reply_to_message, args: Sequence[str]) -> tuple[int, Optional[str]]:
    if reply_to_message and getattr(reply_to_message, "from_user", None):
        from_user = reply_to_message.from_user
        return from_user.id, getattr(from_user, "first_name", None)

    if args:
        try:
            return int(args[0]), None
        except ValueError as exc:
            raise CommandInputError("invalid_id") from exc

    raise CommandInputError("usage")
