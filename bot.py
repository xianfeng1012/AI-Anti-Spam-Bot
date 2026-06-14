"""
AI 反垃圾广告机器人 (AI Anti-Spam Bot)
"""
import asyncio
import base64
import logging
import sys
import os
import time
from datetime import datetime
from telegram import Update, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ChatMemberHandler, ContextTypes, filters, CallbackQueryHandler
)
from config import config
from database import db, UserInfo
from ai import create_ai_client
from ai.prompts import USER_INFO_TEMPLATE
from developer_info import get_start_message
from i18n import t, set_locale
from message_utils import (
    build_ban_notice,
    escape_markdown_v2,
    extract_message_text,
    process_nickname,
)
from handler_logic import evaluate_photo_moderation
from moderation_logic import RuntimeStats, should_check_user
from command_logic import (
    CommandInputError,
    parse_add_ad_payload,
    parse_delete_ad_args,
    parse_unban_callback_data,
    resolve_unban_target,
)
from command_views import render_ad_list, render_stats_panel

# 确保日志目录存在
os.makedirs('data', exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('data/bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

stats = RuntimeStats()

# 群管理员检查结果缓存：避免每个消息都调 getChatMember 触发 Telegram 限流
_ADMIN_CACHE_TTL = 300  # 5 分钟
# 缓存条目上限：超出时清空重来，防止长跑 bot 内存无限增长
_ADMIN_CACHE_MAX_SIZE = 10000
_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}

ai_client = create_ai_client()

# ============ 工具函数 ============

def is_owner(user_id: int) -> bool:
    """检查是否为超级管理员（可管理广告）"""
    owners = config.get("telegram.owners", [])
    return str(user_id) in owners

def is_group_allowed(chat_id: int) -> bool:
    """检查群组是否在白名单内。allow_any_group=true 时放行所有群；否则只放行 groups 列表内的群。"""
    if config.get("telegram.allow_any_group", True):
        return True
    allowed = config.get("telegram.groups", []) or []
    return str(chat_id) in {str(g) for g in allowed}

async def is_chat_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """检查是否为群管理员，结果按 (chat_id, user_id) 缓存 5 分钟，避免高频调用 Telegram API 触发限流。"""
    if len(_admin_cache) >= _ADMIN_CACHE_MAX_SIZE:
        _admin_cache.clear()
    cache_key = (chat_id, user_id)
    now = time.monotonic()
    cached = _admin_cache.get(cache_key)
    if cached is not None and now - cached[0] < _ADMIN_CACHE_TTL:
        return cached[1]

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        is_admin = member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except Exception as e:
        # 网络抖动等瞬时错误不写缓存，否则会把真管理员误判成普通用户并缓存 5 分钟
        logger.debug(f"get_chat_member failed for {user_id} in {chat_id}: {e}")
        return False

    _admin_cache[cache_key] = (now, is_admin)
    return is_admin

def need_check(user: UserInfo) -> bool:
    """
    判断用户是否需要检测
    支持灵活的检测策略配置
    """
    strategy = {
        "verification_times": config.get("strategy.verification_times", 0),
        "joined_days": config.get("strategy.joined_days", 3),
        "check_message_count": config.get("strategy.check_message_count", True),
        "min_messages": config.get("strategy.min_messages", 3),
    }
    return should_check_user(user, strategy)

def build_user_info(user, db_user: UserInfo) -> str:
    """构建用户信息字符串（不包含用户名称，避免因名称误判）。
    调用方已将 db_user.message_count 自增为含当前消息的值，故此处直接使用。"""
    return USER_INFO_TEMPLATE.format(
        msg_count=db_user.message_count,
        join_time=db_user.join_time.strftime("%Y-%m-%d %H:%M")
    )

def schedule_message_deletion(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    delay_seconds: int,
    reason: str = "message"
):
    """后台延迟删除消息，避免群内长期堆积系统提示"""
    if delay_seconds <= 0:
        return

    async def delete_after_delay():
        await asyncio.sleep(delay_seconds)
        try:
            await context.bot.delete_message(chat_id, message_id)
            logger.info(f"Deleted {reason} in group {chat_id} after {delay_seconds}s")
        except Exception as e:
            logger.warning(f"Failed to delete {reason} in {chat_id}: {e}")

    asyncio.create_task(delete_after_delay())

def create_ban_keyboard() -> InlineKeyboardMarkup:
    """
    创建封禁通知的按钮
    包含：解封按钮 + 广告按钮
    """
    buttons = []
    
    # 第一行：解封按钮（占位，实际在 send_ban_notice 中动态生成）
    # buttons.append([InlineKeyboardButton("🔓 解除禁言", callback_data=f"unban_{user_id}")])
    
    # 后续行：广告按钮
    ads = db.get_valid_advertisements()
    for ad in ads:
        buttons.append([InlineKeyboardButton(ad.title, url=ad.url)])
    
    return InlineKeyboardMarkup(buttons) if buttons else None

async def send_ban_notice(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, result):
    """发送禁言通知（带解封按钮 + 广告按钮）"""
    name = f"{user.last_name or ''}{user.first_name or ''}"
    masked_name = process_nickname(name)
    user_link = f"tg://user?id={user.id}"

    notice = build_ban_notice(
        config.get("message.ban_notice_template"),
        masked_name=masked_name,
        user_link=user_link,
        score=result.score,
        reason=result.reason,
        mock_text=result.mock_text,
        user_id=user.id,
        chat_id=chat_id,
        logger=logger,
    )

    # 创建按钮：解封 + 自定义广告
    buttons = []
    buttons.append([InlineKeyboardButton(t('btn_unban'), callback_data=f"unban_{user.id}")])

    ads = db.get_valid_advertisements()
    for ad in ads:
        buttons.append([InlineKeyboardButton(ad.title, url=ad.url)])
    
    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    
    sent_message = await context.bot.send_message(
        chat_id,
        notice,
        parse_mode="MarkdownV2",
        reply_markup=reply_markup
    )

    delete_after = config.get("message.delete_ban_notice_after_seconds", 30)
    schedule_message_deletion(context, chat_id, sent_message.message_id, delete_after, "ban notice")

async def ban_user_and_notify(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, message, result):
    """禁言用户并发送通知"""
    from telegram import ChatPermissions
    
    await message.delete()
    
    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user.id,
        permissions=ChatPermissions(
            can_send_messages=False,
            can_send_audios=False,
            can_send_documents=False,
            can_send_photos=False,
            can_send_videos=False,
            can_send_video_notes=False,
            can_send_voice_notes=False,
            can_send_polls=False,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False
        )
    )
    
    await send_ban_notice(context, chat_id, user, result)
    logger.info(f"Banned user {user.id} in chat {chat_id}, score: {result.score}")


# ============ 消息处理 ============

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息"""
    message = update.message
    user = message.from_user
    chat_id = message.chat_id

    if not is_group_allowed(chat_id):
        return

    if await is_chat_admin(chat_id, user.id, context):
        return

    db_user = db.get_user(user.id, chat_id)
    if not db_user:
        db_user = UserInfo(
            user_id=user.id,
            chat_id=chat_id,
            join_time=datetime.now(),
            message_count=0,
            check_count=0,
            verification_times=0
        )
        db.save_user(db_user)
    
    db.increment_message_count(user.id, chat_id)
    db_user.message_count += 1  # 让 need_check / build_user_info 看到含当前消息的计数

    if not need_check(db_user):
        return

    try:
        user_info = build_user_info(user, db_user)
        message_text = extract_message_text(message) or message.text
        result = await ai_client.check_text(user_info, message_text)
        
        score_threshold = config.get("strategy.spam_score", 80)
        
        if result.is_spam and result.score >= score_threshold:
            # 是垃圾广告，封禁
            await ban_user_and_notify(context, chat_id, user, message, result)
            stats.record_check('banned')
        else:
            # 不是垃圾广告，增加验证通过次数
            db.increment_verification_times(user.id, chat_id)
            stats.record_check('passed')
            logger.info(f"User {user.id} passed check, verification_times increased")
    except Exception as e:
        logger.error(f"❌ AI 检测失败 (用户 {user.id}): {e}", exc_info=True)
        stats.record_check('failed')
        # 失败时不封禁，避免误伤

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理图片消息"""
    message = update.message
    user = message.from_user
    chat_id = message.chat_id

    if not is_group_allowed(chat_id):
        return

    if await is_chat_admin(chat_id, user.id, context):
        return

    db_user = db.get_user(user.id, chat_id)
    if not db_user:
        db_user = UserInfo(
            user_id=user.id,
            chat_id=chat_id,
            join_time=datetime.now(),
            message_count=0,
            check_count=0,
            verification_times=0
        )
        db.save_user(db_user)

    db.increment_message_count(user.id, chat_id)
    db_user.message_count += 1  # 让 need_check / build_user_info 看到含当前消息的计数

    if not need_check(db_user):
        return

    try:
        photo = message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        image_base64 = f"data:image/jpeg;base64,{base64.b64encode(image_bytes).decode()}"

        user_info = build_user_info(user, db_user)
        score_threshold = config.get("strategy.spam_score", 80)
        message_text = extract_message_text(message)
        decision = await evaluate_photo_moderation(
            ai_client=ai_client,
            user_info=user_info,
            image_base64=image_base64,
            score_threshold=score_threshold,
            message_text=message_text,
            logger=logger,
            user_id=user.id,
        )

        if decision.should_ban:
            await ban_user_and_notify(context, chat_id, user, message, decision.result)
            stats.record_check('banned')
        else:
            db.increment_verification_times(user.id, chat_id)
            stats.record_check('passed')
    except Exception as e:
        logger.error(f"❌ 图片检测失败 (用户 {user.id}): {e}", exc_info=True)
        stats.record_check('failed')
        # 失败时不封禁，避免误伤

async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理贴纸消息"""
    message = update.message
    user = message.from_user
    chat_id = message.chat_id

    if not is_group_allowed(chat_id):
        return

    if await is_chat_admin(chat_id, user.id, context):
        return

    db_user = db.get_user(user.id, chat_id)
    if not db_user:
        db_user = UserInfo(
            user_id=user.id,
            chat_id=chat_id,
            join_time=datetime.now(),
            message_count=0,
            check_count=0,
            verification_times=0
        )
        db.save_user(db_user)
    
    db.increment_message_count(user.id, chat_id)
    db_user.message_count += 1  # 让 need_check / build_user_info 看到含当前消息的计数

    if not need_check(db_user):
        return

    try:
        file = await context.bot.get_file(message.sticker.file_id)
        image_bytes = await file.download_as_bytearray()
        image_base64 = f"data:image/webp;base64,{base64.b64encode(image_bytes).decode()}"

        user_info = build_user_info(user, db_user)
        result = await ai_client.check_image(user_info, image_base64)
        
        score_threshold = config.get("strategy.spam_score", 80)
        if result.is_spam and result.score >= score_threshold:
            await ban_user_and_notify(context, chat_id, user, message, result)
            stats.record_check('banned')
        else:
            db.increment_verification_times(user.id, chat_id)
            stats.record_check('passed')
    except Exception as e:
        logger.error(f"❌ 贴纸检测失败 (用户 {user.id}): {e}", exc_info=True)
        stats.record_check('failed')
        # 失败时不封禁，避免误伤


# ============ 成员变动 ============

async def handle_bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 Bot 被添加到群组"""
    chat_member = update.my_chat_member
    new_status = chat_member.new_chat_member.status
    old_status = chat_member.old_chat_member.status
    chat = chat_member.chat
    
    # Bot 被添加到群组（从非成员变为成员）
    if old_status in [ChatMember.LEFT, ChatMember.BANNED] and new_status == ChatMember.MEMBER:
        try:
            sent_message = await context.bot.send_message(chat.id, t('welcome_message'))
            delete_after = config.get("message.delete_welcome_message_after_seconds", 30)
            schedule_message_deletion(context, chat.id, sent_message.message_id, delete_after, "welcome message")
        except Exception as e:
            logger.error(f"Failed to send welcome message to {chat.id}: {e}")

    # Bot 被提升为管理员
    elif old_status == ChatMember.MEMBER and new_status == ChatMember.ADMINISTRATOR:
        try:
            await context.bot.send_message(chat.id, t('admin_promoted'))
            logger.info(f"Bot promoted to admin in group {chat.id} ({chat.title})")
        except Exception as e:
            logger.error(f"Failed to send admin promotion message to {chat.id}: {e}")

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理新成员加入"""
    chat_member = update.chat_member
    if chat_member.new_chat_member.status == ChatMember.MEMBER:
        user = chat_member.new_chat_member.user
        chat_id = chat_member.chat.id

        if not is_group_allowed(chat_id):
            return

        db_user = UserInfo(
            user_id=user.id,
            chat_id=chat_id,
            join_time=datetime.now(),
            message_count=0,
            check_count=0,
            verification_times=0
        )
        db.save_user(db_user)
        logger.info(f"New member {user.id} joined chat {chat_id}")

# ============ 广告管理命令 ============

async def cmd_add_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    添加广告按钮
    格式: /add_ad 标题|链接|过期时间|权重
    例如: /add_ad 官方频道|https://t.me/channel|2099-01-01 00:00:00|100
    """
    user_id = update.effective_user.id
    
    if not is_owner(user_id):
        await update.message.reply_text(t('owner_only'))
        return
    
    if not context.args:
        await update.message.reply_text(t('ad_add_usage'))
        return
    
    try:
        payload = " ".join(context.args)
        ad = parse_add_ad_payload(payload)
        ad_id = db.add_advertisement(ad)
        await update.message.reply_text(t('ad_add_success', id=ad_id))
        
        # 显示所有广告
        await cmd_all_ad(update, context)
    except CommandInputError as e:
        if e.code == "format":
            await update.message.reply_text(t('ad_add_error_format'))
            return
        await update.message.reply_text(t('ad_add_failed', error=str(e)))
    except Exception as e:
        await update.message.reply_text(t('ad_add_failed', error=str(e)))

async def cmd_all_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看所有广告"""
    user_id = update.effective_user.id
    
    if not is_owner(user_id):
        await update.message.reply_text(t('owner_only'))
        return
    
    ads = db.get_all_advertisements()
    await update.message.reply_text(render_ad_list(ads, t))

async def cmd_del_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    删除广告
    格式: /del_ad <ID>
    """
    user_id = update.effective_user.id
    
    if not is_owner(user_id):
        await update.message.reply_text(t('owner_only'))
        return
    
    if not context.args or len(context.args) != 1:
        await update.message.reply_text(t('ad_delete_usage'))
        return
    
    try:
        ad_id = parse_delete_ad_args(context.args)
        db.delete_advertisement(ad_id)
        await update.message.reply_text(t('ad_delete_success', id=ad_id))
        await cmd_all_ad(update, context)
    except CommandInputError as e:
        if e.code == "usage":
            await update.message.reply_text(t('ad_delete_usage'))
            return
        await update.message.reply_text(t('ad_delete_failed', error=str(e)))
    except Exception as e:
        await update.message.reply_text(t('ad_delete_failed', error=str(e)))

# ============ 其他管理命令 ============

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    await update.message.reply_text(get_start_message())

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /admin 命令"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if update.effective_chat.type == "private":
        if not is_owner(user_id):
            await update.message.reply_text(t('no_permission'))
            return
    else:
        if not await is_chat_admin(chat_id, user_id, context):
            await update.message.reply_text(t('admin_only'))
            return

    model = config.get("ai_model", "unknown")
    score = config.get("strategy.spam_score", 80)
    
    msg = t('admin_panel',
        model=model,
        score=score,
        days=config.get('strategy.joined_days', 3),
        messages=config.get('strategy.min_messages', 3),
        verification=config.get('strategy.verification_times', 0)
    )
    
    if is_owner(user_id):
        msg += t('admin_panel_owner')
    
    await update.message.reply_text(msg)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看运行统计（仅超级管理员）"""
    user_id = update.effective_user.id
    
    if not is_owner(user_id):
        await update.message.reply_text(t('owner_only'))
        return
    
    await update.message.reply_text(render_stats_panel(stats.get_stats(), t))

async def handle_unban_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理解除禁言按钮点击"""
    from telegram import ChatPermissions
    
    query = update.callback_query

    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # 不在此处提前 answer()：每个 callback query 只能 answer 一次，
    # 提前应答会让后面的「权限不足/成功/失败」提示全部失效。
    if not await is_chat_admin(chat_id, user_id, context):
        await query.answer(t('admin_only'), show_alert=True)
        return

    target_user_id = parse_unban_callback_data(query.data)
    if target_user_id is None:
        await query.answer()
        return
    
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False
            )
        )
        
        # 删除封禁消息（Go 版本的功能）
        try:
            await query.message.delete()
        except Exception as e:
            logger.warning(f"删除封禁消息失败: {e}")
        
        # 发送解禁通知（Go 版本的功能）
        admin_name = query.from_user.first_name or "Admin"
        notice = t('unban_notice', admin=escape_markdown_v2(admin_name), user_id=target_user_id)
        await context.bot.send_message(chat_id, notice, parse_mode="MarkdownV2")
        
        await query.answer(t('unban_success'), show_alert=False)
        
        logger.info(f"Unmuted user {target_user_id} in chat {chat_id} by admin {user_id} via button")
    except Exception as e:
        await query.answer(t('unban_failed', error=str(e)), show_alert=True)
        logger.error(f"Failed to unmute user {target_user_id}: {e}")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /unban 命令"""
    from telegram import ChatPermissions
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if update.effective_chat.type == "private":
        await update.message.reply_text(t('group_only'))
        return
    
    if not await is_chat_admin(chat_id, user_id, context):
        await update.message.reply_text(t('admin_only'))
        return
    
    try:
        target_user_id, target_user_name = resolve_unban_target(
            update.message.reply_to_message,
            context.args or [],
        )
    except CommandInputError as e:
        if e.code == "invalid_id":
            await update.message.reply_text(t('unban_invalid_id'))
            return
        await update.message.reply_text(t('unban_usage'))
        return
    
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False
            )
        )
        
        if target_user_name:
            await update.message.reply_text(t('unban_success_detail', name=target_user_name, user_id=target_user_id))
        else:
            await update.message.reply_text(t('unban_success_id', user_id=target_user_id))
        
        logger.info(f"Unmuted user {target_user_id} in chat {chat_id} by admin {user_id}")
    except Exception as e:
        await update.message.reply_text(t('unban_failed', error=str(e)))
        logger.error(f"Failed to unmute user {target_user_id}: {e}")

# ============ 启动 ============

def validate_config():
    """验证必要的配置项"""
    errors = []
    
    # 检查 Telegram Token
    token = config.get("telegram.token")
    if not token:
        errors.append("❌ telegram.token 未配置")
    
    # 检查 AI 模型配置
    ai_model = config.get("ai_model", "openai")
    if ai_model == "openai":
        if not config.get("openai.api_key"):
            errors.append("❌ openai.api_key 未配置")
    elif ai_model == "qwen":
        if not config.get("qwen.api_key"):
            errors.append("❌ qwen.api_key 未配置")
    elif ai_model == "deepseek":
        if not config.get("deepseek.api_key"):
            errors.append("❌ deepseek.api_key 未配置")
    else:
        errors.append(f"❌ 不支持的 AI 模型: {ai_model}")
    
    # 检查超级管理员
    owners = config.get("telegram.owners", [])
    if not owners:
        logger.warning("⚠️ telegram.owners 未配置，广告管理功能将无法使用")
    
    if errors:
        logger.error("配置验证失败：")
        for error in errors:
            logger.error(error)
        logger.error("\n请检查 config.yml 配置文件")
        sys.exit(1)
    
    logger.info("✅ 配置验证通过")

def main():
    # 初始化语言设置
    language = config.get("language", "zh")
    set_locale(language)
    
    # 验证配置
    validate_config()

    token = config.get("telegram.token")

    # 初始化 AI 客户端
    try:
        global ai_client
        ai_client = create_ai_client()
        logger.info(f"AI client initialized: {config.get('ai_model')}")
    except Exception as e:
        logger.error(f"AI client init failed: {e}")
        sys.exit(1)

    import os
    api_url = os.getenv("TELEGRAM_API_URL")
    
    builder = Application.builder().token(token)
    if api_url:
        builder = builder.base_url(f"{api_url}/bot")
        builder = builder.base_file_url(f"{api_url}/file/bot")
        logger.info(f"Using custom Telegram API: {api_url}")
    
    app = builder.build()

    # 注册处理器
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("stats", cmd_stats))
    
    # 广告管理命令
    app.add_handler(CommandHandler("add_ad", cmd_add_ad))
    app.add_handler(CommandHandler("all_ad", cmd_all_ad))
    app.add_handler(CommandHandler("del_ad", cmd_del_ad))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(ChatMemberHandler(handle_bot_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(handle_unban_button, pattern="^unban_"))

    logger.info("Bot starting...")
    logger.info(f"Strategy: joined<={config.get('strategy.joined_days')}d | messages<={config.get('strategy.min_messages')} | score>={config.get('strategy.spam_score')}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
