from types import SimpleNamespace

from message_utils import extract_message_text


def test_extract_message_text_includes_text_caption_and_quote_without_duplicates():
    message = SimpleNamespace(
        text="你好",
        caption="图片说明",
        quote=SimpleNamespace(text="引用内容"),
    )

    content = extract_message_text(message)

    assert content == "正文: 你好\n说明: 图片说明\n引用片段: 引用内容"


def test_extract_message_text_includes_reply_content():
    message = SimpleNamespace(
        text="当前消息",
        caption=None,
        quote=None,
        reply_to_message=SimpleNamespace(
            text="被回复正文",
            caption="被回复说明",
        ),
    )

    content = extract_message_text(message)

    assert content == "正文: 当前消息\n回复原文: 被回复正文\n回复说明: 被回复说明"


def test_extract_message_text_marks_forwarded_visible_content():
    message = SimpleNamespace(
        text="转发过来的原文",
        caption="转发说明",
        quote=None,
        reply_to_message=None,
        forward_origin=object(),
    )

    content = extract_message_text(message)

    assert content == "转发正文: 转发过来的原文\n转发说明: 转发说明"


def test_extract_message_text_deduplicates_same_visible_content():
    message = SimpleNamespace(
        text="重复内容",
        caption=None,
        quote=SimpleNamespace(text="重复内容"),
        reply_to_message=SimpleNamespace(
            text="重复内容",
            caption=None,
        ),
    )

    content = extract_message_text(message)

    assert content == "正文: 重复内容"


def test_extract_message_text_returns_empty_string_when_nothing_visible():
    message = SimpleNamespace(text=None, caption=None, quote=None, reply_to_message=None)

    assert extract_message_text(message) == ""


def test_extract_message_text_truncates_oversized_content():
    huge = "x" * 5000
    message = SimpleNamespace(text=huge, caption=None, quote=None, reply_to_message=None)

    result = extract_message_text(message)

    assert len(result) == 2000
