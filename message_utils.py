from __future__ import annotations

from string import Formatter
from typing import Optional

# 提取内容上限：超过此长度的消息文本会被截断，避免喂给 AI 过多 token
MAX_EXTRACTED_TEXT_LENGTH = 2000

# AI 输出字段上限：reason / mock_text 由 AI 生成，可能超过 50 字
MAX_REASON_LENGTH = 500
MAX_MOCK_TEXT_LENGTH = 200

DEFAULT_BAN_NOTICE_TEMPLATE = (
    "\\#封禁预警\n"
    "[{masked_name}]({user_link}) 请注意，你的用户名或发言存在违规\n"
    "⚠️已被AI判断为高风险用户，永久封禁\n"
    "风险分数：{score}\n"
    "📋 违规原因：\n```\n{reason}\n```\n"
    "🤖 AI 嘲讽：\n```\n{mock}\n```"
)

BAN_NOTICE_TEMPLATE_FIELDS = frozenset({
    "masked_name",
    "user_link",
    "score",
    "reason",
    "mock",
    "user_id",
    "chat_id",
    "channel_url",
    "group_url",
})


def escape_markdown_v2(text: str) -> str:
    """转义 MarkdownV2 特殊字符"""
    if text is None:
        return ""

    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


def process_nickname(nickname: str) -> str:
    """
    处理用户名，用 spoiler 隐藏中间部分
    和 Go 版本一致：使用 || 包裹中间部分
    """
    if not nickname or not nickname.strip():
        return "未知用户"

    # 必须先按原始字符切片再分段转义：先转义会把 "\x" 这种两字符转义序列切断，
    # 产生悬空反斜杠导致 MarkdownV2 解析失败、封禁通知发不出去。
    name = nickname.strip()
    length = len(name)
    if length == 1:
        return f"||{escape_markdown_v2(name)}||"
    if length == 2:
        return f"{escape_markdown_v2(name[0])}||{escape_markdown_v2(name[1])}||"
    return (
        f"{escape_markdown_v2(name[0])}"
        f"||{escape_markdown_v2(name[1:-1])}||"
        f"{escape_markdown_v2(name[-1])}"
    )


FORWARD_METADATA_FIELDS = (
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "forward_date",
    "forward_from_message_id",
)


def is_forwarded_message(message) -> bool:
    """判断消息是否为转发消息。"""
    return any(getattr(message, field, None) is not None for field in FORWARD_METADATA_FIELDS)


def _collect_visible_parts(message, *, text_label: str, caption_label: str):
    parts = []
    if message is None:
        return parts

    text = getattr(message, "text", None)
    caption = getattr(message, "caption", None)

    if text:
        parts.append((text_label, text))
    if caption:
        parts.append((caption_label, caption))

    return parts


def extract_message_text(message) -> str:
    """提取用于风控判断的可见文本，覆盖正文、caption、引用片段、回复原文和转发原文。"""
    parts = []

    if is_forwarded_message(message):
        parts.extend(_collect_visible_parts(message, text_label="转发正文", caption_label="转发说明"))
    else:
        parts.extend(_collect_visible_parts(message, text_label="正文", caption_label="说明"))

    quote = getattr(message, "quote", None)
    quote_text = getattr(quote, "text", None)
    if quote_text:
        parts.append(("引用片段", quote_text))

    reply_to_message = getattr(message, "reply_to_message", None)
    if reply_to_message:
        parts.extend(_collect_visible_parts(reply_to_message, text_label="回复原文", caption_label="回复说明"))

    normalized_parts = []
    seen_values = set()
    for label, value in parts:
        cleaned = str(value).strip()
        if cleaned and cleaned not in seen_values:
            normalized_parts.append(f"{label}: {cleaned}")
            seen_values.add(cleaned)

    result = "\n".join(normalized_parts)
    if len(result) > MAX_EXTRACTED_TEXT_LENGTH:
        result = result[:MAX_EXTRACTED_TEXT_LENGTH]
    return result


def build_ban_notice(
    template: Optional[str],
    *,
    masked_name: str,
    user_link: str,
    score: int,
    reason: str,
    mock_text: str,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    channel_url: str = "",
    group_url: str = "",
    logger=None,
) -> str:
    """构造封禁通知，支持自定义模板和安全兜底。"""
    selected_template = template if isinstance(template, str) and template.strip() else DEFAULT_BAN_NOTICE_TEMPLATE

    context = {
        "masked_name": masked_name,
        "user_link": user_link,
        "score": score,
        "reason": escape_markdown_v2((reason or "无")[:MAX_REASON_LENGTH]),
        "mock": escape_markdown_v2((mock_text or "无")[:MAX_MOCK_TEXT_LENGTH]),
        "user_id": user_id if user_id is not None else "",
        "chat_id": chat_id if chat_id is not None else "",
        "channel_url": channel_url,
        "group_url": group_url,
    }

    try:
        fields = {
            field_name
            for _, field_name, _, _ in Formatter().parse(selected_template)
            if field_name
        }
    except ValueError as exc:
        if logger:
            logger.warning(f"Invalid ban notice template, fallback to default: {exc}")
        selected_template = DEFAULT_BAN_NOTICE_TEMPLATE
        fields = set()

    unknown_fields = fields - BAN_NOTICE_TEMPLATE_FIELDS
    if unknown_fields:
        if logger:
            logger.warning(
                "Unknown placeholders in ban notice template: %s; fallback to default",
                ", ".join(sorted(unknown_fields)),
            )
        selected_template = DEFAULT_BAN_NOTICE_TEMPLATE

    try:
        return selected_template.format(**context)
    except (KeyError, ValueError) as exc:
        if logger:
            logger.warning(f"Failed to render ban notice template, fallback to default: {exc}")
        return DEFAULT_BAN_NOTICE_TEMPLATE.format(**context)
