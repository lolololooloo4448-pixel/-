import asyncio
import aiosqlite

DB_PATH = "bot.db"


async def migrate():
    async with aiosqlite.connect(DB_PATH) as db:
        # перевіряємо, які колонки вже є
        async with db.execute("PRAGMA table_info(users)") as cur:
            columns = {row[1] for row in await cur.fetchall()}

        print("Поточні колонки:", columns)

        migrations = [
            ("is_premium", "ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0"),
            ("premium_until", "ALTER TABLE users ADD COLUMN premium_until TIMESTAMP"),
            ("custom_nickname", "ALTER TABLE users ADD COLUMN custom_nickname TEXT"),
            ("nickname_last_changed", "ALTER TABLE users ADD COLUMN nickname_last_changed TIMESTAMP"),
        ]

        for name, sql in migrations:
            if name not in columns:
                print(f"➕ Додаю колонку: {name}")
                await db.execute(sql)
            else:
                print(f"✅ Колонка вже є: {name}")

        await db.commit()
        print("\n🎉 Міграція завершена!")


if __name__ == "__main__":
    asyncio.run(migrate())