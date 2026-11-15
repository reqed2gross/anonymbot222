import asyncio
import html
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, ReplyParameters,
    InputMediaPhoto, InputMediaVideo, InputMediaAnimation,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
)
from aiogram.exceptions import TelegramAPIError
from aiohttp import web

import config
import database as db
from middlewares import check_rate_limit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("anonym-bot")

START_TIME = time.time()

bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# Буферы
_album_buffer: dict[tuple[int, str], list[Message]] = {}
_album_tasks: dict[tuple[int, str], asyncio.Task] = {}
_last_warn: dict[int, float] = {}

# Словарь для режима диалога: {id_админа: id_анонима}
active_chats: dict[int, int] = {}
# Нижнее меню для админа
admin_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="🛑 Выйти из диалога")]
    ],
    resize_keyboard=True
)
def is_admin(message: Message) -> bool:
    return bool(message.from_user) and message.from_user.id == config.ADMIN_ID


async def get_texts():
    return (
        await db.get_setting("welcome_text"),
        await db.get_setting("autoreply_text"),
        await db.get_setting("antispam_text"),
        int(await db.get_setting("antispam_limit")),
        int(await db.get_setting("antispam_window")),
    )


async def send_welcome_if_needed(message: Message):
    user_id = message.from_user.id
    user = await db.get_user_by_tg(user_id)
    if user and user["seen_welcome"]:
        return
    welcome, *_ = await get_texts()
    safe_name = html.escape(message.from_user.first_name or "друг")
    try:
        text = welcome.format(first_name=safe_name)
    except (KeyError, ValueError, IndexError):
        text = welcome.replace("{first_name}", safe_name)
    try:
        await message.answer(text)
    except TelegramAPIError as e:
        log.error(f"welcome send failed: {e}")
        return
    await db.mark_seen_welcome(user_id)


    # Создаем кнопки
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply_{user['id']}"),
            InlineKeyboardButton(text="🚫 Забанить", callback_data=f"block_{user['id']}")
        ]
    ])
    
    # Отправляем с кнопками
    try:
        await bot.send_message(
            admin_id,
            f"<i>Аноним #{user['id']}</i>",
            reply_parameters=ReplyParameters(
                message_id=sent.message_id, allow_sending_without_reply=True
            ),
            reply_markup=kb
        )
    except TelegramAPIError:
        pass

    for msg in messages:
        if msg.photo:
            photos.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=msg.caption))
        elif msg.video:
            videos.append(InputMediaVideo(media=msg.video.file_id, caption=msg.caption))
        elif msg.animation:
            anims.append(InputMediaAnimation(media=msg.animation.file_id, caption=msg.caption))
        else:
            non_group = True

    # Media group можно отправлять только с фото/видео/анимацией одного типа.
    # Если есть документы/аудио/голосовые или смесь — отправляем по одному.
    if non_group or (photos and videos) or (photos and anims) or (videos and anims):
        for msg in messages:
            try:
                sent = await bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=msg.chat.id,
                    message_id=msg.message_id,
                )
                await db.save_message_map(admin_id, sent.message_id, user_id)
            except TelegramAPIError as e:
                log.error(f"album fallback copy failed: {e}")
    else:
        items = photos or videos or anims
        if items:
            try:
                sent_list = await bot.send_media_group(admin_id, items)
                for s in sent_list:
                    await db.save_message_map(admin_id, s.message_id, user_id)
            except TelegramAPIError as e:
                log.error(f"send_media_group failed: {e}, fallback to individual")
                for msg in messages:
                    try:
                        sent = await bot.copy_message(
                            chat_id=admin_id,
                            from_chat_id=msg.chat.id,
                            message_id=msg.message_id,
                        )
                        await db.save_message_map(admin_id, sent.message_id, user_id)
                    except TelegramAPIError as e2:
                        log.error(f"fallback copy failed: {e2}")

    user = await db.get_or_create_user(user_id)
    try:
        await bot.send_message(
            admin_id,
            f"<i>Альбом от Анонима #{user['id']} · reply, чтобы ответить</i>",
        )
    except TelegramAPIError:
        pass

    _, autoreply, *_ = await get_texts()
    try:
        await bot.send_message(user_id, autoreply)
    except TelegramAPIError as e:
        log.error(f"autoreply failed: {e}")


# ================== Команды ==================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if is_admin(message):
        await message.answer(
            "<b>Ты — владелец бота.</b>\n\n"
            "/stats — статистика\n"
            "/block — reply на сообщение анонима → бан\n"
            "/unblock &lt;номер&gt; — разбан (номер — «Аноним #N»)\n"
            "/chat &lt;номер&gt; — режим диалога\n"
            "/stop — выйти из диалога\n"
            "/show_settings — текущие тексты и лимит\n"
            "/set_welcome — reply на сообщение → новое приветствие\n"
            "/set_autoreply — reply → новый автоответ\n"
            "/set_antispam_text — reply → текст антиспама\n"
            "/set_antispam_limit &lt;n&gt; &lt;сек&gt; — лимит и окно",
            reply_markup=admin_menu  # <--- ДОБАВИЛИ ЭТО
        )
        return
    await send_welcome_if_needed(message)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if is_admin(message):
        await cmd_start(message)
        return
    await message.answer(
        "Просто напиши сюда сообщение — оно уйдёт владельцу анонимно. "
        "Ответ придёт сюда же."
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message):
        return
    total_users, blocked, total_msgs = await db.get_stats()
    uptime = int(time.time() - START_TIME)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    await message.answer(
        f"<b>Статистика</b>\n\n"
        f"👥 Пользователей: <code>{total_users}</code>\n"
        f"🚫 В бане: <code>{blocked}</code>\n"
        f"📨 Доставлено сообщений: <code>{total_msgs}</code>\n"
        f"⏱ Аптайм: <code>{h}ч {m}м {s}с</code>"
    )


@dp.message(Command("block"))
async def cmd_block(message: Message):
    if not is_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("Reply на сообщение анонима, которого надо забанить.")
        return
    tg_id = await db.lookup_message_map(
        message.chat.id, message.reply_to_message.message_id
    )
    if not tg_id:
        await message.answer("Не нашёл, кому принадлежит сообщение.")
        return
    await db.set_blocked(tg_id, True)
    user = await db.get_user_by_tg(tg_id)
    n = user["id"] if user else "?"
    await message.answer(f"Аноним #{n} заблокирован.")
    try:
        await bot.send_message(tg_id, "Вы заблокированы.")
    except TelegramAPIError:
        pass
@dp.message(Command("chat"))
async def cmd_chat(message: Message):
    if not is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /chat <номер>\nНапример: /chat 1")
        return
    
    internal_id = int(parts[1])
    user = await db.get_user_by_internal_id(internal_id)
    
    if not user:
        await message.answer(f"Аноним #{internal_id} не найден.")
        return
    if user["is_blocked"]:
        await message.answer(f"Аноним #{internal_id} в бане. Сначала разбаньте его через /unblock.")
        return

    active_chats[message.from_user.id] = user["tg_id"]
    await message.answer(
        f"<b>Режим диалога с Анонимом #{internal_id} включен.</b>\n\n"
        "Все ваши сообщения (текст, фото, стикеры) будут пересылаться ему.\n"
        "Чтобы выйти из диалога, напишите /stop"
    )


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if not is_admin(message):
        return
    if message.from_user.id in active_chats:
        del active_chats[message.from_user.id]
        await message.answer("Режим диалога выключен. Теперь ваши сообщения не пересылаются анонимам.")
    else:
        await message.answer("Вы и так не в режиме диалога.")

@dp.message(Command("unblock"))
async def cmd_unblock(message: Message):
    if not is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "Использование: /unblock &lt;номер&gt;\n"
            "Номер показан под пересланным сообщением: «Аноним #N»."
        )
        return
    internal_id = int(parts[1])
    user = await db.get_user_by_internal_id(internal_id)
    if not user:
        await message.answer(f"Аноним #{internal_id} не найден.")
        return
    await db.set_blocked(user["tg_id"], False)
    await message.answer(f"Аноним #{internal_id} разблокирован.")
    try:
        await bot.send_message(user["tg_id"], "Вы разблокированы. Можно снова писать.")
    except TelegramAPIError:
        pass


@dp.message(Command("show_settings"))
async def cmd_show_settings(message: Message):
    if not is_admin(message):
        return
    welcome, autoreply, antispam_text, limit, window = await get_texts()
    await message.answer(
        f"<b>Текущие настройки</b>\n\n"
        f"<b>Приветствие:</b>\n{welcome}\n\n"
        f"<b>Автоответ:</b>\n{autoreply}\n\n"
        f"<b>Текст антиспама:</b>\n{antispam_text}\n\n"
        f"<b>Лимит:</b> <code>{limit}</code> сообщ. за <code>{window}</code> сек."
    )


@dp.message(Command("set_welcome"))
async def cmd_set_welcome(message: Message):
    if not is_admin(message):
        return
    if not message.reply_to_message:
        await message.answer(
            "Reply на сообщение, текст которого станет новым приветствием.\n"
            "Используй <code>{first_name}</code> — подставится имя."
        )
        return
    new_text = message.reply_to_message.text or message.reply_to_message.caption
    if not new_text:
        await message.answer("В том сообщении нет текста.")
        return
    await db.set_setting("welcome_text", new_text)
    await message.answer("Приветствие обновлено.")


@dp.message(Command("set_autoreply"))
async def cmd_set_autoreply(message: Message):
    if not is_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("Reply на сообщение → новый автоответ.")
        return
    new_text = message.reply_to_message.text or message.reply_to_message.caption
    if not new_text:
        await message.answer("В том сообщении нет текста.")
        return
    await db.set_setting("autoreply_text", new_text)
    await message.answer("Автоответ обновлён.")


@dp.message(Command("set_antispam_text"))
async def cmd_set_antispam_text(message: Message):
    if not is_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("Reply на сообщение → текст антиспама.")
        return
    new_text = message.reply_to_message.text or message.reply_to_message.caption
    if not new_text:
        await message.answer("В том сообщении нет текста.")
        return
    await db.set_setting("antispam_text", new_text)
    await message.answer("Текст антиспама обновлён.")


@dp.message(Command("set_antispam_limit"))
async def cmd_set_antispam_limit(message: Message):
    if not is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Использование: /set_antispam_limit &lt;n&gt; &lt;сек&gt;")
        return
    limit, window = int(parts[1]), int(parts[2])
    if limit < 1 or window < 1:
        await message.answer("Значения должны быть ≥ 1.")
        return
    await db.set_setting("antispam_limit", str(limit))
    await db.set_setting("antispam_window", str(window))
    await message.answer(f"Лимит: <code>{limit}</code> за <code>{window}</code> сек.")


# ================== Ответ админа ==================

@dp.message(F.from_user.id == config.ADMIN_ID)
async def admin_reply(message: Message):
    if message.text and message.text.startswith("/"):
        return  # Пропускаем команды (например, /stats)

    target_tg_id = None

    # 1. Проверяем, включен ли режим диалога (/chat)
    if message.from_user.id in active_chats:
        target_tg_id = active_chats[message.from_user.id]
    
    # 2. Если режима нет, но админ сделал Reply
    elif message.reply_to_message:
        target_tg_id = await db.lookup_message_map(
            message.chat.id, message.reply_to_message.message_id
        )
        if not target_tg_id:
            await message.answer(
                "Не нашёл, кому отвечать (возможно, это сообщение бота). "
                "Используйте /chat <номер> или Reply на сообщение анонима."
            )
            return
    else:
        return # Админ просто пишет в пустоту, ничего не делаем

    # Проверяем, не забанен ли аноним
    user = await db.get_user_by_tg(target_tg_id)
    if user and user["is_blocked"]:
        await message.answer("Этот аноним заблокирован. Сначала /unblock.")
        return

    # Отправляем сообщение анониму
    try:
        await bot.copy_message(
            chat_id=target_tg_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramAPIError as e:
        log.error(f"admin reply failed: {e}")
        await message.answer(f"Не удалось доставить: {e}")
        return
    
    await message.answer("Доставлено анониму.")

# ================== Сообщения от пользователей ==================

@dp.message()
async def user_message(message: Message):
    if is_admin(message):
        return
    if not message.from_user:
        return

    user_id = message.from_user.id
    user = await db.get_or_create_user(user_id)

    if user["is_blocked"]:
        try:
            await bot.send_message(user_id, "Вы заблокированы.")
        except TelegramAPIError:
            pass
        return

    await send_welcome_if_needed(message)

    _, _, antispam_text, limit, window = await get_texts()

    # Альбом: считаем 1 "использование" на первый message медиагруппы
    if message.media_group_id:
        key = (user_id, message.media_group_id)
        if key not in _album_buffer:
            ok = await check_rate_limit(user_id, limit, window)
            if not ok:
                now = time.time()
                if now - _last_warn.get(user_id, 0) > 10:
                    _last_warn[user_id] = now
                    try:
                        await bot.send_message(user_id, antispam_text)
                    except TelegramAPIError:
                        pass
                return
            _album_buffer[key] = []
            _album_tasks[key] = asyncio.create_task(_flush_album(key))
        _album_buffer[key].append(message)
        return

    # Обычное сообщение
    ok = await check_rate_limit(user_id, limit, window)
    if not ok:
        now = time.time()
        if now - _last_warn.get(user_id, 0) > 10:
            _last_warn[user_id] = now
            try:
                await bot.send_message(user_id, antispam_text)
            except TelegramAPIError:
                pass
        return

    await deliver_single(message)


# ================== Health / Webhook / Main ==================
# ================== Обработка кнопок ==================

@dp.callback_query(F.data.startswith("reply_"))
async def cb_reply(callback: CallbackQuery):
    internal_id = int(callback.data.split("_")[1])
    user = await db.get_user_by_internal_id(internal_id)
    
    if not user:
        await callback.answer("Аноним не найден", show_alert=True)
        return
        
    active_chats[callback.from_user.id] = user["tg_id"]
    
    await callback.message.edit_text(
        f"<i>Аноним #{internal_id} (Режим ответа включен)</i>",
        reply_markup=None
    )
    await callback.message.answer("Режим ответа включен. Пишите текст или прикрепите медиа. Для выхода — /stop или кнопка «Выйти из диалога».")
    await callback.answer()

@dp.callback_query(F.data.startswith("block_"))
async def cb_block(callback: CallbackQuery):
    internal_id = int(callback.data.split("_")[1])
    user = await db.get_user_by_internal_id(internal_id)
    
    if user:
        await db.set_blocked(user["tg_id"], True)
        await callback.message.edit_text(
            f"<i>Аноним #{internal_id} [ЗАБЛОКИРОВАН]</i>",
            reply_markup=None
        )
        await callback.answer("Аноним заблокирован")
        try:
            await bot.send_message(user["tg_id"], "Вы заблокированы.")
        except TelegramAPIError:
            pass
    else:
        await callback.answer("Аноним не найден", show_alert=True)

# Обработка кнопок нижнего меню
@dp.message(F.text == "📊 Статистика")
async def btn_stats(message: Message):
    await cmd_stats(message)

@dp.message(F.text == "⚙️ Настройки")
async def btn_settings(message: Message):
    await cmd_show_settings(message)

@dp.message(F.text == "🛑 Выйти из диалога")
async def btn_stop(message: Message):
    await cmd_stop(message)

async def health(request):
    return web.Response(text="OK")


async def _run_webhook():
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    await bot.set_webhook(config.WEBHOOK_URL)
    log.info(f"Webhook set to {config.WEBHOOK_URL}")
    await asyncio.Event().wait()


async def _run_polling():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info(f"Health server on :{config.PORT}")
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Polling started")
    await dp.start_polling(bot, allowed_updates=["message"])


async def main():
    await db.init_db()
    log.info("DB initialized")
    try:
        if config.USE_WEBHOOK:
            await _run_webhook()
        else:
            await _run_polling()
    finally:
        await db.close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped")
