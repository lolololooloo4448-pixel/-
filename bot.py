"""
🎭 Анонім-чат бот — ФІНАЛЬНА РОБОЧА ВЕРСІЯ.

Ключові виправлення:
• Жодних bot: Bot у параметрах хендлерів (тільки message.bot)
• Жодних UserState у декораторах
• Жодних | в фільтрах
• Кожна кнопка — свій хендлер
• Логування кожного кроку для діагностики
"""

import asyncio
import logging
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не знайдено!")

CHANNEL_URL = "посилання на ваший канал"
CHANNEL_NAME = "юз вашого тг канада"
DB_PATH = os.getenv("DB_PATH", "bot.db")

AD_EVERY_N_MESSAGES = 15
AD_EVERY_N_MESSAGES_DEFAULT = 20
REPORT_BUFFER_SIZE = 30
AUTO_BAN_REPORTS = 3
ANTISPAM_WINDOW = 5
ANTISPAM_MAX_MSG = 8

ADMIN_IDS: list[int] = [АЙДИ вашого профілю]
LOG_CHAT_IDS: list[int] = ADMIN_IDS.copy()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


BOOST_PRICES = {1: 50, 3: 120, 7: 250, 30: 800}
PREMIUM_PRICE = 200
NICKNAME_PRICE = 150
PREMIUM_NICKNAME_PRICE = 50
NICKNAME_COOLDOWN_DAYS = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)
router = Router()


class ChatMode(Enum):
    NORMAL = auto()
    FLIRTY = auto()


class UserState(Enum):
    IDLE = auto()
    SEARCHING = auto()
    CHATTING = auto()
    SUPPORT = auto()
    ENTERING_NICKNAME = auto()


class Interest(Enum):
    FRIENDSHIP = "🤝 Дружба"
    RELATIONSHIP = "❤️ Отношения"
    FLIRTY = "🔥 Пошлості"
    TALK = "💬 Поговорити"
    GAMES = "🎮 Ігри"
    MUSIC = "🎵 Музика"
    MOVIES = "🎬 Кіно"
    SPORT = "⚽ Спорт"


@dataclass
class ChatMessage:
    sender_id: int
    chat_id: int
    message_id: int


@dataclass
class ChatRoom:
    user_a: int
    user_b: int
    nickname_a: str
    nickname_b: str
    mode: ChatMode = ChatMode.NORMAL
    history: deque = field(default_factory=lambda: deque(maxlen=REPORT_BUFFER_SIZE))

    def partner_of(self, user_id: int) -> int:
        return self.user_b if user_id == self.user_a else self.user_a

    def nickname_of(self, user_id: int) -> str:
        return self.nickname_a if user_id == self.user_a else self.nickname_b


@dataclass
class UserSession:
    user_id: int
    state: UserState = UserState.IDLE
    partner_id: int | None = None
    last_partner_id: int | None = None
    mode: ChatMode = ChatMode.NORMAL
    messages_in_chat: int = 0
    reputation: int = 50
    likes: int = 0
    dislikes: int = 0
    total_chats: int = 0
    is_banned: bool = False
    flirty_warned: bool = False
    interests: list[str] = field(default_factory=list)
    messages_since_ad: int = 0
    ads_seen: int = 0
    ads_clicked: int = 0
    is_premium: bool = False
    premium_until: str | None = None
    custom_nickname: str | None = None
    nickname_last_changed: str | None = None
    boost_until: str | None = None
    _spam_times: deque = field(default_factory=lambda: deque(maxlen=ANTISPAM_MAX_MSG))

    def spam_ok(self) -> bool:
        now = time.time()
        while self._spam_times and now - self._spam_times[0] > ANTISPAM_WINDOW:
            self._spam_times.popleft()
        if len(self._spam_times) >= ANTISPAM_MAX_MSG:
            return False
        self._spam_times.append(now)
        return True


sessions: dict[int, UserSession] = {}
search_queue_normal: list[int] = []
search_queue_flirty: list[int] = []
queue_lock = asyncio.Lock()
active_rooms: dict[int, ChatRoom] = {}
support_reply_map: dict[int, int] = {}


def get_session(user_id: int) -> UserSession:
    if user_id not in sessions:
        sessions[user_id] = UserSession(user_id=user_id)
    return sessions[user_id]


BAD_WORDS_PATTERN = re.compile(
    r"\b(хуй|пизд|бля|еба|сука|fuck|shit|bitch)\w*", re.IGNORECASE,
)
NICKNAME_VALID_PATTERN = re.compile(r"^[A-Za-zА-Яа-яЇїІіЄєҐґ0-9 _\-]{2,20}$")
DIVIDER = "━━━━━━━━━━━━━━━━━━━━"
DIVIDER_SHORT = "─────────────"


# ===========================================================================
# БД
# ===========================================================================

async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, reputation INTEGER DEFAULT 50,
            likes INTEGER DEFAULT 0, dislikes INTEGER DEFAULT 0,
            total_chats INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS interests (
            user_id INTEGER, interest TEXT, PRIMARY KEY (user_id, interest)
        );
        CREATE TABLE IF NOT EXISTS blocks (
            user_id INTEGER, blocked_id INTEGER, PRIMARY KEY (user_id, blocked_id)
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER,
            target_id INTEGER, category TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ad_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            text TEXT NOT NULL, button_text TEXT, button_url TEXT,
            target_mode TEXT DEFAULT 'any', target_interests TEXT DEFAULT '',
            max_impressions INTEGER DEFAULT 0, impressions INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS top_boosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            stars_paid INTEGER NOT NULL, duration_days INTEGER NOT NULL,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL, impressions INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1, payment_id TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            payment_type TEXT NOT NULL, stars_paid INTEGER NOT NULL,
            payload TEXT, payment_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        async with db.execute("PRAGMA table_info(users)") as cur:
            existing_cols = {row[1] for row in await cur.fetchall()}
        needed_cols = [
            ("is_premium", "ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0"),
            ("premium_until", "ALTER TABLE users ADD COLUMN premium_until TIMESTAMP"),
            ("custom_nickname", "ALTER TABLE users ADD COLUMN custom_nickname TEXT"),
            ("nickname_last_changed", "ALTER TABLE users ADD COLUMN nickname_last_changed TIMESTAMP"),
        ]
        for col_name, sql in needed_cols:
            if col_name not in existing_cols:
                try:
                    await db.execute(sql)
                except Exception:
                    pass
        await db.executescript("""
        CREATE INDEX IF NOT EXISTS idx_boosts_active
            ON top_boosts(user_id, is_active, expires_at);
        CREATE INDEX IF NOT EXISTS idx_premium
            ON users(is_premium, premium_until);
        """)
        await db.commit()
        logger.info("✅ БД ініціалізована")


async def db_load_session(user_id: int) -> UserSession:
    s = get_session(user_id)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """SELECT reputation, likes, dislikes, total_chats, is_banned,
                          is_premium, premium_until, custom_nickname, nickname_last_changed
                   FROM users WHERE user_id=?""",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    (s.reputation, s.likes, s.dislikes, s.total_chats, banned,
                     prem, prem_until, nick, nick_changed) = row
                    s.is_banned = bool(banned)
                    s.is_premium = bool(prem)
                    s.premium_until = prem_until
                    s.custom_nickname = nick
                    s.nickname_last_changed = nick_changed
            async with db.execute(
                "SELECT interest FROM interests WHERE user_id=?", (user_id,)
            ) as cur:
                s.interests = [r[0] for r in await cur.fetchall()]
    except Exception as e:
        logger.warning("db_load_session: %s", e)
    return s


async def db_save_session(s: UserSession) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO users
                   (user_id, reputation, likes, dislikes, total_chats, is_banned,
                    is_premium, premium_until, custom_nickname, nickname_last_changed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     reputation=excluded.reputation, likes=excluded.likes,
                     dislikes=excluded.dislikes, total_chats=excluded.total_chats,
                     is_banned=excluded.is_banned, is_premium=excluded.is_premium,
                     premium_until=excluded.premium_until,
                     custom_nickname=excluded.custom_nickname,
                     nickname_last_changed=excluded.nickname_last_changed""",
                (s.user_id, s.reputation, s.likes, s.dislikes, s.total_chats,
                 int(s.is_banned), int(s.is_premium), s.premium_until,
                 s.custom_nickname, s.nickname_last_changed),
            )
            await db.commit()
    except Exception as e:
        logger.warning("db_save_session: %s", e)


async def db_save_interests(user_id: int, interests: list[str]) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM interests WHERE user_id=?", (user_id,))
            for i in interests:
                await db.execute(
                    "INSERT OR IGNORE INTO interests (user_id, interest) VALUES (?, ?)",
                    (user_id, i))
            await db.commit()
    except Exception as e:
        logger.warning("db_save_interests: %s", e)


async def db_is_blocked(user_id: int, blocked_id: int) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM blocks WHERE user_id=? AND blocked_id=?",
                (user_id, blocked_id)) as cur:
                return await cur.fetchone() is not None
    except Exception:
        return False


async def db_block(user_id: int, blocked_id: int) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO blocks (user_id, blocked_id) VALUES (?, ?)",
                (user_id, blocked_id))
            await db.commit()
    except Exception as e:
        logger.warning("db_block: %s", e)


async def db_add_report(reporter_id: int, target_id: int) -> int:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO reports (reporter_id, target_id, category) VALUES (?, ?, 'chat')",
                (reporter_id, target_id))
            await db.commit()
            async with db.execute(
                "SELECT COUNT(*) FROM reports WHERE target_id=?", (target_id,)) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0
    except Exception:
        return 0


async def db_is_user_boosted(user_id: int) -> dict | None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM top_boosts WHERE user_id=? AND is_active=1
                   AND expires_at > datetime('now')
                   ORDER BY expires_at DESC LIMIT 1""", (user_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
    except Exception:
        return None


async def db_get_active_boosts(limit: int = 10) -> list[dict]:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM top_boosts WHERE is_active=1
                   AND expires_at > datetime('now')
                   ORDER BY started_at DESC LIMIT ?""", (limit,)) as cur:
                return [dict(r) for r in await cur.fetchall()]
    except Exception:
        return []


async def db_boost_impression(boost_id: int) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE top_boosts SET impressions = impressions + 1 WHERE id=?",
                (boost_id,))
            await db.commit()
    except Exception:
        pass


async def db_deactivate_expired_boosts() -> int:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """UPDATE top_boosts SET is_active=0
                   WHERE is_active=1 AND expires_at <= datetime('now')""")
            await db.commit()
            return cur.rowcount
    except Exception:
        return 0


async def db_create_boost(user_id: int, duration_days: int,
                          stars_paid: int, payment_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE top_boosts SET is_active=0 WHERE user_id=? AND is_active=1",
            (user_id,))
        cur = await db.execute(
            """INSERT INTO top_boosts
               (user_id, stars_paid, duration_days, expires_at, payment_id)
               VALUES (?, ?, ?, datetime('now', '+' || ? || ' days'), ?)""",
            (user_id, stars_paid, duration_days, duration_days, payment_id))
        await db.commit()
        return cur.lastrowid


async def db_log_payment(user_id, payment_type, stars_paid, payload, payment_id) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO payments
                   (user_id, payment_type, stars_paid, payload, payment_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, payment_type, stars_paid, payload, payment_id))
            await db.commit()
    except Exception as e:
        logger.warning("db_log_payment: %s", e)


async def db_get_campaign(campaign_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM ad_campaigns WHERE id=?", (campaign_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def db_list_campaigns() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM ad_campaigns ORDER BY id DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def db_add_campaign(title, text, button_text=None, button_url=None,
                          target_mode="any", target_interests="", max_impressions=0) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO ad_campaigns
               (title, text, button_text, button_url, target_mode,
                target_interests, max_impressions)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (title, text, button_text, button_url, target_mode,
             target_interests, max_impressions))
        await db.commit()
        return cur.lastrowid


async def db_set_campaign_active(campaign_id: int, active: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE ad_campaigns SET is_active=? WHERE id=?",
                         (int(active), campaign_id))
        await db.commit()


async def db_delete_campaign(campaign_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM ad_campaigns WHERE id=?", (campaign_id,))
        await db.commit()


async def db_pick_ad_for_user(mode: ChatMode, user_interests: list[str]) -> dict | None:
    try:
        mode_str = "flirty" if mode == ChatMode.FLIRTY else "normal"
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM ad_campaigns WHERE is_active=1
                   AND (expires_at IS NULL OR expires_at > datetime('now'))
                   AND (max_impressions = 0 OR impressions < max_impressions)
                   AND (target_mode = 'any' OR target_mode = ?)
                   ORDER BY RANDOM()""", (mode_str,)) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return None
        user_set = set(user_interests)
        matching = []
        for ad in rows:
            target = ad.get("target_interests") or ""
            if not target.strip():
                matching.append(ad)
                continue
            targets = {t.strip() for t in target.split(",") if t.strip()}
            if targets & user_set:
                matching.append(ad)
        return random.choice(matching) if matching else None
    except Exception:
        return None


async def db_track_impression(campaign_id: int) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE ad_campaigns SET impressions = impressions + 1 WHERE id=?",
                (campaign_id,))
            await db.commit()
    except Exception:
        pass


async def db_track_click(campaign_id: int) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE ad_campaigns SET clicks = clicks + 1 WHERE id=?",
                             (campaign_id,))
            await db.commit()
    except Exception:
        pass


async def db_boost_stats() -> dict:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """SELECT COUNT(*), COALESCE(SUM(stars_paid),0)
                   FROM top_boosts WHERE is_active=1
                     AND expires_at > datetime('now')""") as cur:
                active_count, active_stars = await cur.fetchone()
            async with db.execute(
                "SELECT COUNT(*), COALESCE(SUM(stars_paid),0) FROM top_boosts") as cur:
                total_count, total_stars = await cur.fetchone()
        return {"active_count": active_count, "active_stars": active_stars,
                "total_count": total_count, "total_stars": total_stars}
    except Exception:
        return {"active_count": 0, "active_stars": 0,
                "total_count": 0, "total_stars": 0}


async def db_payment_stats() -> dict:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """SELECT payment_type, COUNT(*), COALESCE(SUM(stars_paid),0)
                   FROM payments GROUP BY payment_type""") as cur:
                rows = await cur.fetchall()
            result = {}
            for ptype, count, stars in rows:
                result[ptype] = {"count": count, "stars": stars}
            async with db.execute(
                "SELECT COUNT(*), COALESCE(SUM(stars_paid),0) FROM payments") as cur:
                total_count, total_stars = await cur.fetchone()
            result["_total"] = {"count": total_count, "stars": total_stars}
            return result
    except Exception:
        return {"_total": {"count": 0, "stars": 0}}


# ===========================================================================
# Кампанії
# ===========================================================================

DEFAULT_CAMPAIGNS = [
    {
        "title": "🤖 Yourbdai_bot",
        "text": (
            "🤖 <b>Спробуй @Yourbdai_bot!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔥 Крутий бот!\n"
            "🎁 Бонус для нових\n"
            "⚡️ Швидко, зручно, безкоштовно\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👇 Тисни кнопку"
        ),
        "button_text": "🚀 Відкрити @Yourbdai_bot",
        "button_url": "https://t.me/Yourbdai_bot?start=_tgr_k0EphPsxOTEy",
        "target_mode": "any", "target_interests": "", "max_impressions": 0,
    },
]


async def db_ensure_default_campaigns() -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            for camp in DEFAULT_CAMPAIGNS:
                async with db.execute(
                    "SELECT id FROM ad_campaigns WHERE button_url=?",
                    (camp["button_url"],)) as cur:
                    if await cur.fetchone():
                        continue
                await db.execute(
                    """INSERT INTO ad_campaigns
                       (title, text, button_text, button_url, target_mode,
                        target_interests, max_impressions)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (camp["title"], camp["text"], camp["button_text"],
                     camp["button_url"], camp["target_mode"],
                     camp["target_interests"], camp["max_impressions"]))
            await db.commit()
    except Exception as e:
        logger.warning("db_ensure_default_campaigns: %s", e)


# ===========================================================================
# Клавіатури
# ===========================================================================

def kb_idle() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💬 Звичайний чат")],
            [KeyboardButton(text="🔥 Пошлий чат 18+")],
            [KeyboardButton(text="🎯 Інтереси"), KeyboardButton(text="👤 Профіль")],
            [KeyboardButton(text="🏆 Топ"), KeyboardButton(text="🚀 Буст у топі")],
            [KeyboardButton(text="⭐️ Преміум"), KeyboardButton(text="🏷 Кастомний нік")],
            [KeyboardButton(text="📢 Канал"), KeyboardButton(text="🛠 Підтримка")],
            [KeyboardButton(text="ℹ️ Довідка")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Обери дію нижче 👇",
    )


def kb_searching(mode: ChatMode) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text="👥 Черга")]]
    if mode == ChatMode.FLIRTY:
        rows.append([KeyboardButton(text="💬 Звичайний чат")])
    else:
        rows.append([KeyboardButton(text="🔥 Пошлий чат 18+")])
    rows.append([KeyboardButton(text="❌ Скасувати")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb_chatting() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏭ Наступний"), KeyboardButton(text="🚫 Завершити")],
            [KeyboardButton(text="⚠️ Скарга"), KeyboardButton(text="🚷 Блокувати")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Пиши повідомлення...",
    )


def kb_support() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Вийти з підтримки")]],
        resize_keyboard=True)


def kb_report_confirm() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Надіслати скаргу")],
                  [KeyboardButton(text="◀️ Скасувати")]],
        resize_keyboard=True)


def kb_nickname_cancel() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Скасувати зміну ніка")]],
        resize_keyboard=True,
        input_field_placeholder="Введи новий нік (2-20 символів)")


def inline_channel_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text=f"📢 Підписатись на {CHANNEL_NAME}", url=CHANNEL_URL)]])


def kb_rate_partner() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 Класний", callback_data="rate:like"),
         InlineKeyboardButton(text="👎 Не сподобався", callback_data="rate:dislike")],
        [InlineKeyboardButton(text="⏭ Пропустити", callback_data="rate:skip")],
    ])


def kb_interests(selected: set[str]) -> InlineKeyboardMarkup:
    rows, row = [], []
    for interest in Interest:
        mark = "✅" if interest.value in selected else "⬜"
        row.append(InlineKeyboardButton(
            text=f"{mark} {interest.value}",
            callback_data=f"interest:{interest.name}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="💾 Зберегти", callback_data="interest:save")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_boost_menu() -> InlineKeyboardMarkup:
    rows = []
    for days, stars in BOOST_PRICES.items():
        label = f"🚀 {days} дн. — {stars} ⭐️" if days > 1 else f"🚀 1 день — {stars} ⭐️"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"boost:buy:{days}")])
    rows.append([InlineKeyboardButton(text="📊 Мої бусти", callback_data="boost:history")])
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="boost:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_premium_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"⭐️ Підписка на місяць — {PREMIUM_PRICE} ⭐️",
            callback_data="premium:buy")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="premium:cancel")],
    ])


def kb_nickname_menu(has_premium: bool) -> InlineKeyboardMarkup:
    price = PREMIUM_NICKNAME_PRICE if has_premium else NICKNAME_PRICE
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🏷 Змінити нік — {price} ⭐️",
                              callback_data="nick:buy")],
        [InlineKeyboardButton(text="🗑 Скинути нік", callback_data="nick:reset")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="nick:cancel")],
    ])


# ===========================================================================
# Дизайн
# ===========================================================================

def random_nickname() -> str:
    return f"Незнайомець #{random.randint(1000, 9999)}"


def mode_title(mode: ChatMode) -> str:
    return "🔥 Пошлий" if mode == ChatMode.FLIRTY else "💬 Звичайний"


def rep_bar(rep: int) -> str:
    filled = rep // 10
    return "▰" * filled + "▱" * (10 - filled)


def rep_stars(rep: int) -> str:
    n = min(5, rep // 20)
    return "⭐" * n + "☆" * (5 - n)


def rep_emoji(rep: int) -> str:
    if rep >= 80: return "💎"
    if rep >= 60: return "🌟"
    if rep >= 40: return "✨"
    if rep >= 20: return "🌱"
    return "🪨"


def rep_label(rep: int) -> str:
    if rep >= 80: return "Легенда"
    if rep >= 60: return "Дуже хороший"
    if rep >= 40: return "Нормальний"
    if rep >= 20: return "Новенький"
    return "Підозрілий"


# ===========================================================================
# Допоміжні функції
# ===========================================================================

def _pick_queue(mode: ChatMode) -> list[int]:
    return search_queue_flirty if mode == ChatMode.FLIRTY else search_queue_normal


async def start_search(bot, user_id: int, mode: ChatMode | None = None) -> None:
    """Запускає пошук співрозмовника."""
    logger.info("🚀 start_search user=%s mode=%s", user_id, mode)
    session = get_session(user_id)

    if session.is_banned:
        logger.info("🚀 user banned")
        await bot.send_message(user_id, "🚫 <b>Ти забанений</b>")
        return

    if mode is None:
        mode = session.mode
    session.mode = mode

    async with queue_lock:
        logger.info("🚀 в queue_lock")
        if user_id in search_queue_normal:
            search_queue_normal.remove(user_id)
        if user_id in search_queue_flirty:
            search_queue_flirty.remove(user_id)

        queue = _pick_queue(mode)
        best_match = None
        best_score = -1

        for candidate_id in queue:
            if candidate_id == user_id:
                continue
            if await db_is_blocked(user_id, candidate_id):
                continue
            if await db_is_blocked(candidate_id, user_id):
                continue
            candidate = get_session(candidate_id)
            if candidate.is_banned:
                continue
            score = len(set(session.interests) & set(candidate.interests))
            if candidate.is_premium:
                score += 2
            if score > best_score:
                best_score = score
                best_match = candidate_id

        if best_match is not None:
            queue.remove(best_match)
            logger.info("🚀 знайдено пару: %s", best_match)
            await _create_room(bot, user_id, best_match, mode)
            return

        session.state = UserState.SEARCHING
        session.partner_id = None
        queue.append(user_id)
        queue_size = len(queue)
        logger.info("🚀 додано в чергу, size=%d", queue_size)

        if mode == ChatMode.FLIRTY:
            icon, title = "🔥", "Шукаємо партнера для флірту..."
        else:
            icon, title = "💬", "Шукаємо співрозмовника..."

        interests_line = ""
        if session.interests:
            interests_line = f"\n🎯 Твої інтереси: <b>{', '.join(session.interests)}</b>"
        premium_line = "\n⭐️ <i>Преміум: пріоритет</i>" if session.is_premium else ""

        logger.info("🚀 відправляю повідомлення про пошук")
        await bot.send_message(
            user_id,
            f"{icon} <b>{title}</b>\n{DIVIDER}\n"
            f"👥 У черзі: <b>{queue_size}</b>{interests_line}{premium_line}\n"
            f"{DIVIDER}\n⏳ Зачекай, будь ласка 🙌",
            reply_markup=kb_searching(mode))
        logger.info("🚀 повідомлення відправлено ✅")


async def stop_search(bot, user_id: int) -> None:
    async with queue_lock:
        if user_id in search_queue_normal:
            search_queue_normal.remove(user_id)
        if user_id in search_queue_flirty:
            search_queue_flirty.remove(user_id)
    session = get_session(user_id)
    session.state = UserState.IDLE
    await bot.send_message(user_id, "❌ <b>Пошук скасовано</b>", reply_markup=kb_idle())


async def end_chat(bot, user_id: int, notify_partner: bool = True) -> None:
    session = get_session(user_id)
    partner_id = session.partner_id
    session.state = UserState.IDLE
    session.partner_id = None
    session.messages_in_chat = 0
    active_rooms.pop(user_id, None)

    if partner_id is not None:
        partner_session = get_session(partner_id)
        active_rooms.pop(partner_id, None)
        if partner_session.partner_id == user_id:
            partner_session.state = UserState.IDLE
            partner_session.partner_id = None
            partner_session.messages_in_chat = 0
            if notify_partner:
                try:
                    await bot.send_message(
                        partner_id,
                        f"❗️ <b>Співрозмовник завершив чат</b>\n{DIVIDER}\nОціни 👇",
                        reply_markup=kb_rate_partner())
                    await bot.send_message(
                        user_id,
                        f"👋 <b>Чат завершено</b>\n{DIVIDER}\nОціни 👇",
                        reply_markup=kb_rate_partner())
                except Exception as e:
                    logger.warning("Оцінка: %s", e)


async def _create_room(bot, user_id: int, partner_id: int, mode: ChatMode) -> None:
    session = get_session(user_id)
    partner_session = get_session(partner_id)
    nick_a = session.custom_nickname or random_nickname()
    nick_b = partner_session.custom_nickname or random_nickname()

    room = ChatRoom(user_a=user_id, user_b=partner_id,
                    nickname_a=nick_a, nickname_b=nick_b, mode=mode)
    active_rooms[user_id] = room
    active_rooms[partner_id] = room

    session.state = UserState.CHATTING
    session.partner_id = partner_id
    session.last_partner_id = partner_id
    session.messages_in_chat = 0
    session.total_chats += 1

    partner_session.state = UserState.CHATTING
    partner_session.partner_id = user_id
    partner_session.last_partner_id = user_id
    partner_session.messages_in_chat = 0
    partner_session.total_chats += 1

    await db_save_session(session)
    await db_save_session(partner_session)

    rep_a, rep_b = session.reputation, partner_session.reputation
    common = set(session.interests) & set(partner_session.interests)
    common_line = f"\n🎯 Спільне: <b>{', '.join(common)}</b>" if common else ""

    if mode == ChatMode.FLIRTY:
        icon, title = "🔥", "Знайдено партнера для флірту!"
        subtitle = "Розслабся та спілкуйся відверто 🙏"
    else:
        icon, title = "💬", "Співрозмовника знайдено!"
        subtitle = "Повна анонімність — без імен, фото, юзернеймів."

    def build_text(partner_nick, partner_rep, partner_premium):
        badge = "⭐️ " if partner_premium else ""
        return (f"{icon} <b>{title}</b>\n{DIVIDER}\n"
                f"🎭 Співрозмовник: {badge}<b>{partner_nick}</b>\n"
                f"{rep_emoji(partner_rep)} Репутація: <b>{partner_rep}</b>/100 "
                f"{rep_stars(partner_rep)}{common_line}\n{DIVIDER}\n"
                f"💡 {subtitle}\n\n👇 <i>Пиши повідомлення</i>")

    await bot.send_message(user_id,
                           build_text(room.nickname_b, rep_a, partner_session.is_premium),
                           reply_markup=kb_chatting())
    await bot.send_message(partner_id,
                           build_text(room.nickname_a, rep_b, session.is_premium),
                           reply_markup=kb_chatting())


async def show_ad_if_due(bot, message, session) -> None:
    if session.is_premium:
        return
    session.messages_since_ad += 1
    if session.messages_since_ad < AD_EVERY_N_MESSAGES_DEFAULT:
        return
    session.messages_since_ad = 0
    room = active_rooms.get(message.from_user.id)
    mode = room.mode if room else session.mode
    ad = await db_pick_ad_for_user(mode, session.interests)
    if not ad:
        return
    text = f"📢 <b>Реклама</b>\n{DIVIDER}\n{ad['text']}\n{DIVIDER_SHORT}\n<i>Від: {ad['title']}</i>"
    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=ad["button_text"], url=ad["button_url"])]
        ]) if ad.get("button_text") and ad.get("button_url") else None
        await bot.send_message(message.from_user.id, text, reply_markup=kb)
        await db_track_impression(ad["id"])
        session.ads_seen += 1
    except Exception as e:
        logger.warning("Реклама: %s", e)


async def log_message_to_admins(bot, message, *, mode, sender_id, sender_nick,
                                recipient_id, recipient_nick) -> None:
    if not LOG_CHAT_IDS:
        return
    header = (f"📩 <b>Чат [{mode_title(mode)}]</b>\n"
              f"👤 <b>{sender_nick}</b> <code>{sender_id}</code>\n"
              f"    ➡️ <b>{recipient_nick}</b> <code>{recipient_id}</code>")
    for admin_id in LOG_CHAT_IDS:
        try:
            await bot.send_message(admin_id, header)
            await bot.copy_message(chat_id=admin_id,
                                   from_chat_id=message.chat.id,
                                   message_id=message.message_id)
        except Exception as e:
            logger.warning("Лог: %s", e)


# ===========================================================================
# ХЕНДЛЕРИ
# ===========================================================================

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    logger.info("📩 /start user=%s", message.from_user.id)
    s = await db_load_session(message.from_user.id)
    if s.is_banned:
        await message.answer("🚫 <b>Ти забанений</b>")
        return
    prem = "⭐️ <b>Преміум активний</b>\n" if s.is_premium else ""
    nick = f"🏷 Нік: <b>{s.custom_nickname}</b>\n" if s.custom_nickname else ""
    text = (f"👋 <b>Вітаю в анонім-чаті!</b>\n{DIVIDER}\n{prem}{nick}"
            f"{rep_emoji(s.reputation)} <b>Репутація:</b> {s.reputation}/100 "
            f"{rep_stars(s.reputation)}\n"
            f"{rep_bar(s.reputation)} <b>{rep_label(s.reputation)}</b>\n{DIVIDER}\n"
            f"💬 <b>Звичайний чат</b> — будь-які теми\n"
            f"🔥 <b>Пошлий чат 18+</b> — флірт\n{DIVIDER}\n"
            f"⭐️ Преміум · 🚀 Буст · 🏷 Нік — у меню")
    await message.answer(text, reply_markup=kb_idle(), disable_web_page_preview=True)


# --- ЧАТ ---

@router.message(F.text == "💬 Звичайний чат")
async def btn_find_normal(message: Message) -> None:
    logger.info("📩 btn_find_normal user=%s", message.from_user.id)
    session = await db_load_session(message.from_user.id)
    logger.info("   state=%s is_banned=%s", session.state, session.is_banned)

    if session.is_banned:
        await message.answer("🚫 <b>Ти забанений</b>")
        return
    if session.state == UserState.CHATTING:
        await message.answer("💬 <b>Ти вже у чаті</b>\n\nНатисни 🚫 Завершити, щоб вийти.")
        return
    if session.state == UserState.SUPPORT:
        session.state = UserState.IDLE

    logger.info("   → start_search NORMAL")
    await start_search(message.bot, message.from_user.id, ChatMode.NORMAL)


@router.message(Command("find"))
async def cmd_find_normal(message: Message) -> None:
    logger.info("📩 /find user=%s", message.from_user.id)
    session = await db_load_session(message.from_user.id)
    if session.is_banned:
        await message.answer("🚫 <b>Ти забанений</b>")
        return
    if session.state == UserState.CHATTING:
        await message.answer("💬 <b>Ти вже у чаті</b>")
        return
    await start_search(message.bot, message.from_user.id, ChatMode.NORMAL)


@router.message(F.text == "🔥 Пошлий чат 18+")
async def btn_find_flirty(message: Message) -> None:
    logger.info("📩 btn_find_flirty user=%s", message.from_user.id)
    session = await db_load_session(message.from_user.id)
    if session.is_banned:
        await message.answer("🚫 <b>Ти забанений</b>")
        return
    if session.state == UserState.CHATTING:
        await message.answer("🔥 <b>Ти вже у чаті</b>")
        return
    if session.state == UserState.SUPPORT:
        session.state = UserState.IDLE

    if not session.flirty_warned:
        session.flirty_warned = True
        await message.answer(
            f"🔥 <b>Пошлий чат 18+</b>\n{DIVIDER}\n"
            f"Анонімний режим для флірту.\n\n"
            f"⚠️ Тільки 18+. Без цькування та погроз.\n\n"
            f"{DIVIDER_SHORT}\n👇 Натисни ще раз, щоб підтвердити")
        return
    await start_search(message.bot, message.from_user.id, ChatMode.FLIRTY)


@router.message(Command("flirty"))
async def cmd_find_flirty(message: Message) -> None:
    session = await db_load_session(message.from_user.id)
    if session.is_banned:
        await message.answer("🚫 <b>Ти забанений</b>")
        return
    if not session.flirty_warned:
        session.flirty_warned = True
        await message.answer(f"🔥 <b>Пошлий чат 18+</b>\n\nНатисни ще раз для підтвердження")
        return
    await start_search(message.bot, message.from_user.id, ChatMode.FLIRTY)


@router.message(F.text == "👥 Черга")
async def btn_queue(message: Message) -> None:
    session = get_session(message.from_user.id)
    if session.state != UserState.SEARCHING:
        await message.answer("Не в пошуку.", reply_markup=kb_idle())
        return
    if session.mode == ChatMode.FLIRTY:
        size, prefix = len(search_queue_flirty), "🔥 Пошла черга"
    else:
        size, prefix = len(search_queue_normal), "💬 Звичайна черга"
    await message.answer(f"👥 <b>{prefix}:</b> {size}")


@router.message(F.text == "❌ Скасувати")
async def btn_cancel_search(message: Message) -> None:
    await stop_search(message.bot, message.from_user.id)


@router.message(F.text == "⏭ Наступний")
async def btn_next(message: Message) -> None:
    session = get_session(message.from_user.id)
    mode = session.mode
    if session.state == UserState.CHATTING:
        await end_chat(message.bot, message.from_user.id)
    await start_search(message.bot, message.from_user.id, mode)


@router.message(Command("next"))
async def cmd_next(message: Message) -> None:
    session = get_session(message.from_user.id)
    mode = session.mode
    if session.state == UserState.CHATTING:
        await end_chat(message.bot, message.from_user.id)
    await start_search(message.bot, message.from_user.id, mode)


@router.message(F.text == "🚫 Завершити")
async def btn_stop(message: Message) -> None:
    session = get_session(message.from_user.id)
    if session.state == UserState.CHATTING:
        await end_chat(message.bot, message.from_user.id)
        await message.answer("✅ <b>Чат завершено</b>", reply_markup=kb_idle())
    elif session.state == UserState.SEARCHING:
        await stop_search(message.bot, message.from_user.id)
    else:
        await message.answer("🤷 Немає активного чату.", reply_markup=kb_idle())


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    session = get_session(message.from_user.id)
    if session.state == UserState.CHATTING:
        await end_chat(message.bot, message.from_user.id)
        await message.answer("✅ <b>Чат завершено</b>", reply_markup=kb_idle())
    elif session.state == UserState.SEARCHING:
        await stop_search(message.bot, message.from_user.id)
    else:
        await message.answer("🤷 Немає активного чату.", reply_markup=kb_idle())


# --- СКАРГИ ---

@router.message(F.text == "⚠️ Скарга")
async def btn_report(message: Message) -> None:
    session = get_session(message.from_user.id)
    if session.state != UserState.CHATTING:
        await message.answer("⚠️ Тільки під час чату.")
        return
    await message.answer(
        f"⚠️ <b>Поскаржитись?</b>\n{DIVIDER}\nАдміну надішлеться лог.",
        reply_markup=kb_report_confirm())


@router.message(F.text == "◀️ Скасувати")
async def btn_report_cancel(message: Message) -> None:
    session = get_session(message.from_user.id)
    if session.state == UserState.CHATTING:
        await message.answer("↩️ Скасовано.", reply_markup=kb_chatting())
    else:
        await message.answer("↩️ Скасовано.", reply_markup=kb_idle())


@router.message(F.text == "✅ Надіслати скаргу")
async def btn_report_send(message: Message) -> None:
    bot = message.bot
    reporter_id = message.from_user.id
    session = get_session(reporter_id)
    if session.state != UserState.CHATTING or reporter_id not in active_rooms:
        await message.answer("⚠️ Немає активного чату.", reply_markup=kb_idle())
        return
    room = active_rooms[reporter_id]
    partner_id = room.partner_of(reporter_id)
    total = await db_add_report(reporter_id, partner_id)
    header = (f"🚨 <b>НОВА СКАРГА</b>\n{DIVIDER}\n"
              f"🎭 Режим: {mode_title(room.mode)}\n"
              f"🔸 Скаржник: <code>{reporter_id}</code>\n"
              f"🔹 Відповідач: <code>{partner_id}</code>\n"
              f"📊 Скарг: <b>{total}</b>\n{DIVIDER}")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, header)
            for msg in room.history:
                label = "🔸" if msg.sender_id == reporter_id else "🔹"
                await bot.send_message(admin_id, label)
                await bot.copy_message(chat_id=admin_id,
                                       from_chat_id=msg.chat_id,
                                       message_id=msg.message_id)
        except Exception as e:
            logger.warning("Скарга: %s", e)
    if total >= AUTO_BAN_REPORTS:
        target = get_session(partner_id)
        target.is_banned = True
        await db_save_session(target)
    await message.answer("✅ <b>Скаргу надіслано</b>\n\nДякуємо! 🙏",
                         reply_markup=kb_chatting())


@router.message(F.text == "🚷 Блокувати")
async def btn_block(message: Message) -> None:
    session = get_session(message.from_user.id)
    if session.state != UserState.CHATTING or session.partner_id is None:
        await message.answer("Немає активного чату.", reply_markup=kb_idle())
        return
    partner_id = session.partner_id
    await db_block(message.from_user.id, partner_id)
    await end_chat(message.bot, message.from_user.id, notify_partner=True)
    await message.answer("🚷 <b>Заблоковано</b>", reply_markup=kb_idle())


# --- ІНТЕРЕСИ ---

@router.message(F.text == "🎯 Інтереси")
async def btn_interests(message: Message) -> None:
    s = await db_load_session(message.from_user.id)
    await message.answer(
        f"🎯 <b>Обери інтереси</b>\n{DIVIDER}\nБот шукатиме людей зі схожими темами.",
        reply_markup=kb_interests(set(s.interests)))


@router.message(Command("interests"))
async def cmd_interests(message: Message) -> None:
    s = await db_load_session(message.from_user.id)
    await message.answer(f"🎯 <b>Обери інтереси</b>", reply_markup=kb_interests(set(s.interests)))


@router.callback_query(F.data.startswith("interest:"))
async def cb_interest(callback: CallbackQuery) -> None:
    action = callback.data.split(":", 1)[1]
    s = await db_load_session(callback.from_user.id)
    if action == "save":
        await db_save_interests(callback.from_user.id, s.interests)
        chosen = ", ".join(s.interests) if s.interests else "жодного"
        await callback.message.edit_text(f"✅ <b>Збережено</b>\n{DIVIDER}\n🎯 {chosen}")
        await callback.answer("Збережено!")
        return
    try:
        interest = Interest[action].value
    except KeyError:
        await callback.answer("Невідома категорія", show_alert=True)
        return
    if interest in s.interests:
        s.interests.remove(interest)
    else:
        s.interests.append(interest)
    await callback.message.edit_reply_markup(reply_markup=kb_interests(set(s.interests)))
    await callback.answer()


# --- ПРОФІЛЬ ---

@router.message(F.text == "👤 Профіль")
async def btn_me(message: Message) -> None:
    s = await db_load_session(message.from_user.id)
    likes_total = s.likes + s.dislikes
    like_pct = int(s.likes / likes_total * 100) if likes_total else 0
    interests = ", ".join(s.interests) if s.interests else "<i>не обрано</i>"
    boost = await db_is_user_boosted(message.from_user.id)
    boost_line = ""
    if boost:
        boost_line = f"\n🚀 <b>Буст</b> до {boost['expires_at'][:16]}"
    prem_line = ""
    if s.is_premium:
        prem_line = f"\n⭐️ <b>Преміум</b> до {s.premium_until[:16] if s.premium_until else '—'}"
    nick_line = f"\n🏷 <b>Нік:</b> {s.custom_nickname}" if s.custom_nickname else ""
    await message.answer(
        f"👤 <b>Твій профіль</b>\n{DIVIDER}\n"
        f"{rep_emoji(s.reputation)} <b>Репутація:</b> {s.reputation}/100\n"
        f"{rep_bar(s.reputation)}\n{rep_stars(s.reputation)} <b>{rep_label(s.reputation)}</b>\n"
        f"{DIVIDER}\n👍 {s.likes} · 👎 {s.dislikes} · 📊 {like_pct}%\n"
        f"💬 Чатів: <b>{s.total_chats}</b>{prem_line}{nick_line}{boost_line}\n"
        f"{DIVIDER}\n🎯 <b>Інтереси:</b>\n{interests}",
        reply_markup=kb_idle())


@router.message(Command("me"))
async def cmd_me(message: Message) -> None:
    await btn_me(message)


# --- ТОП ---

@router.message(F.text == "🏆 Топ")
async def btn_top(message: Message) -> None:
    await db_deactivate_expired_boosts()
    boosted = await db_get_active_boosts(limit=5)
    boosted_ids = {b["user_id"] for b in boosted}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT user_id, reputation, likes, total_chats FROM users
               WHERE reputation > 0 AND is_banned = 0
               ORDER BY reputation DESC, likes DESC LIMIT 20""") as cur:
            rows = await cur.fetchall()
    regular = [r for r in rows if r[0] not in boosted_ids]
    for b in boosted:
        await db_boost_impression(b["id"])
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Топ-10</b>", DIVIDER, ""]
    for b in boosted:
        uid = b["user_id"]
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT reputation, likes, total_chats FROM users WHERE user_id=?",
                (uid,)) as cur:
                row = await cur.fetchone()
        if not row:
            continue
        rep, likes, chats = row
        lines.append(f"🚀 <code>id{str(uid)[-4:]}</code> — {rep_emoji(rep)} "
                     f"<b>{rep}</b>/100 · 👍 {likes} · 💬 {chats}")
    if boosted:
        lines.append("")
    for i, (uid, rep, likes, chats) in enumerate(regular[:10 - len(boosted)]):
        idx = i + len(boosted)
        medal = medals[idx] if idx < 3 else f"<b>{idx+1}.</b>"
        lines.append(f"{medal} <code>id{str(uid)[-4:]}</code> — {rep_emoji(rep)} "
                     f"<b>{rep}</b>/100 · 👍 {likes} · 💬 {chats}")
    lines.append("\n🚀 <i>Хочеш на вершину? /boost</i>")
    await message.answer("\n".join(lines), reply_markup=kb_idle())


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    await btn_top(message)


# --- БУСТ ---

@router.message(F.text == "🚀 Буст у топі")
async def btn_boost(message: Message) -> None:
    await _show_boost(message)


@router.message(Command("boost"))
async def cmd_boost(message: Message) -> None:
    await _show_boost(message)


async def _show_boost(message: Message) -> None:
    await db_load_session(message.from_user.id)
    active = await db_is_user_boosted(message.from_user.id)
    if active:
        await message.answer(
            f"🚀 <b>Твій буст активний!</b>\n{DIVIDER}\n"
            f"⏱ До: {active['expires_at']}\n👁 {active['impressions']}\n"
            f"💎 {active['stars_paid']} ⭐️",
            reply_markup=kb_boost_menu())
        return
    prices_text = "\n".join(f"• {d} дн. — <b>{s}</b> ⭐️" for d, s in BOOST_PRICES.items())
    await message.answer(
        f"🚀 <b>Буст у топі</b>\n{DIVIDER}\n"
        f"Підніми профіль на <b>вершину рейтингу</b>!\n\n"
        f"💰 <b>Ціни:</b>\n{prices_text}\n{DIVIDER_SHORT}\n"
        f"💳 <i>Оплата через Telegram Stars</i>",
        reply_markup=kb_boost_menu())


@router.callback_query(F.data == "boost:cancel")
async def cb_boost_cancel(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Скасовано")


@router.callback_query(F.data == "boost:history")
async def cb_boost_history(callback: CallbackQuery) -> None:
    boosts = await db_get_active_boosts(limit=10)
    my = [b for b in boosts if b["user_id"] == callback.from_user.id]
    if not my:
        await callback.answer("У тебе немає бустів", show_alert=True)
        return
    lines = ["🚀 <b>Мої бусти</b>", DIVIDER, ""]
    for b in my:
        lines.append(f"🟢 <b>{b['duration_days']} дн.</b> — {b['stars_paid']} ⭐️")
    try:
        await callback.message.edit_text("\n".join(lines))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("boost:buy:"))
async def cb_boost_buy(callback: CallbackQuery) -> None:
    bot = callback.bot
    days = int(callback.data.split(":")[2])
    stars = BOOST_PRICES.get(days)
    if not stars:
        await callback.answer("Невірний термін", show_alert=True)
        return
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"🚀 Буст топу на {days} днів",
            description=f"Профіль на вершині {days} днів.",
            payload=f"boost:{callback.from_user.id}:{days}:{stars}",
            provider_token="", currency="XTR",
            prices=[LabeledPrice(label=f"Буст на {days} дн.", amount=int(stars))])
        await callback.answer()
    except Exception as e:
        logger.exception("Буст: %s", e)
        await callback.answer("⚠️ Помилка", show_alert=True)


# --- ПРЕМІУМ ---

@router.message(F.text == "⭐️ Преміум")
async def btn_premium(message: Message) -> None:
    await _show_premium(message)


@router.message(Command("premium"))
async def cmd_premium(message: Message) -> None:
    await _show_premium(message)


async def _show_premium(message: Message) -> None:
    s = await db_load_session(message.from_user.id)
    if s.is_premium:
        await message.answer(
            f"⭐️ <b>Преміум активний!</b>\n{DIVIDER}\n"
            f"⏱ До: {s.premium_until}\n{DIVIDER}\n"
            f"🎁 Без реклами · Пріоритет · Знижка на нік · Бейдж ⭐️",
            reply_markup=kb_premium_menu())
        return
    await message.answer(
        f"⭐️ <b>Преміум-підписка</b>\n{DIVIDER}\n"
        f"🎁 <b>Що дає:</b>\n• Без реклами\n• Пріоритет у черзі\n"
        f"• Знижка на нік (50 ⭐️)\n• Бейдж ⭐️\n\n"
        f"💰 <b>Ціна:</b> {PREMIUM_PRICE} ⭐️/міс\n"
        f"💳 <i>Оплата через Telegram Stars</i>",
        reply_markup=kb_premium_menu())


@router.callback_query(F.data == "premium:cancel")
async def cb_premium_cancel(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Скасовано")


@router.callback_query(F.data == "premium:buy")
async def cb_premium_buy(callback: CallbackQuery) -> None:
    bot = callback.bot
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title="⭐️ Преміум-підписка",
            description="30 днів без реклами",
            payload=f"premium:{callback.from_user.id}:30:{PREMIUM_PRICE}",
            provider_token="", currency="XTR",
            prices=[LabeledPrice(label="Преміум на 30 днів", amount=int(PREMIUM_PRICE))])
        await callback.answer()
    except Exception as e:
        logger.exception("Преміум: %s", e)
        await callback.answer("⚠️ Помилка", show_alert=True)


# --- НІК ---

@router.message(F.text == "🏷 Кастомний нік")
async def btn_nick(message: Message) -> None:
    await _show_nick(message)


@router.message(Command("nick"))
async def cmd_nick(message: Message) -> None:
    await _show_nick(message)


async def _show_nick(message: Message) -> None:
    s = await db_load_session(message.from_user.id)
    price = PREMIUM_NICKNAME_PRICE if s.is_premium else NICKNAME_PRICE
    current = f"\n🏷 <b>Поточний:</b> {s.custom_nickname}\n" if s.custom_nickname else ""
    await message.answer(
        f"🏷 <b>Кастомний нік</b>\n{DIVIDER}\n"
        f"Замість 'Незнайомець #1234' — твоє ім'я!\n{current}\n"
        f"🎯 2-20 символів, літери, цифри, пробіл, _ і -\n"
        f"💰 <b>Ціна:</b> {price} ⭐️",
        reply_markup=kb_nickname_menu(s.is_premium))


@router.callback_query(F.data == "nick:cancel")
async def cb_nick_cancel(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Скасовано")


@router.callback_query(F.data == "nick:reset")
async def cb_nick_reset(callback: CallbackQuery) -> None:
    s = await db_load_session(callback.from_user.id)
    if not s.custom_nickname:
        await callback.answer("Немає кастомного ніка", show_alert=True)
        return
    s.custom_nickname = None
    await db_save_session(s)
    await callback.answer("✅ Нік скинуто", show_alert=True)


@router.callback_query(F.data == "nick:buy")
async def cb_nick_buy(callback: CallbackQuery) -> None:
    bot = callback.bot
    s = await db_load_session(callback.from_user.id)
    price = PREMIUM_NICKNAME_PRICE if s.is_premium else NICKNAME_PRICE
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title="🏷 Кастомний нік",
            description="Оплати, потім введи нік",
            payload=f"nickname:{callback.from_user.id}:0:{price}",
            provider_token="", currency="XTR",
            prices=[LabeledPrice(label="Кастомний нік", amount=int(price))])
        await callback.answer()
    except Exception as e:
        logger.exception("Нік: %s", e)
        await callback.answer("⚠️ Помилка", show_alert=True)


@router.message(F.text == "❌ Скасувати зміну ніка")
async def btn_nick_cancel(message: Message) -> None:
    session = get_session(message.from_user.id)
    session.state = UserState.IDLE
    await message.answer("↩️ Скасовано.", reply_markup=kb_idle())


# --- КАНАЛ ---

@router.message(F.text == "📢 Канал")
async def btn_channel(message: Message) -> None:
    await message.answer(
        f"📢 <b>Наш канал</b>\n{DIVIDER}\n➡️ <a href='{CHANNEL_URL}'>{CHANNEL_NAME}</a>",
        reply_markup=inline_channel_button(), disable_web_page_preview=True)


@router.message(Command("channel"))
async def cmd_channel(message: Message) -> None:
    await btn_channel(message)


# --- ПІДТРИМКА ---

@router.message(F.text == "🛠 Підтримка")
async def btn_support(message: Message) -> None:
    session = get_session(message.from_user.id)
    if session.state == UserState.CHATTING:
        await end_chat(message.bot, message.from_user.id)
    elif session.state == UserState.SEARCHING:
        await stop_search(message.bot, message.from_user.id)
    session.state = UserState.SUPPORT
    await message.answer(
        f"🛠 <b>Техпідтримка</b>\n{DIVIDER}\nОпиши питання — адмін відповість тут.",
        reply_markup=kb_support())


@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    await btn_support(message)


@router.message(F.text == "❌ Вийти з підтримки")
async def btn_support_exit(message: Message) -> None:
    session = get_session(message.from_user.id)
    session.state = UserState.IDLE
    await message.answer("✅ Звернення завершено. 🙏", reply_markup=kb_idle())


# --- ДОВІДКА ---

@router.message(F.text == "ℹ️ Довідка")
async def btn_help(message: Message) -> None:
    await _show_help(message)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _show_help(message)


async def _show_help(message: Message) -> None:
    await message.answer(
        f"📖 <b>Довідка</b>\n{DIVIDER}\n"
        f"💬 <b>Звичайний чат</b>\n🔥 <b>Пошлий 18+</b>\n"
        f"🎯 <b>Інтереси</b>\n👤 <b>Профіль</b>\n🏆 <b>Топ</b>\n\n"
        f"{DIVIDER_SHORT}\n⭐️ <b>Преміум</b> — 200 ⭐️/міс\n"
        f"🚀 <b>Буст</b> — від 50 ⭐️\n🏷 <b>Нік</b> — 150/50 ⭐️",
        reply_markup=kb_idle())


# --- ОЦІНКА ---

@router.callback_query(F.data.startswith("rate:"))
async def cb_rate(callback: CallbackQuery) -> None:
    rater = await db_load_session(callback.from_user.id)
    partner_id = rater.last_partner_id
    if not partner_id:
        await callback.answer("Нема кого оцінювати 🤷", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    partner = await db_load_session(partner_id)
    if action == "like":
        partner.likes += 1
        partner.reputation = min(100, partner.reputation + 5)
        text = "✅ <b>+5 до репутації</b> 👍"
    elif action == "dislike":
        partner.dislikes += 1
        partner.reputation = max(0, partner.reputation - 5)
        text = "😕 <b>−5</b> 👎"
    else:
        text = "⏭ <b>Пропущено</b>"
    await db_save_session(partner)
    rater.last_partner_id = None
    await db_save_session(rater)
    try:
        await callback.message.edit_text(text)
    except Exception:
        pass
    await callback.answer()


# --- АДМІН ---

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    total_users = len(sessions)
    chatting = sum(1 for s in sessions.values() if s.state == UserState.CHATTING)
    support = sum(1 for s in sessions.values() if s.state == UserState.SUPPORT)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            db_users = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE is_banned=1") as cur:
            banned = (await cur.fetchone())[0]
    await message.answer(
        f"📊 <b>Статистика</b>\n{DIVIDER}\n"
        f"👥 Сесій: <b>{total_users}</b>\n💾 У БД: <b>{db_users}</b>\n"
        f"🚫 Бан: <b>{banned}</b>\n{DIVIDER_SHORT}\n"
        f"⏳ Черга: <b>{len(search_queue_normal)}</b> / 🔥 <b>{len(search_queue_flirty)}</b>\n"
        f"💬 Чатів: <b>{chatting // 2}</b>\n🛠 Підтримка: <b>{support}</b>")


@router.message(Command("ban"))
async def cmd_ban(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Використання: /ban <user_id>")
        return
    uid = int(parts[1])
    s = get_session(uid)
    s.is_banned = True
    await db_save_session(s)
    if s.state == UserState.CHATTING:
        try:
            await end_chat(message.bot, uid)
        except Exception:
            pass
    await message.answer(f"🚫 Забанено <code>{uid}</code>.")


@router.message(Command("unban"))
async def cmd_unban(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Використання: /unban <user_id>")
        return
    uid = int(parts[1])
    s = get_session(uid)
    s.is_banned = False
    await db_save_session(s)
    await message.answer(f"✅ Розбанено <code>{uid}</code>.")


@router.message(Command("ads"))
async def cmd_ads(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    campaigns = await db_list_campaigns()
    if not campaigns:
        await message.answer("📭 Кампаній немає.")
        return
    lines = ["📢 <b>Кампанії</b>", DIVIDER, ""]
    for c in campaigns:
        status = "🟢" if c["is_active"] else "🔴"
        lines.append(f"{status} <b>#{c['id']}</b> · {c['title']}\n"
                     f"   👁 {c['impressions']} · 🖱 {c['clicks']}")
    await message.answer("\n".join(lines))


@router.message(Command("adadd"))
async def cmd_ad_add(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = [p.strip() for p in message.text.split("|")]
    if len(parts) < 3:
        await message.answer("Формат: /adadd Назва | Текст | Кнопка | URL | mode | інтереси | ліміт")
        return
    title, text = parts[0], parts[1]
    button_text = parts[2] if len(parts) > 2 and parts[2] else None
    button_url = parts[3] if len(parts) > 3 and parts[3] else None
    target_mode = parts[4] if len(parts) > 4 and parts[4] else "any"
    target_interests = parts[5] if len(parts) > 5 else ""
    max_impressions = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0
    cid = await db_add_campaign(title, text, button_text, button_url,
                                target_mode, target_interests, max_impressions)
    await message.answer(f"✅ Кампанія <code>{cid}</code> створена.")


# --- ПЛАТЕЖІ ---

@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout: PreCheckoutQuery) -> None:
    try:
        await pre_checkout.answer(ok=True)
    except Exception as e:
        logger.warning("Pre-checkout: %s", e)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    bot = message.bot
    payment = message.successful_payment
    payload = payment.invoice_payload
    try:
        ptype, uid_str, days_str, stars_str = payload.split(":")
        uid, days, stars = int(uid_str), int(days_str), int(stars_str)
    except Exception:
        return
    await db_log_payment(uid, ptype, stars, payload,
                         payment.telegram_payment_charge_id)
    if ptype == "boost":
        await db_create_boost(uid, days, stars, payment.telegram_payment_charge_id)
        s = await db_load_session(uid)
        await db_save_session(s)
        await message.answer(f"🎉 <b>Буст активовано!</b>\n⏱ {days} дн.\n💎 {stars} ⭐️")
    elif ptype == "premium":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE users SET is_premium=1,
                   premium_until=datetime(COALESCE(premium_until,'now'),'+30 days')
                   WHERE user_id=?""", (uid,))
            await db.commit()
        await db_load_session(uid)
        await message.answer(f"🎉 <b>Преміум активовано!</b>")
    elif ptype == "nickname":
        s = await db_load_session(uid)
        s.state = UserState.ENTERING_NICKNAME
        await db_save_session(s)
        await message.answer(
            f"🏷 <b>Оплату отримано!</b>\nВведи свій новий нік (2-20 символів):",
            reply_markup=kb_nickname_cancel())
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id,
                f"💰 <b>ОПЛАТА</b>\n👤 <code>{uid}</code>\n📦 {ptype}\n💎 {stars} ⭐️")
        except Exception:
            pass


# --- ВВЕДЕННЯ НІКА ---

@router.message(F.text)
async def process_nickname_input(message: Message) -> None:
    """Обробляє введення ніка — або передає в relay_message."""
    s = await db_load_session(message.from_user.id)

    if s.state == UserState.ENTERING_NICKNAME:
        nick = (message.text or "").strip()
        if not NICKNAME_VALID_PATTERN.match(nick):
            await message.answer(
                f"❌ <b>Невірний формат</b>\n{DIVIDER}\n2-20 символів, літери, цифри, пробіл, _ і -",
                reply_markup=kb_nickname_cancel())
            return
        if BAD_WORDS_PATTERN.search(nick):
            await message.answer("❌ <b>Заборонені слова</b>", reply_markup=kb_nickname_cancel())
            return
        s.custom_nickname = nick
        s.state = UserState.IDLE
        await db_save_session(s)
        await message.answer(f"✅ <b>Нік змінено!</b>\n🏷 <b>{nick}</b>", reply_markup=kb_idle())
        return

    # Не в режимі введення ніка — передаємо в relay
    await relay_message(message)


# --- ОСНОВНИЙ RELAY ---

async def relay_message(message: Message) -> None:
    bot = message.bot
    session = await db_load_session(message.from_user.id)

    if session.is_banned:
        await message.answer("🚫 Забанений.")
        return

    if session.state == UserState.SUPPORT:
        user = message.from_user
        header = (f"🆘 <b>ЗВЕРНЕННЯ</b>\n{DIVIDER}\n"
                  f"👤 {user.full_name}\n"
                  f"🔗 @{user.username if user.username else '—'}\n"
                  f"🆔 <code>{user.id}</code>\n{DIVIDER}")
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, header)
                copied = await bot.copy_message(chat_id=admin_id,
                                                from_chat_id=message.chat.id,
                                                message_id=message.message_id)
                support_reply_map[copied.message_id] = user.id
            except Exception as e:
                logger.warning("Підтримка: %s", e)
        await message.answer("✅ <b>Надіслано</b>", reply_markup=kb_support())
        return

    if session.state != UserState.CHATTING or session.partner_id is None:
        await message.answer(
            "🤷 <b>Ти не у чаті</b>\n\nНатисни 💬 або 🔥, щоб почати.",
            reply_markup=kb_idle())
        return

    if not session.spam_ok():
        await message.answer("⏳ <b>Занадто швидко!</b>")
        return

    room = active_rooms.get(message.from_user.id)
    if room and room.mode == ChatMode.NORMAL and message.text:
        if BAD_WORDS_PATTERN.search(message.text):
            await message.answer("⚠️ <b>Заборонені слова.</b>")
            return

    partner_id = session.partner_id
    if room is not None:
        await log_message_to_admins(
            bot, message, mode=room.mode,
            sender_id=message.from_user.id,
            sender_nick=room.nickname_of(message.from_user.id),
            recipient_id=partner_id,
            recipient_nick=room.nickname_of(partner_id))

    try:
        await bot.copy_message(chat_id=partner_id,
                               from_chat_id=message.chat.id,
                               message_id=message.message_id)
    except Exception as e:
        logger.warning("Пересилання: %s", e)
        await message.answer("⚠️ <b>Не доставлено.</b>")
        await end_chat(bot, message.from_user.id, notify_partner=False)
        await message.answer("💡 Спробуй нового 👇", reply_markup=kb_idle())
        return

    if room is not None:
        room.history.append(ChatMessage(
            sender_id=message.from_user.id,
            chat_id=message.chat.id,
            message_id=message.message_id))
    session.messages_in_chat += 1
    await show_ad_if_due(bot, message, session)


# ===========================================================================
# Фонові процеси та запуск
# ===========================================================================

async def boost_cleanup_loop():
    while True:
        try:
            count = await db_deactivate_expired_boosts()
            if count:
                logger.info("⏱ Деактивовано %d бустів", count)
        except Exception as e:
            logger.warning("Cleanup: %s", e)
        await asyncio.sleep(600)


async def main() -> None:
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS порожній.")
    await db_init()
    await db_ensure_default_campaigns()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(boost_cleanup_loop())
    logger.info("✅ Бот запущено. Ctrl+C для зупинки.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Бот зупинено")