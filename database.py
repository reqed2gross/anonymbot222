import asyncpg
from config import DATABASE_URL

pool: asyncpg.Pool | None = None


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                tg_id BIGINT UNIQUE NOT NULL,
                seen_welcome BOOLEAN DEFAULT FALSE,
                is_blocked BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS message_map (
                id SERIAL PRIMARY KEY,
                admin_chat_id BIGINT NOT NULL,
                admin_message_id BIGINT NOT NULL,
                user_tg_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_map "
            "ON message_map (admin_chat_id, admin_message_id);"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
    await _seed_defaults()


async def _seed_defaults():
    from texts import (
        DEFAULT_WELCOME, DEFAULT_AUTOREPLY, DEFAULT_ANTISPAM_TEXT,
        DEFAULT_ANTISPAM_LIMIT, DEFAULT_ANTISPAM_WINDOW,
    )
    defaults = {
        "welcome_text": DEFAULT_WELCOME,
        "autoreply_text": DEFAULT_AUTOREPLY,
        "antispam_text": DEFAULT_ANTISPAM_TEXT,
        "antispam_limit": str(DEFAULT_ANTISPAM_LIMIT),
        "antispam_window": str(DEFAULT_ANTISPAM_WINDOW),
    }
    async with pool.acquire() as conn:
        for k, v in defaults.items():
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO NOTHING",
                k, v,
            )


async def get_setting(key: str) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
    return row["value"] if row else None


async def set_setting(key: str, value: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key, value,
        )


async def get_or_create_user(tg_id: int) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)
        if row:
            return dict(row)
        row = await conn.fetchrow(
            "INSERT INTO users (tg_id) VALUES ($1) RETURNING *", tg_id
        )
        return dict(row)


async def get_user_by_tg(tg_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)
    return dict(row) if row else None


async def get_user_by_internal_id(internal_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", internal_id)
    return dict(row) if row else None


async def mark_seen_welcome(tg_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET seen_welcome = TRUE WHERE tg_id = $1", tg_id
        )


async def set_blocked(tg_id: int, blocked: bool):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET is_blocked = $1 WHERE tg_id = $2", blocked, tg_id
        )


async def save_message_map(admin_chat_id: int, admin_message_id: int, user_tg_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO message_map (admin_chat_id, admin_message_id, user_tg_id) "
            "VALUES ($1, $2, $3)",
            admin_chat_id, admin_message_id, user_tg_id,
        )


async def lookup_message_map(admin_chat_id: int, admin_message_id: int) -> int | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_tg_id FROM message_map "
            "WHERE admin_chat_id = $1 AND admin_message_id = $2",
            admin_chat_id, admin_message_id,
        )
    return row["user_tg_id"] if row else None


async def get_stats() -> tuple[int, int, int]:
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        blocked_users = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE is_blocked = TRUE"
        )
        total_messages = await conn.fetchval("SELECT COUNT(*) FROM message_map")
    return total_users, blocked_users, total_messages

async def get_users_paginated(offset: int, limit: int) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, is_blocked FROM users ORDER BY id LIMIT $1 OFFSET $2",
            limit, offset
        )
    return [dict(r) for r in rows]

async def get_users_count() -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")

async def close_db():
    global pool
    if pool:
        await pool.close()
        pool = None
