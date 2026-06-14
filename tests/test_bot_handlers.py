import asyncio
import importlib
import sys
from datetime import datetime
from types import ModuleType
from types import SimpleNamespace

import pytest


def fake_t(key: str, **kwargs) -> str:
    if not kwargs:
        return key
    rendered = ",".join(f"{name}={kwargs[name]}" for name in sorted(kwargs))
    return f"{key}|{rendered}"


class FakeMessage:
    def __init__(self, *, chat_id=100, reply_to_message=None):
        self.chat_id = chat_id
        self.reply_to_message = reply_to_message
        self.replies = []
        self.deleted = False

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))

    async def delete(self):
        self.deleted = True


class FakeBot:
    def __init__(self):
        self.restrict_calls = []
        self.send_calls = []
        self.files = {}
        self.sent_message_id = 900

    async def restrict_chat_member(self, **kwargs):
        self.restrict_calls.append(kwargs)

    async def send_message(self, chat_id, text, **kwargs):
        self.send_calls.append((chat_id, text, kwargs))
        self.sent_message_id += 1
        return SimpleNamespace(message_id=self.sent_message_id)

    async def get_file(self, file_id):
        return self.files[file_id]


class FakeDownloadedFile:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def download_as_bytearray(self):
        return bytearray(self.payload)


class FakeDB:
    def __init__(self, user=None):
        self.user = user
        self.saved_users = []
        self.incremented_messages = []
        self.incremented_verifications = []

    def get_user(self, user_id, chat_id):
        return self.user

    def save_user(self, user):
        self.user = user
        self.saved_users.append(user)

    def increment_message_count(self, user_id, chat_id):
        self.incremented_messages.append((user_id, chat_id))

    def increment_verification_times(self, user_id, chat_id):
        self.incremented_verifications.append((user_id, chat_id))


class FakeQuery:
    def __init__(self, *, data, from_user, message):
        self.data = data
        self.from_user = from_user
        self.message = message
        self.answers = []

    async def answer(self, text=None, show_alert=None):
        self.answers.append((text, show_alert))


class FakeLogger:
    def __init__(self):
        self.infos = []
        self.warnings = []
        self.errors = []

    def info(self, message, *args, **kwargs):
        self.infos.append(message % args if args else message)

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message % args if args else message)

    def error(self, message, *args, **kwargs):
        self.errors.append(message % args if args else message)


@pytest.fixture
def bot_module(monkeypatch, tmp_path):
    values = {
        "ai_model": "openai",
        "openai.api_key": "test-key",
        "openai.base_url": "https://example.com/v1",
        "openai.model": "gpt-test",
        "strategy.spam_score": 80,
        "strategy.joined_days": 3,
        "strategy.min_messages": 3,
        "strategy.verification_times": 0,
        "telegram.owners": ["1"],
        "message.delete_ban_notice_after_seconds": 30,
        "message.delete_welcome_message_after_seconds": 30,
    }

    monkeypatch.chdir(tmp_path)
    fake_config_module = ModuleType("config")
    fake_config_module.config = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    fake_ai_module = ModuleType("ai")
    fake_ai_module.create_ai_client = lambda: SimpleNamespace()
    fake_prompts_module = ModuleType("ai.prompts")
    fake_prompts_module.USER_INFO_TEMPLATE = "count={msg_count} joined={join_time}"
    fake_i18n_module = ModuleType("i18n")
    fake_i18n_module.t = fake_t
    fake_i18n_module.set_locale = lambda locale: None

    class FakeChatMember:
        ADMINISTRATOR = "administrator"
        OWNER = "creator"
        MEMBER = "member"
        LEFT = "left"
        BANNED = "kicked"

    class FakeInlineKeyboardButton:
        def __init__(self, text, url=None, callback_data=None):
            self.text = text
            self.url = url
            self.callback_data = callback_data

    class FakeInlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    class FakeChatPermissions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_telegram_module = ModuleType("telegram")
    fake_telegram_module.Update = object
    fake_telegram_module.ChatMember = FakeChatMember
    fake_telegram_module.InlineKeyboardButton = FakeInlineKeyboardButton
    fake_telegram_module.InlineKeyboardMarkup = FakeInlineKeyboardMarkup
    fake_telegram_module.ChatPermissions = FakeChatPermissions

    class FakeApplication:
        @staticmethod
        def builder():
            return SimpleNamespace(token=lambda value: SimpleNamespace())

    class FakeChatMemberHandler:
        MY_CHAT_MEMBER = "my_chat_member"
        CHAT_MEMBER = "chat_member"

    fake_ext_module = ModuleType("telegram.ext")
    fake_ext_module.Application = FakeApplication
    fake_ext_module.CommandHandler = object
    fake_ext_module.MessageHandler = object
    fake_ext_module.ChatMemberHandler = FakeChatMemberHandler
    fake_ext_module.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    fake_ext_module.filters = SimpleNamespace(TEXT=1, COMMAND=2, PHOTO=3, Sticker=SimpleNamespace(ALL=4))
    fake_ext_module.CallbackQueryHandler = object

    monkeypatch.setitem(sys.modules, "config", fake_config_module)
    monkeypatch.setitem(sys.modules, "ai", fake_ai_module)
    monkeypatch.setitem(sys.modules, "ai.prompts", fake_prompts_module)
    monkeypatch.setitem(sys.modules, "i18n", fake_i18n_module)
    monkeypatch.setitem(sys.modules, "telegram", fake_telegram_module)
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext_module)
    sys.modules.pop("bot", None)
    module = importlib.import_module("bot")
    monkeypatch.setattr(module, "t", fake_t)
    return module


async def _return_true(*args, **kwargs):
    return True


async def _return_false(*args, **kwargs):
    return False


def test_is_chat_admin_uses_cache_to_avoid_repeated_api_calls(bot_module, monkeypatch):
    """第二次调用同样的 (chat_id, user_id) 不应再触发 get_chat_member。"""
    bot_module._admin_cache.clear()
    fake_bot = SimpleNamespace()
    call_count = []

    async def fake_get_chat_member(chat_id, user_id):
        call_count.append((chat_id, user_id))
        return SimpleNamespace(status=bot_module.ChatMember.ADMINISTRATOR)

    fake_bot.get_chat_member = fake_get_chat_member
    context = SimpleNamespace(bot=fake_bot)

    first = asyncio.run(bot_module.is_chat_admin(100, 42, context))
    second = asyncio.run(bot_module.is_chat_admin(100, 42, context))

    assert first is True
    assert second is True
    assert len(call_count) == 1


def test_is_chat_admin_cache_treats_different_users_independently(bot_module, monkeypatch):
    bot_module._admin_cache.clear()
    fake_bot = SimpleNamespace()
    call_count = []

    async def fake_get_chat_member(chat_id, user_id):
        call_count.append((chat_id, user_id))
        if user_id == 1:
            return SimpleNamespace(status=bot_module.ChatMember.OWNER)
        return SimpleNamespace(status=bot_module.ChatMember.MEMBER)

    fake_bot.get_chat_member = fake_get_chat_member
    context = SimpleNamespace(bot=fake_bot)

    r1 = asyncio.run(bot_module.is_chat_admin(100, 1, context))
    r2 = asyncio.run(bot_module.is_chat_admin(100, 2, context))

    assert r1 is True
    assert r2 is False
    assert len(call_count) == 2


def test_cmd_stats_requires_owner(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_owner", lambda user_id: False)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=99),
        message=message,
    )

    asyncio.run(bot_module.cmd_stats(update, SimpleNamespace()))

    assert message.replies == [("owner_only", {})]


def test_cmd_stats_renders_panel_for_owner(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_owner", lambda user_id: True)
    monkeypatch.setattr(bot_module, "stats", SimpleNamespace(get_stats=lambda: {"checks_total": 5}))
    monkeypatch.setattr(bot_module, "render_stats_panel", lambda snapshot, translator: f"stats:{snapshot['checks_total']}")
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
    )

    asyncio.run(bot_module.cmd_stats(update, SimpleNamespace()))

    assert message.replies == [("stats:5", {})]


def test_cmd_add_ad_success_calls_list_refresh(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_owner", lambda user_id: True)
    fake_db = SimpleNamespace(add_advertisement=lambda ad: 7)
    monkeypatch.setattr(bot_module, "db", fake_db)
    refreshed = []

    async def fake_cmd_all_ad(update, context):
        refreshed.append((update, context))

    monkeypatch.setattr(bot_module, "cmd_all_ad", fake_cmd_all_ad)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
    )
    context = SimpleNamespace(args=["Title|https://t.me/test|2099-01-01", "00:00:00|100"])

    asyncio.run(bot_module.cmd_add_ad(update, context))

    assert message.replies == [("ad_add_success|id=7", {})]
    assert len(refreshed) == 1


def test_cmd_del_ad_invalid_id_reports_error(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_owner", lambda user_id: True)
    monkeypatch.setattr(bot_module, "db", SimpleNamespace(delete_advertisement=lambda ad_id: None))
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
    )
    context = SimpleNamespace(args=["abc"])

    asyncio.run(bot_module.cmd_del_ad(update, context))

    assert message.replies == [("ad_delete_failed|error=invalid_id", {})]


def test_cmd_unban_requires_group_chat(bot_module):
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        effective_chat=SimpleNamespace(id=100, type="private"),
        message=message,
    )

    asyncio.run(bot_module.cmd_unban(update, SimpleNamespace(args=[])))

    assert message.replies == [("group_only", {})]


def test_cmd_unban_requires_admin(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        effective_chat=SimpleNamespace(id=100, type="supergroup"),
        message=message,
    )

    asyncio.run(bot_module.cmd_unban(update, SimpleNamespace(args=["123"])))

    assert message.replies == [("admin_only", {})]


def test_cmd_unban_unbans_reply_target(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_true)
    fake_bot = FakeBot()
    reply_to_message = SimpleNamespace(from_user=SimpleNamespace(id=123, first_name="Alice"))
    message = FakeMessage(reply_to_message=reply_to_message)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        effective_chat=SimpleNamespace(id=100, type="supergroup"),
        message=message,
    )
    context = SimpleNamespace(args=[], bot=fake_bot)

    asyncio.run(bot_module.cmd_unban(update, context))

    assert len(fake_bot.restrict_calls) == 1
    assert fake_bot.restrict_calls[0]["user_id"] == 123
    assert message.replies == [("unban_success_detail|name=Alice,user_id=123", {})]


def test_handle_unban_button_requires_admin(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    query_message = FakeMessage(chat_id=100)
    query = FakeQuery(
        data="unban_123",
        from_user=SimpleNamespace(id=2, first_name="Boss"),
        message=query_message,
    )
    update = SimpleNamespace(callback_query=query)

    asyncio.run(bot_module.handle_unban_button(update, SimpleNamespace(bot=FakeBot())))

    assert query.answers == [("admin_only", True)]


def test_handle_unban_button_unbans_target_and_notifies(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_true)
    fake_bot = FakeBot()
    query_message = FakeMessage(chat_id=100)
    query = FakeQuery(
        data="unban_123",
        from_user=SimpleNamespace(id=2, first_name="Boss"),
        message=query_message,
    )
    update = SimpleNamespace(callback_query=query)

    asyncio.run(bot_module.handle_unban_button(update, SimpleNamespace(bot=fake_bot)))

    assert len(fake_bot.restrict_calls) == 1
    assert fake_bot.restrict_calls[0]["user_id"] == 123
    assert query_message.deleted is True
    assert fake_bot.send_calls == [
        (100, "unban_notice|admin=Boss,user_id=123", {"parse_mode": "MarkdownV2"})
    ]
    assert query.answers == [("unban_success", False)]


def test_handle_text_skips_admin_messages(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_true)
    fake_db = FakeDB()
    monkeypatch.setattr(bot_module, "db", fake_db)
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=9)
    message.text = "hello"
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_text(update, SimpleNamespace(bot=FakeBot())))

    assert fake_db.incremented_messages == []


def test_handle_text_creates_user_and_bans_spam(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    monkeypatch.setattr(bot_module, "need_check", lambda user: True)
    monkeypatch.setattr(bot_module, "extract_message_text", lambda message: "visible spam")
    fake_db = FakeDB()
    monkeypatch.setattr(bot_module, "db", fake_db)
    fake_ai = SimpleNamespace(check_text=lambda user_info, text: _async_result(SimpleNamespace(is_spam=True, score=95)))
    monkeypatch.setattr(bot_module, "ai_client", fake_ai)
    stats_calls = []
    monkeypatch.setattr(bot_module, "stats", SimpleNamespace(record_check=lambda outcome: stats_calls.append(outcome)))
    banned = []

    async def fake_ban(context, chat_id, user, message, result):
        banned.append((chat_id, user.id, result.score))

    monkeypatch.setattr(bot_module, "ban_user_and_notify", fake_ban)
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=10)
    message.text = "spam"
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_text(update, SimpleNamespace(bot=FakeBot())))

    assert len(fake_db.saved_users) == 1
    assert fake_db.incremented_messages == [(10, 100)]
    assert banned == [(100, 10, 95)]
    assert stats_calls == ["banned"]


def test_handle_text_skips_ai_when_need_check_is_false(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    monkeypatch.setattr(bot_module, "need_check", lambda user: False)
    fake_db = FakeDB(user=SimpleNamespace(user_id=10, chat_id=100, join_time=datetime(2026, 1, 1), message_count=0, verification_times=0))
    monkeypatch.setattr(bot_module, "db", fake_db)
    called = []
    monkeypatch.setattr(bot_module, "ai_client", SimpleNamespace(check_text=lambda *args, **kwargs: called.append(True)))
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=10)
    message.text = "hello"
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_text(update, SimpleNamespace(bot=FakeBot())))

    assert fake_db.incremented_messages == [(10, 100)]
    assert called == []


def test_handle_text_marks_passed_and_increments_verification(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    monkeypatch.setattr(bot_module, "need_check", lambda user: True)
    monkeypatch.setattr(bot_module, "extract_message_text", lambda message: "clean text")
    fake_db = FakeDB(user=SimpleNamespace(user_id=11, chat_id=100, join_time=datetime(2026, 1, 1), message_count=1, verification_times=0))
    monkeypatch.setattr(bot_module, "db", fake_db)
    monkeypatch.setattr(
        bot_module,
        "ai_client",
        SimpleNamespace(check_text=lambda user_info, text: _async_result(SimpleNamespace(is_spam=False, score=20))),
    )
    stats_calls = []
    monkeypatch.setattr(bot_module, "stats", SimpleNamespace(record_check=lambda outcome: stats_calls.append(outcome)))
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=11)
    message.text = "hello"
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_text(update, SimpleNamespace(bot=FakeBot())))

    assert fake_db.incremented_verifications == [(11, 100)]
    assert stats_calls == ["passed"]


def test_handle_text_records_failed_when_ai_errors(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    monkeypatch.setattr(bot_module, "need_check", lambda user: True)
    fake_db = FakeDB(user=SimpleNamespace(user_id=12, chat_id=100, join_time=datetime(2026, 1, 1), message_count=1, verification_times=0))
    monkeypatch.setattr(bot_module, "db", fake_db)

    async def broken_check_text(user_info, text):
        raise RuntimeError("boom")

    monkeypatch.setattr(bot_module, "ai_client", SimpleNamespace(check_text=broken_check_text))
    stats_calls = []
    monkeypatch.setattr(bot_module, "stats", SimpleNamespace(record_check=lambda outcome: stats_calls.append(outcome)))
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=12)
    message.text = "hello"
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_text(update, SimpleNamespace(bot=FakeBot())))

    assert stats_calls == ["failed"]
    assert fake_db.incremented_verifications == []


def test_handle_photo_downloads_image_and_marks_passed(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    monkeypatch.setattr(bot_module, "need_check", lambda user: True)
    monkeypatch.setattr(bot_module, "extract_message_text", lambda message: "caption text")
    fake_db = FakeDB(user=SimpleNamespace(user_id=20, chat_id=100, join_time=datetime(2026, 1, 1), message_count=1, verification_times=0))
    monkeypatch.setattr(bot_module, "db", fake_db)
    stats_calls = []
    monkeypatch.setattr(bot_module, "stats", SimpleNamespace(record_check=lambda outcome: stats_calls.append(outcome)))
    captured = {}

    async def fake_evaluate(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(should_ban=False, result=SimpleNamespace(score=0))

    monkeypatch.setattr(bot_module, "evaluate_photo_moderation", fake_evaluate)
    fake_bot = FakeBot()
    fake_bot.files["photo-1"] = FakeDownloadedFile(b"img")
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=20)
    message.photo = [SimpleNamespace(file_id="photo-1")]
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_photo(update, SimpleNamespace(bot=fake_bot)))

    assert captured["message_text"] == "caption text"
    assert captured["image_base64"].startswith("data:image/jpeg;base64,")
    assert fake_db.incremented_verifications == [(20, 100)]
    assert stats_calls == ["passed"]


def test_handle_photo_bans_when_decision_is_spam(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    monkeypatch.setattr(bot_module, "need_check", lambda user: True)
    fake_db = FakeDB(user=SimpleNamespace(user_id=21, chat_id=100, join_time=datetime(2026, 1, 1), message_count=1, verification_times=0))
    monkeypatch.setattr(bot_module, "db", fake_db)
    stats_calls = []
    monkeypatch.setattr(bot_module, "stats", SimpleNamespace(record_check=lambda outcome: stats_calls.append(outcome)))
    monkeypatch.setattr(
        bot_module,
        "evaluate_photo_moderation",
        lambda **kwargs: _async_result(SimpleNamespace(should_ban=True, result=SimpleNamespace(score=99))),
    )
    banned = []

    async def fake_ban(context, chat_id, user, message, result):
        banned.append((chat_id, user.id, result.score))

    monkeypatch.setattr(bot_module, "ban_user_and_notify", fake_ban)
    fake_bot = FakeBot()
    fake_bot.files["photo-2"] = FakeDownloadedFile(b"img")
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=21)
    message.photo = [SimpleNamespace(file_id="photo-2")]
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_photo(update, SimpleNamespace(bot=fake_bot)))

    assert banned == [(100, 21, 99)]
    assert stats_calls == ["banned"]


def test_handle_photo_records_failed_when_download_errors(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    monkeypatch.setattr(bot_module, "need_check", lambda user: True)
    fake_db = FakeDB(user=SimpleNamespace(user_id=22, chat_id=100, join_time=datetime(2026, 1, 1), message_count=1, verification_times=0))
    monkeypatch.setattr(bot_module, "db", fake_db)
    stats_calls = []
    monkeypatch.setattr(bot_module, "stats", SimpleNamespace(record_check=lambda outcome: stats_calls.append(outcome)))
    fake_bot = FakeBot()
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=22)
    message.photo = [SimpleNamespace(file_id="missing")]
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_photo(update, SimpleNamespace(bot=fake_bot)))

    assert stats_calls == ["failed"]


def test_handle_sticker_marks_passed(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    monkeypatch.setattr(bot_module, "need_check", lambda user: True)
    fake_db = FakeDB(user=SimpleNamespace(user_id=30, chat_id=100, join_time=datetime(2026, 1, 1), message_count=1, verification_times=0))
    monkeypatch.setattr(bot_module, "db", fake_db)
    monkeypatch.setattr(
        bot_module,
        "ai_client",
        SimpleNamespace(check_image=lambda user_info, image: _async_result(SimpleNamespace(is_spam=False, score=20))),
    )
    stats_calls = []
    monkeypatch.setattr(bot_module, "stats", SimpleNamespace(record_check=lambda outcome: stats_calls.append(outcome)))
    fake_bot = FakeBot()
    fake_bot.files["sticker-1"] = FakeDownloadedFile(b"webp")
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=30)
    message.sticker = SimpleNamespace(file_id="sticker-1")
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_sticker(update, SimpleNamespace(bot=fake_bot)))

    assert fake_db.incremented_verifications == [(30, 100)]
    assert stats_calls == ["passed"]


def test_handle_sticker_bans_spam(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    monkeypatch.setattr(bot_module, "need_check", lambda user: True)
    fake_db = FakeDB(user=SimpleNamespace(user_id=31, chat_id=100, join_time=datetime(2026, 1, 1), message_count=1, verification_times=0))
    monkeypatch.setattr(bot_module, "db", fake_db)
    monkeypatch.setattr(
        bot_module,
        "ai_client",
        SimpleNamespace(check_image=lambda user_info, image: _async_result(SimpleNamespace(is_spam=True, score=96))),
    )
    stats_calls = []
    monkeypatch.setattr(bot_module, "stats", SimpleNamespace(record_check=lambda outcome: stats_calls.append(outcome)))
    banned = []

    async def fake_ban(context, chat_id, user, message, result):
        banned.append((chat_id, user.id, result.score))

    monkeypatch.setattr(bot_module, "ban_user_and_notify", fake_ban)
    fake_bot = FakeBot()
    fake_bot.files["sticker-2"] = FakeDownloadedFile(b"webp")
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=31)
    message.sticker = SimpleNamespace(file_id="sticker-2")
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_sticker(update, SimpleNamespace(bot=fake_bot)))

    assert banned == [(100, 31, 96)]
    assert stats_calls == ["banned"]


def test_handle_sticker_logs_error_when_detection_fails(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_false)
    monkeypatch.setattr(bot_module, "need_check", lambda user: True)
    fake_db = FakeDB(user=SimpleNamespace(user_id=32, chat_id=100, join_time=datetime(2026, 1, 1), message_count=1, verification_times=0))
    monkeypatch.setattr(bot_module, "db", fake_db)
    stats_calls = []
    fake_logger = FakeLogger()
    monkeypatch.setattr(bot_module, "logger", fake_logger)
    monkeypatch.setattr(bot_module, "stats", SimpleNamespace(record_check=lambda outcome: stats_calls.append(outcome)))
    fake_bot = FakeBot()
    message = FakeMessage(chat_id=100)
    message.from_user = SimpleNamespace(id=32)
    message.sticker = SimpleNamespace(file_id="missing")
    update = SimpleNamespace(message=message)

    asyncio.run(bot_module.handle_sticker(update, SimpleNamespace(bot=fake_bot)))

    assert stats_calls == ["failed"]
    assert fake_logger.errors


def test_handle_bot_added_to_group_sends_welcome_and_schedules_cleanup(bot_module, monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        bot_module,
        "schedule_message_deletion",
        lambda context, chat_id, message_id, delay_seconds, reason: scheduled.append((chat_id, message_id, delay_seconds, reason)),
    )
    fake_bot = FakeBot()
    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(
            old_chat_member=SimpleNamespace(status=bot_module.ChatMember.LEFT),
            new_chat_member=SimpleNamespace(status=bot_module.ChatMember.MEMBER),
            chat=SimpleNamespace(id=555, title="Group A"),
        )
    )

    asyncio.run(bot_module.handle_bot_added_to_group(update, SimpleNamespace(bot=fake_bot)))

    assert len(fake_bot.send_calls) == 1
    assert fake_bot.send_calls[0][0] == 555
    assert "welcome_message" in fake_bot.send_calls[0][1]
    assert scheduled == [(555, 901, 30, "welcome message")]


def test_handle_bot_added_to_group_logs_welcome_send_failure(bot_module, monkeypatch):
    fake_logger = FakeLogger()
    monkeypatch.setattr(bot_module, "logger", fake_logger)

    class FailingBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("send failed")

    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(
            old_chat_member=SimpleNamespace(status=bot_module.ChatMember.LEFT),
            new_chat_member=SimpleNamespace(status=bot_module.ChatMember.MEMBER),
            chat=SimpleNamespace(id=556, title="Group B"),
        )
    )

    asyncio.run(bot_module.handle_bot_added_to_group(update, SimpleNamespace(bot=FailingBot())))

    assert fake_logger.errors


def test_handle_bot_added_to_group_logs_admin_promotion(bot_module, monkeypatch):
    fake_logger = FakeLogger()
    monkeypatch.setattr(bot_module, "logger", fake_logger)
    fake_bot = FakeBot()
    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(
            old_chat_member=SimpleNamespace(status=bot_module.ChatMember.MEMBER),
            new_chat_member=SimpleNamespace(status=bot_module.ChatMember.ADMINISTRATOR),
            chat=SimpleNamespace(id=557, title="Group C"),
        )
    )

    asyncio.run(bot_module.handle_bot_added_to_group(update, SimpleNamespace(bot=fake_bot)))

    assert len(fake_bot.send_calls) == 1
    assert "admin_promoted" in fake_bot.send_calls[0][1]
    assert fake_logger.infos


def test_handle_bot_added_to_group_logs_admin_promotion_failure(bot_module, monkeypatch):
    fake_logger = FakeLogger()
    monkeypatch.setattr(bot_module, "logger", fake_logger)

    class FailingBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("send failed")

    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(
            old_chat_member=SimpleNamespace(status=bot_module.ChatMember.MEMBER),
            new_chat_member=SimpleNamespace(status=bot_module.ChatMember.ADMINISTRATOR),
            chat=SimpleNamespace(id=558, title="Group D"),
        )
    )

    asyncio.run(bot_module.handle_bot_added_to_group(update, SimpleNamespace(bot=FailingBot())))

    assert fake_logger.errors


def test_handle_unban_button_logs_warning_when_notice_delete_fails(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "is_chat_admin", _return_true)
    fake_logger = FakeLogger()
    monkeypatch.setattr(bot_module, "logger", fake_logger)
    fake_bot = FakeBot()
    query_message = FakeMessage(chat_id=100)

    async def broken_delete():
        raise RuntimeError("cannot delete")

    query_message.delete = broken_delete
    query = FakeQuery(
        data="unban_123",
        from_user=SimpleNamespace(id=2, first_name="Boss"),
        message=query_message,
    )
    update = SimpleNamespace(callback_query=query)

    asyncio.run(bot_module.handle_unban_button(update, SimpleNamespace(bot=fake_bot)))

    assert fake_logger.warnings
    assert fake_bot.send_calls == [
        (100, "unban_notice|admin=Boss,user_id=123", {"parse_mode": "MarkdownV2"})
    ]


async def _async_result(value):
    return value
