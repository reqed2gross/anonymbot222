import asyncio
import html
import logging
import time
import math

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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
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
dp = Dispatcher(storage=MemoryStorage())

# Буферы и состояния
_album_buffer: dict[tuple[int, str], list[Message]] = {}
_album_tasks: dict[tuple[int, str], asyncio.Task] = {}
_last_warn: dict[int, float] = {}
active_chats: dict[int, int] = {}  # {admin_id: tg_id}

# Машина состояний для изменения текстов
class AdminStates(StatesGroup):
    waiting_for_welcome = State()
    waiting_for_autoreply = State()
    waiting_for_antispam_text = State()
    waiting_for_antispam_limit = State()

def is_admin(message: Message) -> bool:
    return bool(message.from_user) and message.from_user.id == config.ADMIN_ID

# ================== Клавиатуры ==================

def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="💬 Выбрать чат")]
        ],
        resize_keyboard=True
    )

def get_dialog_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛑 Завершить диалог")],
            [KeyboardButton(text="🔙 В главное меню")]
        ],
        resize_keyboard=True
    )

def get_settings_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить приветствие", callback_data="set_welcome")],
        [InlineKeyboardButton(text="Изменить автоответ", callback_data="set_autoreply")],
        [InlineKeyboardButton(text="Изменить текст антиспама", callback_data="set_antispam_text")],
        [InlineKeyboardButton(text="Изменить лимит антиспама", callback_data="set_antispam_limit")],
        [InlineKeyboardButton(text="Текущие тексты и лимит", callback_data="show_settings")]
    ])

async def get_chats_menu(page: int):
    limit = 6
    offset = page * limit
    users = await db.get_users_paginated(offset, limit)
    total_users = await db.get_users_count()
    total_pages = math.ceil(total_users / limit)

    kb = []
    # Кнопки с анонимами (по 2 в ряд)
    row = []
    for u in users:
        status = "🚫" if u["is_blocked"] else "✅"
        row.append(InlineKeyboardButton(text=f"Аноним #{u['id']} {status}", callback_data=f"select_{u['id']}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    # Навигация (Назад / Вперед)
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{page-1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page_{page+1}"))
    if nav_row:
        kb.append(nav_row)
        
    # Кнопка в меню
    kb.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")])

    return InlineKeyboardMarkup(inline_keyboard=kb)

# ================== Отправка сообщений ==================

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

async def deliver_single(message: Message):
    admin_id = config.ADMIN_ID
    user_id = message.from_user.id
    try:
        sent = await bot.copy_message(
            chat_id=admin_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramAPIError as e:
        log.error(f"delivery to admin failed: {e}")
        return

    await db.save_message_map(admin_id, sent.message_id, user_id)
    user = await db.get_or_create_user(user_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply_{user['id']}"),
            InlineKeyboardButton(text="🚫 Забанить", callback_data=f"block_{user['id']}")
        ]
    ])

    try:
        await bot.send_message(
            admin_id,
            f"<i>Аноним #{user['id']}</i>",
            reply_parameters=ReplyParameters(message_id=sent.message_id, allow_sending_without_reply=True),
            reply_markup=kb
        )
    except TelegramAPIError:
        pass

    _, autoreply, *_ = await get_texts()
    try:
        await bot.send_message(user_id, autoreply)
    except TelegramAPIError as e:
        log.error(f"autoreply failed: {e}")

async def _flush_album(key: tuple[int, str]):
    await asyncio.sleep(1.0)
    messages = _album_buffer.pop(key, [])
    _album_tasks.pop(key, None)
    if not messages:
        return

    user_id = key[0]
    admin_id = config.ADMIN_ID

    photos, videos, anims = [], [], []
    non_group = False

    for msg in messages:
        if msg.photo: photos.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=msg.caption))
        elif msg.video: videos.append(InputMediaVideo(media=msg.video.file_id, caption=msg.caption))
        elif msg.animation: anims.append(InputMediaAnimation(media=msg.animation.file_id, caption=msg.caption))
        else: non_group = True

    if non_group or (photos and videos) or (photos and anims) or (videos and anims):
        for msg in messages:
            try:
                sent = await bot.copy_message(chat_id=admin_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
                await db.save_message_map(admin_id, sent.message_id, user_id)
            except TelegramAPIError: pass
    else:
        items = photos or videos or anims
        if items:
            try:
                sent_list = await bot.send_media_group(admin_id, items)
                for s in sent_list: await db.save_message_map(admin_id, s.message_id, user_id)
            except TelegramAPIError:
                for msg in messages:
                    try:
                        sent = await bot.copy_message(chat_id=admin_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
                        await db.save_message_map(admin_id, sent.message_id, user_id)
                    except TelegramAPIError: pass

    user = await db.get_or_create_user(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply_{user['id']}"),
            InlineKeyboardButton(text="🚫 Забанить", callback_data=f"block_{user['id']}")
        ]
    ])
    try:
        await bot.send_message(admin_id, f"<i>Альбом от Анонима #{user['id']}</i>", reply_markup=kb)
    except TelegramAPIError: pass

    _, autoreply, *_ = await get_texts()
    try:
        await bot.send_message(user_id, autoreply)
    except TelegramAPIError as e:
        log.error(f"autoreply failed: {e}")

# ================== Админ: Обработка кнопок ==================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if is_admin(message):
        await message.answer("👋 <b>Главное меню</b>\nВыберите действие на клавиатуре ниже:", reply_markup=get_main_menu())
        return
    await send_welcome_if_needed(message)

@dp.message(F.text == "🔙 В главное меню")
async def back_to_main(message: Message):
    if not is_admin(message): return
    active_chats.pop(message.from_user.id, None)
    await message.answer("👋 <b>Главное меню</b>", reply_markup=get_main_menu())

@dp.message(F.text == "🛑 Завершить диалог")
async def end_dialog(message: Message):
    if not is_admin(message): return
    if message.from_user.id in active_chats:
        del active_chats[message.from_user.id]
        await message.answer("Диалог завершен.", reply_markup=get_main_menu())
    else:
        await message.answer("Вы не в диалоге.", reply_markup=get_main_menu())

@dp.message(F.text == "📊 Статистика")
async def btn_stats(message: Message):
    if not is_admin(message): return
    total_users, blocked, total_msgs = await db.get_stats()
    uptime = int(time.time() - START_TIME)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    await message.answer(
        f"<b>Статистика</b>\n\n"
        f"👥 Пользователей: <code>{total_users}</code>\n"
        f"🚫 В бане: <code>{blocked}</code>\n"
        f"📨 Доставлено: <code>{total_msgs}</code>\n"
        f"⏱ Аптайм: <code>{h}ч {m}м {s}с</code>"
    )

@dp.message(F.text == "⚙️ Настройки")
async def btn_settings(message: Message):
    if not is_admin(message): return
    await message.answer("⚙️ <b>Настройки бота</b>", reply_markup=get_settings_menu())

@dp.message(F.text == "💬 Выбрать чат")
async def btn_chats(message: Message):
    if not is_admin(message): return
    await message.answer("Выберите анонима для диалога:", reply_markup=await get_chats_menu(0))

# ================== Админ: Callback (Инлайн-кнопки) ==================

@dp.callback_query()
async def handle_callbacks(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    data = callback.data

    # Пагинация чатов
    if data.startswith("page_"):
        page = int(data.split("_")[1])
        await callback.message.edit_reply_markup(reply_markup=await get_chats_menu(page))
        await callback.answer()
        return
    
    if data == "main_menu":
        active_chats.pop(callback.from_user.id, None)
        await callback.message.delete()
        await callback.message.answer("👋 <b>Главное меню</b>", reply_markup=get_main_menu())
        await callback.answer()
        return

    # Выбор чата из списка
    if data.startswith("select_"):
        internal_id = int(data.split("_")[1])
        user = await db.get_user_by_internal_id(internal_id)
        if not user:
            await callback.answer("Аноним не найден", show_alert=True)
            return
        active_chats[callback.from_user.id] = user["tg_id"]
        await callback.message.delete()
        await callback.message.answer(
            f"💬 <b>Диалог с Анонимом #{internal_id}</b>\n\nВсе ваши сообщения будут уходить ему. Для выхода нажмите кнопку ниже.",
            reply_markup=get_dialog_menu()
        )
        await callback.answer()
        return

    # Кнопки под сообщениями
    if data.startswith("reply_"):
        internal_id = int(data.split("_")[1])
        user = await db.get_user_by_internal_id(internal_id)
        if user:
            active_chats[callback.from_user.id] = user["tg_id"]
            await callback.message.answer(f"💬 <b>Диалог с Анонимом #{internal_id}</b>", reply_markup=get_dialog_menu())
            await callback.answer("Режим диалога включен")
        return

    if data.startswith("block_"):
        internal_id = int(data.split("_")[1])
        user = await db.get_user_by_internal_id(internal_id)
        if user:
            await db.set_blocked(user["tg_id"], True)
            await callback.message.edit_text(f"<i>Аноним #{internal_id} [ЗАБЛОКИРОВАН]</i>")
            await callback.answer("Забанен")
            try: await bot.send_message(user["tg_id"], "Вы заблокированы.")
            except: pass
        return

    # Настройки
    if data == "show_settings":
        welcome, autoreply, antispam_text, limit, window = await get_texts()
        await callback.message.answer(
            f"<b>Текущие настройки</b>\n\n"
            f"<b>Приветствие:</b>\n{welcome}\n\n"
            f"<b>Автоответ:</b>\n{autoreply}\n\n"
            f"<b>Антиспам:</b>\n{antispam_text}\n\n"
            f"<b>Лимит:</b> <code>{limit}</code> за <code>{window}</code> сек."
        )
        await callback.answer()
        return

    # Запуск ожидания ввода
    states_map = {
        "set_welcome": (AdminStates.waiting_for_welcome, "Отправьте новый текст <b>приветствия</b> одним сообщением:"),
        "set_autoreply": (AdminStates.waiting_for_autoreply, "Отправьте новый текст <b>автоответа</b>:"),
        "set_antispam_text": (AdminStates.waiting_for_antispam_text, "Отправьте новый текст <b>антиспама</b>:"),
        "set_antispam_limit": (AdminStates.waiting_for_antispam_limit, "Отправьте новый лимит в формате: <code>5 30</code> (5 сообщений за 30 секунд):")
    }

    if data in states_map:
        target_state, prompt = states_map[data]
        await state.set_state(target_state)
        await callback.message.answer(prompt)
        await callback.answer()

# ================== Обработка состояний (Сохранение настроек) ==================

@dp.message(AdminStates.waiting_for_welcome)
async def process_welcome(message: Message, state: FSMContext):
    if message.text:
        await db.set_setting("welcome_text", message.text)
        await message.answer("✅ Приветствие обновлено.", reply_markup=get_main_menu())
        await state.clear()

@dp.message(AdminStates.waiting_for_autoreply)
async def process_autoreply(message: Message, state: FSMContext):
    if message.text:
        await db.set_setting("autoreply_text", message.text)
        await message.answer("✅ Автоответ обновлен.", reply_markup=get_main_menu())
        await state.clear()

@dp.message(AdminStates.waiting_for_antispam_text)
async def process_antispam_text(message: Message, state: FSMContext):
    if message.text:
        await db.set_setting("antispam_text", message.text)
        await message.answer("✅ Текст антиспама обновлен.", reply_markup=get_main_menu())
        await state.clear()

@dp.message(AdminStates.waiting_for_antispam_limit)
async def process_antispam_limit(message: Message, state: FSMContext):
    parts = (message.text or "").split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        await db.set_setting("antispam_limit", parts[0])
        await db.set_setting("antispam_window", parts[1])
        await message.answer("✅ Лимит антиспама обновлен.", reply_markup=get_main_menu())
        await state.clear()
    else:
        await message.answer("❌ Ошибка! Отправьте два числа через пробел (например, <code>5 30</code>).")

# ================== Админ: Ответы и тексты ==================

@dp.message(F.from_user.id == config.ADMIN_ID)
async def admin_reply(message: Message):
    if message.text and message.text.startswith("/"):
        return
    
    # 1. Проверяем активный диалог
    target_tg_id = active_chats.get(message.from_user.id)
    
    # 2. Если нет активного, проверяем Reply
    if not target_tg_id and message.reply_to_message:
        target_tg_id = await db.lookup_message_map(message.chat.id, message.reply_to_message.message_id)
        if not target_tg_id:
            await message.answer("Не нашёл, кому отвечать. Выберите чат через меню или сделайте Reply.")
            return
    elif not target_tg_id:
        return # Если просто пишет в пустоту

    user = await db.get_user_by_tg(target_tg_id)
    if user and user["is_blocked"]:
        await message.answer("Этот аноним заблокирован. Сначала разбаньте его.")
        return

    try:
        await bot.copy_message(chat_id=target_tg_id, from_chat_id=message.chat.id, message_id=message.message_id)
        if not active_chats.get(message.from_user.id):
            await message.answer("Доставлено анониму.")
    except TelegramAPIError as e:
        await message.answer(f"Не удалось доставить: {e}")

# ================== Обычные пользователи ==================

@dp.message()
async def user_message(message: Message):
    if is_admin(message) or not message.from_user: return

    user_id = message.from_user.id
    user = await db.get_or_create_user(user_id)

    if user["is_blocked"]:
        try: await bot.send_message(user_id, "Вы заблокированы.")
        except: pass
        return

    _, _, antispam_text, limit, window = await get_texts()

    if message.media_group_id:
        key = (user_id, message.media_group_id)
        if key not in _album_buffer:
            if not await check_rate_limit(user_id, limit, window):
                if time.time() - _last_warn.get(user_id, 0) > 10:
                    _last_warn[user_id] = time.time()
                    try: await bot.send_message(user_id, antispam_text)
                    except: pass
                return
            _album_buffer[key] = []
            _album_tasks[key] = asyncio.create_task(_flush_album(key))
        _album_buffer[key].append(message)
        return

    if not await check_rate_limit(user_id, limit, window):
        if time.time() - _last_warn.get(user_id, 0) > 10:
            _last_warn[user_id] = time.time()
            try: await bot.send_message(user_id, antispam_text)
            except: pass
        return

    await deliver_single(message)

# ================== Webhook / Main ==================

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
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

async def main():
    await db.init_db()
    try:
        if config.USE_WEBHOOK: await _run_webhook()
        else: await _run_polling()
    finally:
        await db.close_db()

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
