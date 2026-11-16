import asyncio
import json
import logging
import os
import time
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiohttp import web
from database import (
    admin_add_gift,
    admin_change_balance,
    create_withdrawal,
    find_user_by_query,
    get_global_stats,
    get_or_create_user,
    get_pending_withdrawals,
    get_withdrawal,
    init_db,
    save_user_state,
    set_withdrawal_status,
)


# =========================================================
# КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ (.ENV)
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения (.env)!")

VERCEL_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
if not VERCEL_URL:
    raise RuntimeError("Не задан WEBAPP_URL в переменных окружения (.env)!")

API_PUBLIC_URL = os.getenv("API_PUBLIC_URL", "").strip().rstrip("/")
API_HOST = "0.0.0.0"
API_PORT = int(os.getenv("PORT", os.getenv("SERVER_PORT", 8080)))

ADMIN_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

GIFT_PRESETS = {
    "g1": {"id": "g1", "name": "Heart Locket", "tag": "Gift • Tier I", "price": 1, "color": "#60A5FA", "svgType": "heart"},
    "g4": {"id": "g4", "name": "Sweet Rose", "tag": "Gift • Tier I", "price": 5, "color": "#60A5FA", "svgType": "rose"},
    "g7": {"id": "g7", "name": "Crystal Cup", "tag": "Gift • Uncommon", "price": 15, "color": "#34D399", "svgType": "trophy"},
    "g11": {"id": "g11", "name": "Blue Diamond", "tag": "Gift • Rare", "price": 50, "color": "#A855F7", "svgType": "diamond"},
    "g14": {"id": "g14", "name": "Magic Potion", "tag": "Gift • Rare", "price": 100, "color": "#A855F7", "svgType": "potion"},
    "g16": {"id": "g16", "name": "Durov Cap", "tag": "NFT • Epic", "price": 150, "color": "#EC4899", "svgType": "cap"},
    "g22": {"id": "g22", "name": "Pepe Plush", "tag": "NFT • Legendary", "price": 410, "color": "#FBD506", "svgType": "pepe"},
    "g24": {"id": "g24", "name": "Golden Star", "tag": "NFT • Immortal", "price": 500, "color": "#FBD506", "svgType": "star"},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stargrader_bot")

bot_instance: Bot | None = None
avatar_cache: dict[int, tuple[float, bytes]] = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


class UserStates(StatesGroup):
    waiting_topup_amount = State()


class AdminStates(StatesGroup):
    waiting_give_stars_target = State()
    waiting_give_stars_amount = State()
    waiting_take_stars_target = State()
    waiting_take_stars_amount = State()
    waiting_give_gift_target = State()
    waiting_give_gift_id = State()
    waiting_find_user = State()


def build_webapp_url(user=None) -> str:
    params = {}
    if user is not None:
        params["uid"] = str(user.id)
        params["first_name"] = user.first_name or "Игрок"
        params["username"] = user.username or ""
        if API_PUBLIC_URL:
            params["avatar_url"] = f"{API_PUBLIC_URL}/api/avatar?user_id={user.id}"
    if API_PUBLIC_URL:
        params["api"] = API_PUBLIC_URL

    if params:
        return f"{VERCEL_URL}/?{urlencode(params)}"
    return f"{VERCEL_URL}/"


def build_reply_keyboard(user) -> ReplyKeyboardMarkup:
    url = build_webapp_url(user)
    rows = [
        [
            KeyboardButton(
                text="🚀 Открыть STARGRADER",
                web_app=WebAppInfo(url=url),
            )
        ],
        [
            KeyboardButton(text="⭐ Пополнить Stars"),
            KeyboardButton(text="👤 Мой профиль"),
        ],
    ]
    if is_admin(user.id):
        rows.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_admin_inline_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Выдать ⭐", callback_data="adm:give_stars"),
                InlineKeyboardButton(text="➖ Списать ⭐", callback_data="adm:take_stars"),
            ],
            [
                InlineKeyboardButton(text="🎁 Выдать подарок", callback_data="adm:give_gift"),
                InlineKeyboardButton(text="🔍 Найти игрока", callback_data="adm:find_user"),
            ],
            [
                InlineKeyboardButton(text="📤 Заявки на вывод", callback_data="adm:withdrawals"),
                InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
            ],
        ]
    )


# =========================================================
# REST API ДЛЯ СВЯЗИ VERCEL <-> BOTHOST (БД, АВАТАР, ИНВОЙСЫ, ВЫВОД)
# =========================================================
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "stargrader_api"})


async def api_get_user(request: web.Request) -> web.Response:
    uid_raw = request.query.get("user_id", "")
    if not uid_raw.isdigit():
        return web.json_response({"ok": False, "error": "invalid user_id"}, status=400)

    user_id = int(uid_raw)
    username = request.query.get("username", "")
    first_name = request.query.get("first_name", "Игрок")
    photo_url = request.query.get("photo_url", "")

    data = get_or_create_user(
        user_id=user_id,
        username=username,
        first_name=first_name,
        photo_url=photo_url,
    )
    return web.json_response({"ok": True, "user": data})


async def api_save_user(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    uid_raw = str(body.get("user_id", ""))
    if not uid_raw.isdigit():
        return web.json_response({"ok": False, "error": "invalid user_id"}, status=400)

    updated = save_user_state(int(uid_raw), body)
    return web.json_response({"ok": True, "user": updated})


async def api_get_avatar(request: web.Request) -> web.StreamResponse:
    uid_raw = request.query.get("user_id", "")
    if not uid_raw.isdigit() or bot_instance is None:
        return web.Response(status=404)

    uid = int(uid_raw)
    now = time.time()
    cached = avatar_cache.get(uid)
    if cached and (now - cached[0]) < 600:
        return web.Response(body=cached[1], content_type="image/jpeg")

    try:
        photos = await bot_instance.get_user_profile_photos(user_id=uid, limit=1)
        if not photos.photos:
            return web.Response(status=404)
        file_id = photos.photos[0][-1].file_id
        tg_file = await bot_instance.get_file(file_id)
        if not tg_file.file_path:
            return web.Response(status=404)
        buf = await bot_instance.download_file(tg_file.file_path)
        raw_bytes = buf.read()
        avatar_cache[uid] = (now, raw_bytes)
        return web.Response(body=raw_bytes, content_type="image/jpeg")
    except Exception:
        return web.Response(status=404)


async def api_create_invoice(request: web.Request) -> web.Response:
    """Создаёт ссылку на оплату Telegram Stars (XTR) от 1 звезды для Web App."""
    if bot_instance is None:
        return web.json_response({"ok": False, "error": "Bot not ready"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    uid_raw = str(body.get("user_id", ""))
    if not uid_raw.isdigit():
        return web.json_response({"ok": False, "error": "invalid user_id"}, status=400)

    try:
        amount = int(float(body.get("amount", 0)))
    except (ValueError, TypeError):
        amount = 0

    if amount < 1 or amount > 100000:
        return web.json_response(
            {"ok": False, "error": "Сумма пополнения от 1 до 100 000 ⭐"},
            status=400,
        )

    user_id = int(uid_raw)
    get_or_create_user(
        user_id=user_id,
        username=str(body.get("username", "")),
        first_name=str(body.get("first_name", "Игрок")),
    )

    try:
        invoice_link = await bot_instance.create_invoice_link(
            title=f"Пополнение баланса STARGRADER ({amount} ⭐)",
            description=f"Зачисление {amount} Telegram Stars на игровой баланс",
            payload=f"topup:{user_id}:{amount}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"{amount} Stars", amount=amount)],
        )
        return web.json_response({"ok": True, "invoice_link": invoice_link})
    except Exception as e:
        logger.exception("Ошибка создания инвойса XTR")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def api_withdraw(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    uid_raw = str(body.get("user_id", ""))
    if not uid_raw.isdigit():
        return web.json_response({"ok": False, "error": "invalid user_id"}, status=400)

    user_id = int(uid_raw)
    amount = round(float(body.get("amount", 0)), 2)
    if amount < 15:
        return web.json_response({"ok": False, "error": "Минимум 15 ⭐"}, status=400)

    user = get_or_create_user(
        user_id=user_id,
        username=str(body.get("username", "")),
        first_name=str(body.get("first_name", "Игрок")),
    )
    if user["balance"] < amount:
        return web.json_response({"ok": False, "error": "Недостаточно звёзд"}, status=400)

    fee = round(amount * 0.15, 2)
    receive_amount = round(amount - fee, 2)

    updated = admin_change_balance(str(user_id), -amount)
    w_id = create_withdrawal(
        user_id=user_id,
        username=user["username"],
        first_name=user["first_name"],
        amount=amount,
        fee=fee,
        receive_amount=receive_amount,
    )

    if bot_instance:
        uname_str = f"@{user['username']}" if user["username"] else f"ID {user_id}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Выполнено", callback_data=f"w_ok:{w_id}"
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить (вернуть ⭐)", callback_data=f"w_no:{w_id}"
                    ),
                ]
            ]
        )
        for adm_id in ADMIN_IDS:
            try:
                await bot_instance.send_message(
                    adm_id,
                    f"📤 <b>Новая заявка на вывод #{w_id}</b>\n\n"
                    f"• Игрок: <b>{user['first_name']}</b> ({uname_str})\n"
                    f"• ID: <code>{user_id}</code>\n"
                    f"• Списано: <b>{amount:.2f} ⭐</b>\n"
                    f"• Комиссия (15%): <b>{fee:.2f} ⭐</b>\n"
                    f"• К получению: <b>{receive_amount:.2f} ⭐</b>",
                    reply_markup=kb,
                )
            except Exception:
                pass

    return web.json_response(
        {
            "ok": True,
            "withdrawal_id": w_id,
            "balance": updated["balance"] if updated else 0.0,
            "receive_amount": receive_amount,
        }
    )


async def start_api_server() -> web.AppRunner:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", api_health)
    app.router.add_get("/api/user", api_get_user)
    app.router.add_post("/api/user/save", api_save_user)
    app.router.add_get("/api/avatar", api_get_avatar)
    app.router.add_post("/api/create-invoice", api_create_invoice)
    app.router.add_post("/api/withdraw", api_withdraw)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, API_HOST, API_PORT)
    await site.start()
    logger.info("API сервер БД запущен на http://%s:%s", API_HOST, API_PORT)
    return runner


# =========================================================
# ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЕЙ, ИНВОЙСОВ XTR И АДМИН-ПАНЕЛИ
# =========================================================
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if not user:
        return

    get_or_create_user(
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "Игрок",
    )

    try:
        await message.bot.set_chat_menu_button(
            chat_id=user.id,
            menu_button=MenuButtonWebApp(
                text="Играть",
                web_app=WebAppInfo(url=build_webapp_url(user)),
            ),
        )
    except Exception:
        pass

    await message.answer(
        f"Привет, <b>{user.first_name}</b>! 👋\n\n"
        "Добро пожаловать в <b>STARGRADER</b>.\n"
        "• Ежедневный 7-дневный календарь наград (от <b>1 ⭐</b> до подарка на 7-й день)\n"
        "• Пополнение баланса от <b>1 ⭐</b> и вывод от <b>15 ⭐</b>\n\n"
        "Нажми кнопку ниже, чтобы запустить игру:",
        reply_markup=build_reply_keyboard(user),
    )


# ---------------- ПОПОЛНЕНИЕ В БОТЕ ОТ 1 ⭐ ----------------
@dp.message(F.text == "⭐ Пополнить Stars")
async def cmd_topup_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 ⭐", callback_data="topup:1"),
                InlineKeyboardButton(text="5 ⭐", callback_data="topup:5"),
                InlineKeyboardButton(text="15 ⭐", callback_data="topup:15"),
            ],
            [
                InlineKeyboardButton(text="50 ⭐", callback_data="topup:50"),
                InlineKeyboardButton(text="100 ⭐", callback_data="topup:100"),
                InlineKeyboardButton(text="250 ⭐", callback_data="topup:250"),
            ],
            [
                InlineKeyboardButton(text="✏️ Ввести свою сумму (от 1 ⭐)", callback_data="topup:custom")
            ],
        ]
    )
    await message.answer(
        "⭐ <b>Пополнение баланса Telegram Stars</b>\n"
        "Выберите пакет или введите произвольное количество звёзд (минимум <b>1 ⭐</b>):",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "topup:custom")
async def cb_topup_custom(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserStates.waiting_topup_amount)
    await call.message.answer("✏️ Введите целое количество звёзд для пополнения (минимум <code>1</code>):")
    await call.answer()


@dp.message(UserStates.waiting_topup_amount)
async def st_topup_custom_amount(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        return
    try:
        amount = int((message.text or "").strip())
        if amount < 1 or amount > 100000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число от 1 до 100000 (например: <code>1</code>):")
        return

    await state.clear()
    await send_stars_invoice(message, user.id, amount)


@dp.callback_query(F.data.startswith("topup:"))
async def cb_topup_preset(call: CallbackQuery) -> None:
    user = call.from_user
    if not user:
        return
    val = call.data.split(":")[1]
    if not val.isdigit():
        await call.answer()
        return
    amount = int(val)
    await send_stars_invoice(call.message, user.id, amount)
    await call.answer()


async def send_stars_invoice(target_message: Message, user_id: int, amount: int) -> None:
    await target_message.answer_invoice(
        title=f"Пополнение баланса ({amount} ⭐)",
        description=f"Зачисление {amount} Telegram Stars на баланс STARGRADER",
        payload=f"topup:{user_id}:{amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} Stars", amount=amount)],
    )


@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery) -> None:
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    user = message.from_user
    sp = message.successful_payment
    if not user or not sp:
        return

    amount = int(sp.total_amount)
    payload = sp.invoice_payload or ""
    target_uid = user.id

    if payload.startswith("topup:"):
        parts = payload.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            target_uid = int(parts[1])

    get_or_create_user(
        user_id=target_uid,
        username=user.username or "",
        first_name=user.first_name or "Игрок",
    )
    updated = admin_change_balance(str(target_uid), float(amount))

    await message.answer(
        f"✅ Оплата получена! На ваш баланс зачислено <b>+{amount} ⭐</b>.\n"
        f"Текущий баланс: <b>{updated['balance']:.2f} ⭐</b>",
        reply_markup=build_reply_keyboard(user),
    )


# ---------------- ПРОФИЛЬ ----------------
@dp.message(F.text == "👤 Мой профиль")
async def cmd_profile(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    data = get_or_create_user(
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "Игрок",
    )
    st = data["stats"]
    games = st["games"]
    wins = st["wins"]
    losses = st["losses"]
    winrate = (wins / games * 100) if games > 0 else 0.0

    await message.answer(
        f"👤 <b>Профиль игрока</b>\n"
        f"• Имя: <b>{data['first_name']}</b> (<code>{user.id}</code>)\n"
        f"• Баланс: <b>{data['balance']:.2f} ⭐</b>\n"
        f"• Подарков в инвентаре: <b>{len(data['inventory'])}</b>\n"
        f"• Побед: <b>{wins}</b> | Поражений: <b>{losses}</b> (Игр: <b>{games}</b>)\n"
        f"• Винрейт: <b>{winrate:.1f}%</b>\n"
        f"• Сумма ставок: <b>{st['totalWagered']:.2f} ⭐</b>\n"
        f"• Сумма выигрышей: <b>{st['totalWon']:.2f} ⭐</b>\n"
        f"• Календарь бонусов: <b>День {data['dailyDayIndex'] + 1}/7</b>",
        reply_markup=build_reply_keyboard(user),
    )


# ---------------- АДМИН-ПАНЕЛЬ ----------------
@dp.message(F.text == "🛠 Админ-панель")
async def cmd_admin_panel(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user or not is_admin(user.id):
        return
    await state.clear()
    await message.answer(
        "🛠 <b>Админ-панель STARGRADER</b>\nВыберите действие:",
        reply_markup=build_admin_inline_menu(),
    )


@dp.callback_query(F.data == "adm:stats")
async def cb_admin_stats(call: CallbackQuery) -> None:
    if not call.from_user or not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    gs = get_global_stats()
    await call.message.edit_text(
        "📊 <b>Общая статистика проекта</b>\n\n"
        f"• Игроков в базе: <b>{gs['total_users']}</b>\n"
        f"• Суммарный баланс: <b>{gs['total_balance']:.2f} ⭐</b>\n"
        f"• Всего сыграно игр: <b>{gs['total_games']}</b> (Побед: <b>{gs['total_wins']}</b>)\n"
        f"• Ожидающих заявок на вывод: <b>{gs['pending_withdrawals']}</b>",
        reply_markup=build_admin_inline_menu(),
    )
    await call.answer()


@dp.callback_query(F.data == "adm:give_stars")
async def cb_admin_give_stars(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_give_stars_target)
    await call.message.answer(
        "➕ Введите <code>@username</code> или <code>user_id</code> игрока, которому нужно выдать звёзды:"
    )
    await call.answer()


@dp.message(AdminStates.waiting_give_stars_target)
async def st_give_stars_target(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    target = find_user_by_query(message.text or "")
    if not target:
        await message.answer("❌ Игрок не найден в базе. Попросите его нажать /start в боте и введите снова:")
        return
    await state.update_data(target_uid=target["user_id"])
    await state.set_state(AdminStates.waiting_give_stars_amount)
    await message.answer(
        f"Игрок: <b>{target['first_name']}</b> (@{target['username'] or 'нет'}, баланс <b>{target['balance']:.2f} ⭐</b>)\n"
        "Введите количество звёзд для выдачи (например: <code>50</code>):"
    )


@dp.message(AdminStates.waiting_give_stars_amount)
async def st_give_stars_amount(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        amount = float((message.text or "").replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное число (например: 25):")
        return

    data = await state.get_data()
    target_uid = data["target_uid"]
    updated = admin_change_balance(str(target_uid), amount)
    await state.clear()

    if updated:
        await message.answer(
            f"✅ Выдано <b>+{amount:.2f} ⭐</b> игроку <b>{updated['first_name']}</b>.\n"
            f"Новый баланс: <b>{updated['balance']:.2f} ⭐</b>",
            reply_markup=build_admin_inline_menu(),
        )
        try:
            await message.bot.send_message(
                target_uid,
                f"🎁 Администратор начислил вам <b>+{amount:.2f} ⭐</b>!\n"
                f"Текущий баланс: <b>{updated['balance']:.2f} ⭐</b>",
            )
        except Exception:
            pass


@dp.callback_query(F.data == "adm:take_stars")
async def cb_admin_take_stars(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_take_stars_target)
    await call.message.answer(
        "➖ Введите <code>@username</code> или <code>user_id</code> игрока для списания звёзд:"
    )
    await call.answer()


@dp.message(AdminStates.waiting_take_stars_target)
async def st_take_stars_target(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    target = find_user_by_query(message.text or "")
    if not target:
        await message.answer("❌ Игрок не найден в базе. Введите корректный <code>@username</code> или ID:")
        return
    await state.update_data(target_uid=target["user_id"])
    await state.set_state(AdminStates.waiting_take_stars_amount)
    await message.answer(
        f"Игрок: <b>{target['first_name']}</b> (баланс <b>{target['balance']:.2f} ⭐</b>)\n"
        "Введите сумму для списания:"
    )


@dp.message(AdminStates.waiting_take_stars_amount)
async def st_take_stars_amount(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    try:
        amount = float((message.text or "").replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное число:")
        return

    data = await state.get_data()
    target_uid = data["target_uid"]
    updated = admin_change_balance(str(target_uid), -amount)
    await state.clear()

    if updated:
        await message.answer(
            f"✅ Списано <b>-{amount:.2f} ⭐</b> у игрока <b>{updated['first_name']}</b>.\n"
            f"Остаток: <b>{updated['balance']:.2f} ⭐</b>",
            reply_markup=build_admin_inline_menu(),
        )


@dp.callback_query(F.data == "adm:give_gift")
async def cb_admin_give_gift(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_give_gift_target)
    await call.message.answer(
        "🎁 Введите <code>@username</code> или <code>user_id</code> получателя подарка:"
    )
    await call.answer()


@dp.message(AdminStates.waiting_give_gift_target)
async def st_give_gift_target(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    target = find_user_by_query(message.text or "")
    if not target:
        await message.answer("❌ Игрок не найден. Введите <code>@username</code> или ID:")
        return
    await state.update_data(target_uid=target["user_id"])
    await state.set_state(AdminStates.waiting_give_gift_id)
    gifts_list = "\n".join(
        [f"• <code>{k}</code> — {v['name']} ({v['price']} ⭐)" for k, v in GIFT_PRESETS.items()]
    )
    await message.answer(
        f"Выберите код подарка для <b>{target['first_name']}</b>:\n\n{gifts_list}"
    )


@dp.message(AdminStates.waiting_give_gift_id)
async def st_give_gift_id(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    code = (message.text or "").strip().lower()
    if code not in GIFT_PRESETS:
        await message.answer("❌ Неверный код. Введите один из: " + ", ".join(GIFT_PRESETS.keys()))
        return

    data = await state.get_data()
    target_uid = data["target_uid"]
    preset = GIFT_PRESETS[code]
    gift_instance = {
        "uid": f"uid_{int(time.time() * 1000)}_{code}",
        **preset,
    }
    updated = admin_add_gift(str(target_uid), gift_instance)
    await state.clear()

    if updated:
        await message.answer(
            f"✅ Подарок <b>{preset['name']} ({preset['price']} ⭐)</b> выдан игроку <b>{updated['first_name']}</b>!",
            reply_markup=build_admin_inline_menu(),
        )
        try:
            await message.bot.send_message(
                target_uid,
                f"🎁 Вам выдан подарок <b>{preset['name']} ({preset['price']} ⭐)</b>!",
            )
        except Exception:
            pass


@dp.callback_query(F.data == "adm:find_user")
async def cb_admin_find_user(call: CallbackQuery, state: FSMContext) -> None:
    if not call.from_user or not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_find_user)
    await call.message.answer("🔍 Введите <code>@username</code> или <code>user_id</code> игрока:")
    await call.answer()


@dp.message(AdminStates.waiting_find_user)
async def st_find_user(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    target = find_user_by_query(message.text or "")
    await state.clear()
    if not target:
        await message.answer("❌ Игрок не найден в базе.", reply_markup=build_admin_inline_menu())
        return

    st = target["stats"]
    await message.answer(
        f"🔍 <b>Карточка игрока</b>\n\n"
        f"• Имя: <b>{target['first_name']}</b> (@{target['username'] or 'нет'})\n"
        f"• ID: <code>{target['user_id']}</code>\n"
        f"• Баланс: <b>{target['balance']:.2f} ⭐</b>\n"
        f"• Подарков: <b>{len(target['inventory'])}</b>\n"
        f"• Игр: <b>{st['games']}</b> (Побед: <b>{st['wins']}</b>, Поражений: <b>{st['losses']}</b>)\n"
        f"• Ставок: <b>{st['totalWagered']:.2f} ⭐</b> | Выигрышей: <b>{st['totalWon']:.2f} ⭐</b>\n"
        f"• Календарь: <b>День {target['dailyDayIndex'] + 1}/7</b>",
        reply_markup=build_admin_inline_menu(),
    )


@dp.callback_query(F.data == "adm:withdrawals")
async def cb_admin_withdrawals(call: CallbackQuery) -> None:
    if not call.from_user or not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    items = get_pending_withdrawals()
    if not items:
        await call.message.edit_text(
            "📤 Активных заявок на вывод сейчас нет.",
            reply_markup=build_admin_inline_menu(),
        )
        await call.answer()
        return

    lines = ["📤 <b>Ожидающие заявки на вывод:</b>\n"]
    buttons = []
    for w in items[:10]:
        uname = f"@{w['username']}" if w["username"] else f"ID {w['user_id']}"
        lines.append(
            f"• <b>#{w['id']}</b> | {w['first_name']} ({uname}) — <b>{w['receive_amount']:.2f} ⭐</b> (списано {w['amount']:.2f} ⭐)"
        )
        buttons.append(
            [
                InlineKeyboardButton(text=f"✅ #{w['id']}", callback_data=f"w_ok:{w['id']}"),
                InlineKeyboardButton(text=f"❌ #{w['id']}", callback_data=f"w_no:{w['id']}"),
            ]
        )

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("w_ok:"))
async def cb_withdraw_approve(call: CallbackQuery) -> None:
    if not call.from_user or not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    w_id = int(call.data.split(":")[1])
    w = get_withdrawal(w_id)
    if not w or w["status"] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return

    set_withdrawal_status(w_id, "approved")
    await call.message.edit_text(
        f"✅ Заявка <b>#{w_id}</b> подтверждена! Игроку отправлено <b>{w['receive_amount']:.2f} ⭐</b>."
    )
    try:
        await call.bot.send_message(
            w["user_id"],
            f"✅ Ваша заявка на вывод <b>#{w_id}</b> выполнена!\n"
            f"Отправлено: <b>{w['receive_amount']:.2f} ⭐</b> (с учётом комиссии 15%).",
        )
    except Exception:
        pass
    await call.answer("Выполнено")


@dp.callback_query(F.data.startswith("w_no:"))
async def cb_withdraw_reject(call: CallbackQuery) -> None:
    if not call.from_user or not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    w_id = int(call.data.split(":")[1])
    w = get_withdrawal(w_id)
    if not w or w["status"] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return

    set_withdrawal_status(w_id, "rejected")
    admin_change_balance(str(w["user_id"]), float(w["amount"]))
    await call.message.edit_text(
        f"❌ Заявка <b>#{w_id}</b> отклонена. На баланс игрока возвращено <b>{w['amount']:.2f} ⭐</b>."
    )
    try:
        await call.bot.send_message(
            w["user_id"],
            f"❌ Заявка на вывод <b>#{w_id}</b> отклонена администратором.\n"
            f"На ваш баланс возвращено <b>{w['amount']:.2f} ⭐</b>.",
        )
    except Exception:
        pass
    await call.answer("Отклонено")


@dp.message(F.web_app_data)
async def handle_web_app_data(message: Message) -> None:
    user = message.from_user
    if not user or not message.web_app_data:
        return

    try:
        payload = json.loads(message.web_app_data.data)
    except json.JSONDecodeError:
        return

    if payload.get("action") == "withdraw":
        amount = round(float(payload.get("amount", 0)), 2)
        fee = round(amount * 0.15, 2)
        receive_amount = round(amount - fee, 2)
        if amount >= 15:
            w_id = create_withdrawal(
                user_id=user.id,
                username=user.username or "",
                first_name=user.first_name or "Игрок",
                amount=amount,
                fee=fee,
                receive_amount=receive_amount,
            )
            await message.answer(
                f"📤 Заявка на вывод <b>#{w_id}</b> создана!\n"
                f"• Сумма: <b>{amount:.2f} ⭐</b>\n"
                f"• Комиссия 15%: <b>{fee:.2f} ⭐</b>\n"
                f"• К получению: <b>{receive_amount:.2f} ⭐</b>"
            )
            for adm_id in ADMIN_IDS:
                try:
                    await message.bot.send_message(
                        adm_id,
                        f"📤 <b>Новая заявка на вывод #{w_id}</b> от @{user.username or user.id}: "
                        f"к получению <b>{receive_amount:.2f} ⭐</b>",
                    )
                except Exception:
                    pass
        return

    payload["user_id"] = user.id
    payload["username"] = user.username or ""
    payload["first_name"] = user.first_name or "Игрок"
    save_user_state(user.id, payload)


async def main() -> None:
    global bot_instance
    init_db()
    bot_instance = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await bot_instance.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Играть",
                web_app=WebAppInfo(url=build_webapp_url()),
            )
        )
    except Exception:
        pass

    runner = await start_api_server()
    try:
        logger.info("Бот STARGRADER запущен...")
        await dp.start_polling(bot_instance)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
