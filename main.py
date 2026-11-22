import asyncio
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from decimal import Decimal

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel

# --- НАСТРОЙКИ ---
MY_WALLET_ADDRESS = os.getenv(
    "MY_WALLET_ADDRESS",
    "UQBfYcTB3QStbmeE-uVOVKqz20qPeLSe_lDRPp3UFhs-7KYo",
)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
TONAPI_KEY = os.getenv("TONAPI_KEY", "")

USDT_MASTER = "0:b113a994b5024a16719f69139328eb759596c38a25f59028b146fecdc3621dfe"
TONAPI_BASE = "https://tonapi.io/v2"
DB_PATH = os.getenv("DB_PATH", "pickaxe_empire.db")
ORDER_TTL_SECONDS = 900
BASE_TOKENS_PER_SEC = 2.0

BOOST_PACKAGES = {
    "starter_ton": {"title": "Starter Pickaxe", "asset": "TON", "amount": Decimal("1.0"), "tokens": 2500, "mining_mult": 2},
    "pro_usdt": {"title": "Pro Drill USDT", "asset": "USDT", "amount": Decimal("5.0"), "tokens": 15000, "mining_mult": 5},
    "titan_ton": {"title": "Titan Rig TON", "asset": "TON", "amount": Decimal("4.5"), "tokens": 18000, "mining_mult": 6},
}

# --- БАЗА ДАННЫХ ---
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                referrer_id INTEGER,
                tokens INTEGER NOT NULL DEFAULT 0,
                unclaimed_tokens REAL NOT NULL DEFAULT 0,
                mining_multiplier INTEGER NOT NULL DEFAULT 1,
                purchases INTEGER NOT NULL DEFAULT 0,
                last_tick INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                package_id TEXT NOT NULL,
                asset TEXT NOT NULL,
                amount TEXT NOT NULL,
                memo TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                tx_event_id TEXT UNIQUE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            """
        )


init_db()

# --- TELEGRAM БОТ ---
async def run_telegram_bot() -> None:
    if not BOT_TOKEN:
        print("ВНИМАНИЕ: BOT_TOKEN не задан, бот не запущен.")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        args = (message.text or "").split(maxsplit=1)
        start_param = args[1].strip() if len(args) > 1 else ""
        base_url = MINI_APP_URL.rstrip("/")
        app_url = f"{base_url}?tgWebAppStartParam={start_param}" if start_param else base_url
        ref_link = f"https://t.me/{BOT_USERNAME}/app?startapp=r_{message.from_user.id}"

        buttons = []
        if base_url.startswith("https://"):
            buttons.append([InlineKeyboardButton(text="⛏️ Открыть Pickaxe Boost", web_app=WebAppInfo(url=app_url))])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await message.answer(
            "Добро пожаловать в <b>Pickaxe Boost</b>!\n\n"
            "Подключай кошелёк в приложении, покупай бусты в TON или USDT и ускоряй добычу.\n\n"
            f"Твоя реферальная ссылка:\n<code>{ref_link}</code>",
            parse_mode="HTML",
            reply_markup=kb,
        )

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_task = asyncio.create_task(run_telegram_bot())
    yield
    bot_task.cancel()


app = FastAPI(title="Pickaxe Boost Backend + Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def sync_mining_progress(conn: sqlite3.Connection, user_row: sqlite3.Row, now: int) -> dict:
    elapsed = max(0, now - int(user_row["last_tick"]))
    rate_per_sec = BASE_TOKENS_PER_SEC * int(user_row["mining_multiplier"])
    earned = round(elapsed * rate_per_sec, 2)
    new_unclaimed = round(float(user_row["unclaimed_tokens"]) + earned, 2)

    conn.execute(
        "UPDATE users SET unclaimed_tokens = ?, last_tick = ? WHERE user_id = ?",
        (new_unclaimed, now, user_row["user_id"]),
    )
    updated = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_row["user_id"],)).fetchone()
    data = dict(updated)
    data["rate_per_sec"] = rate_per_sec
    return data


def ensure_user(conn: sqlite3.Connection, user_id: int, username: str = "", referrer_id: int | None = None) -> dict:
    now = int(time.time())
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        valid_ref = referrer_id if (referrer_id and referrer_id != user_id) else None
        conn.execute(
            """
            INSERT INTO users (user_id, username, referrer_id, tokens, unclaimed_tokens, mining_multiplier, purchases, last_tick)
            VALUES (?, ?, ?, 250, 0, 1, 0, ?)
            """,
            (user_id, username, valid_ref, now),
        )
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    elif username and row["username"] != username:
        conn.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return sync_mining_progress(conn, row, now)


class SessionRequest(BaseModel):
    user_id: int
    username: str = ""
    referrer_id: int | None = None


class CreateOrderRequest(BaseModel):
    user_id: int
    package_id: str


class VerifyOrderRequest(BaseModel):
    user_id: int


def normalize_addr(addr: str | None) -> str:
    return (addr or "").strip().lower()


def extract_jetton_comment(action: dict, info: dict) -> str:
    comment = (info.get("comment") or "").strip()
    if comment:
        return comment
    return ((action.get("simple_preview") or {}).get("description") or "").strip()


async def find_matching_transfer(
    expected_to: str,
    asset: str,
    min_amount: Decimal,
    required_memo: str,
    used_event_ids: set[str],
) -> str | None:
    headers = {"Accept": "application/json"}
    if TONAPI_KEY:
        headers["Authorization"] = f"Bearer {TONAPI_KEY}"

    url = f"{TONAPI_BASE}/accounts/{expected_to}/events"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params={"limit": 40}, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    target_addr = normalize_addr(expected_to)
    memo_clean = required_memo.strip()

    for event in data.get("events", []):
        event_id = event.get("event_id")
        if not event_id or event_id in used_event_ids:
            continue

        for action in event.get("actions", []):
            action_type = action.get("type")

            if asset == "TON" and action_type == "TonTransfer":
                info = action.get("TonTransfer", {})
                recipient = normalize_addr((info.get("recipient") or {}).get("address"))
                comment = (info.get("comment") or "").strip()
                amount_ton = Decimal(str(info.get("amount", 0))) / Decimal("1000000000")

                if recipient == target_addr and amount_ton >= min_amount and comment == memo_clean:
                    return event_id

            if asset == "USDT" and action_type == "JettonTransfer":
                info = action.get("JettonTransfer", {})
                recipient = normalize_addr((info.get("recipient") or {}).get("address"))
                jetton_master = normalize_addr((info.get("jetton") or {}).get("address"))
                comment = extract_jetton_comment(action, info)
                amount_usdt = Decimal(str(info.get("amount", 0))) / Decimal("1000000")

                if (
                    recipient == target_addr
                    and jetton_master == normalize_addr(USDT_MASTER)
                    and amount_usdt >= min_amount
                    and memo_clean in comment
                ):
                    return event_id

    return None


@app.get("/")
def healthcheck():
    return {"status": "ok", "service": "Pickaxe Boost API"}


@app.post("/api/session")
def get_session(req: SessionRequest):
    with get_db() as conn:
        user = ensure_user(conn, req.user_id, req.username, req.referrer_id)
    return {"user": user, "wallet": MY_WALLET_ADDRESS}


@app.post("/api/claim")
def claim_mined(req: VerifyOrderRequest):
    now = int(time.time())
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (req.user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Игрок не найден")
        synced = sync_mining_progress(conn, row, now)
        claimable = int(synced["unclaimed_tokens"])
        if claimable <= 0:
            return {"claimed": 0, "user": synced}

        remainder = round(float(synced["unclaimed_tokens"]) - claimable, 2)
        conn.execute(
            "UPDATE users SET tokens = tokens + ?, unclaimed_tokens = ? WHERE user_id = ?",
            (claimable, remainder, req.user_id),
        )
        final_row = conn.execute("SELECT * FROM users WHERE user_id = ?", (req.user_id,)).fetchone()
        user_data = dict(final_row)
        user_data["rate_per_sec"] = BASE_TOKENS_PER_SEC * int(final_row["mining_multiplier"])

    return {"claimed": claimable, "user": user_data}


@app.post("/api/orders")
def create_order(req: CreateOrderRequest):
    pkg = BOOST_PACKAGES.get(req.package_id)
    if not pkg:
        raise HTTPException(status_code=400, detail="Неизвестный пакет буста")

    now = int(time.time())
    expires_at = now + ORDER_TTL_SECONDS
    order_id = f"ord_{secrets.token_hex(8)}"
    memo = f"px_{secrets.token_hex(6)}"

    with get_db() as conn:
        ensure_user(conn, req.user_id)
        conn.execute(
            """
            INSERT INTO orders (order_id, user_id, package_id, asset, amount, memo, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (order_id, req.user_id, req.package_id, pkg["asset"], str(pkg["amount"]), memo, now, expires_at),
        )

    return {
        "order_id": order_id,
        "recipient": MY_WALLET_ADDRESS,
        "asset": pkg["asset"],
        "amount": str(pkg["amount"]),
        "memo": memo,
        "expires_at": expires_at,
    }


@app.post("/api/orders/{order_id}/verify")
async def verify_order(order_id: str, req: VerifyOrderRequest):
    now = int(time.time())

    with get_db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Заказ не найден")
        if order["user_id"] != req.user_id:
            raise HTTPException(status_code=403, detail="Заказ принадлежит другому игроку")

        if order["status"] == "paid":
            user_row = conn.execute("SELECT * FROM users WHERE user_id = ?", (req.user_id,)).fetchone()
            user_data = sync_mining_progress(conn, user_row, now)
            return {"status": "already_paid", "event_id": order["tx_event_id"], "user": user_data}

        if now > order["expires_at"]:
            conn.execute("UPDATE orders SET status = 'expired' WHERE order_id = ?", (order_id,))
            raise HTTPException(status_code=400, detail="Время оплаты заказа истекло. Создайте новый заказ.")

        used_rows = conn.execute("SELECT tx_event_id FROM orders WHERE tx_event_id IS NOT NULL").fetchall()
        used_event_ids = {r["tx_event_id"] for r in used_rows}

    try:
        event_id = await find_matching_transfer(
            expected_to=MY_WALLET_ADDRESS,
            asset=order["asset"],
            min_amount=Decimal(order["amount"]),
            required_memo=order["memo"],
            used_event_ids=used_event_ids,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка TonAPI: {exc}") from exc

    if not event_id:
        raise HTTPException(
            status_code=404,
            detail="Перевод пока не найден в блокчейне. Подождите 3–5 секунд и попробуйте снова.",
        )

    pkg = BOOST_PACKAGES[order["package_id"]]

    with get_db() as conn:
        cur = conn.execute(
            "UPDATE orders SET status = 'paid', tx_event_id = ? WHERE order_id = ? AND status = 'pending'",
            (event_id, order_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail="Заказ уже обработан")

        buyer_row = conn.execute("SELECT * FROM users WHERE user_id = ?", (req.user_id,)).fetchone()
        sync_mining_progress(conn, buyer_row, now)

        conn.execute(
            """
            UPDATE users
            SET tokens = tokens + ?,
                mining_multiplier = MAX(mining_multiplier, ?),
                purchases = purchases + 1
            WHERE user_id = ?
            """,
            (pkg["tokens"], pkg["mining_mult"], req.user_id),
        )

        ref_id = buyer_row["referrer_id"]
        if ref_id:
            ref_bonus = max(1, int(pkg["tokens"] * 0.10))
            conn.execute("UPDATE users SET tokens = tokens + ? WHERE user_id = ?", (ref_bonus, ref_id))

        updated_buyer = conn.execute("SELECT * FROM users WHERE user_id = ?", (req.user_id,)).fetchone()
        user_data = dict(updated_buyer)
        user_data["rate_per_sec"] = BASE_TOKENS_PER_SEC * int(updated_buyer["mining_multiplier"])

    return {
        "status": "ok",
        "event_id": event_id,
        "credited": {"tokens": pkg["tokens"], "mining_multiplier": pkg["mining_mult"]},
        "user": user_data,
  }
  
