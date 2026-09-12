"""
🩺 Doctor v3.0 — перевірка бота + Telegram-сповіщення + Web-панель.

Використання:
    python doctor.py                       # перевірка + HTML + Telegram
    python doctor.py --no-tg               # без Telegram
    python doctor.py --quiet               # тихо (тільки помилки)
    python doctor.py --all                 # + виправлення
    python doctor.py --web                 # + запустити Web-панель
    python doctor.py --notify-only         # тільки надіслати останній звіт
"""

import argparse
import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Налаштування
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "bot.db")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DOCTOR_WEB_PORT = int(os.getenv("DOCTOR_WEB_PORT", "8080"))
DOCTOR_WEB_TOKEN = os.getenv("DOCTOR_WEB_TOKEN", "change-me")

BOT_FILE = "bot.py"
ENV_FILE = ".env"
REQ_FILE = "requirements.txt"
HTML_REPORT = "doctor_report.html"
JSON_REPORT = "doctor_report.json"
HISTORY_FILE = "doctor_history.json"

REQUIRED_PACKAGES = ["aiogram", "aiosqlite", "dotenv"]

REQUIRED_TABLES = {
    "users": [
        "user_id", "reputation", "likes", "dislikes", "total_chats",
        "is_banned", "is_premium", "premium_until",
        "custom_nickname", "nickname_last_changed",
    ],
    "interests": ["user_id", "interest"],
    "blocks": ["user_id", "blocked_id"],
    "reports": ["id", "reporter_id", "target_id", "category", "created_at"],
    "ad_campaigns": [
        "id", "title", "text", "button_text", "button_url",
        "target_mode", "target_interests", "max_impressions",
        "impressions", "clicks", "is_active", "created_at", "expires_at",
    ],
    "top_boosts": [
        "id", "user_id", "stars_paid", "duration_days",
        "started_at", "expires_at", "impressions", "is_active", "payment_id",
    ],
    "payments": [
        "id", "user_id", "payment_type", "stars_paid",
        "payload", "payment_id", "created_at",
    ],
}

AUTO_MIGRATIONS = {
    "users": [
        ("is_premium", "ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0"),
        ("premium_until", "ALTER TABLE users ADD COLUMN premium_until TIMESTAMP"),
        ("custom_nickname", "ALTER TABLE users ADD COLUMN custom_nickname TEXT"),
        ("nickname_last_changed", "ALTER TABLE users ADD COLUMN nickname_last_changed TIMESTAMP"),
    ],
}


# ---------------------------------------------------------------------------
# Кольори
# ---------------------------------------------------------------------------

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"


def ok(msg): print(f"{C.GREEN}✅ {msg}{C.RESET}")
def warn(msg): print(f"{C.YELLOW}⚠️  {msg}{C.RESET}")
def err(msg): print(f"{C.RED}❌ {msg}{C.RESET}")
def info(msg): print(f"{C.CYAN}ℹ️  {msg}{C.RESET}")
def header(msg): print(f"\n{C.BOLD}{C.BLUE}━━━ {msg} ━━━{C.RESET}")


# ---------------------------------------------------------------------------
# Збір результатів
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.checks: list[dict] = []
        self.fixes: list[dict] = []
        self.tests: list[dict] = []
        self.start_time = datetime.now()
        self.end_time: datetime | None = None

    def add_check(self, category, name, status, message="", details=None,
                  line=None, file=None):
        self.checks.append({
            "category": category, "name": name, "status": status,
            "message": message, "details": details or [],
            "line": line, "file": file,
        })

    def add_fix(self, name, status, message=""):
        self.fixes.append({"name": name, "status": status, "message": message})

    def add_test(self, name, status, message="", duration=0):
        self.tests.append({
            "name": name, "status": status,
            "message": message, "duration": duration,
        })

    def finish(self):
        self.end_time = datetime.now()

    @property
    def total(self): return len(self.checks)

    @property
    def passed(self): return sum(1 for c in self.checks if c["status"] == "ok")

    @property
    def warnings(self): return sum(1 for c in self.checks if c["status"] == "warn")

    @property
    def errors(self): return sum(1 for c in self.checks if c["status"] == "error")

    @property
    def score(self):
        if self.total == 0: return 0
        return int((self.passed + self.warnings * 0.5) / self.total * 100)

    @property
    def duration(self):
        end = self.end_time or datetime.now()
        return (end - self.start_time).total_seconds()

    @property
    def critical(self):
        """Критичні помилки (тільки error, не warn)."""
        return [c for c in self.checks if c["status"] == "error"]

    def to_dict(self):
        return {
            "start_time": self.start_time.isoformat(),
            "end_time": (self.end_time or datetime.now()).isoformat(),
            "duration": self.duration,
            "score": self.score,
            "total": self.total,
            "passed": self.passed,
            "warnings": self.warnings,
            "errors": self.errors,
            "checks": self.checks,
            "fixes": self.fixes,
            "tests": self.tests,
        }


# ---------------------------------------------------------------------------
# ПЕРЕВІРКИ
# ---------------------------------------------------------------------------

def check_files(report):
    header("Перевірка файлів")
    for f in [BOT_FILE, ENV_FILE, REQ_FILE]:
        if Path(f).exists():
            size = Path(f).stat().st_size
            ok(f"{f} знайдено ({size} байт)")
            report.add_check("Файли", f, "ok", f"{size} байт")
        else:
            err(f"{f} НЕ знайдено")
            report.add_check("Файли", f, "error", "Файл відсутній")


def check_env(report):
    header("Перевірка .env")
    if not Path(ENV_FILE).exists():
        report.add_check(".env", "Існування", "error", ".env не знайдено")
        return
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        report.add_check(".env", "BOT_TOKEN", "error", "Не знайдено")
    elif len(token) < 30:
        report.add_check(".env", "BOT_TOKEN", "warn", f"Занадто короткий ({len(token)})")
    elif ":" not in token:
        report.add_check(".env", "BOT_TOKEN", "error", "Немає ':'")
    else:
        masked = token[:10] + "..." + token[-4:]
        report.add_check(".env", "BOT_TOKEN", "ok", f"Знайдено ({masked})")


def check_packages(report):
    header("Перевірка пакетів")
    for pkg in REQUIRED_PACKAGES:
        try:
            mod = __import__(pkg)
            version = getattr(mod, "__version__", "?")
            report.add_check("Пакети", pkg, "ok", f"v{version}")
        except ImportError:
            err(f"Пакет {pkg} НЕ встановлено")
            report.add_check("Пакети", pkg, "error", f"pip install {pkg}")


def check_syntax(report):
    header("Перевірка синтаксису")
    if not Path(BOT_FILE).exists():
        report.add_check("Синтаксис", "bot.py", "error", "Файл не існує")
        return None
    try:
        with open(BOT_FILE, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        ok(f"Синтаксис OK ({len(source)} символів)")
        report.add_check("Синтаксис", "bot.py", "ok", f"{len(source)} символів")
        return tree
    except SyntaxError as e:
        err(f"Синтаксична помилка: {e}")
        report.add_check("Синтаксис", "bot.py", "error", f"{e.msg}",
                         line=e.lineno, file=BOT_FILE)
        return None


def check_long_lines(report, max_len=120):
    header("Перевірка довгих рядків")
    if not Path(BOT_FILE).exists():
        return
    with open(BOT_FILE, encoding="utf-8") as f:
        lines = f.readlines()
    long_lines = [
        (i + 1, len(line.rstrip()))
        for i, line in enumerate(lines)
        if len(line.rstrip()) > max_len
    ]
    if not long_lines:
        report.add_check("Стиль", "Довгі рядки", "ok", f"≤ {max_len}")
    else:
        report.add_check("Стиль", "Довгі рядки", "warn",
                         f"{len(long_lines)} рядків > {max_len}",
                         details=[f"Рядок {ln}: {l} симв." for ln, l in long_lines[:10]])


def check_unused_imports(report):
    header("Перевірка невикористаних імпортів")
    if not Path(BOT_FILE).exists():
        return
    try:
        with open(BOT_FILE, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:
        return
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imported[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                imported[name] = node.lineno
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used.add(node.value.id)
    unused = {n: ln for n, ln in imported.items() if n not in used and n != "*"}
    if not unused:
        report.add_check("Імпорти", "Невикористані", "ok", "Усі використано")
    else:
        report.add_check("Імпорти", "Невикористані", "warn",
                         f"{len(unused)} невикористаних",
                         details=[f"{n} (рядок {ln})" for n, ln in unused.items()])


def check_await_usage(report):
    header("Перевірка await")
    if not Path(BOT_FILE).exists():
        return
    try:
        with open(BOT_FILE, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:
        return
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_async = isinstance(node, ast.AsyncFunctionDef)
            for child in ast.walk(node):
                if isinstance(child, ast.Await) and not is_async:
                    problems.append(
                        f"'{node.name}' (рядок {node.lineno}) — await без async"
                    )
    if not problems:
        report.add_check("Await", "Перевірка", "ok", "OK")
    else:
        report.add_check("Await", "Перевірка", "error",
                         f"{len(problems)} проблем", details=problems)


def check_duplicate_functions(report):
    header("Перевірка дублікатів")
    if not Path(BOT_FILE).exists():
        return
    try:
        with open(BOT_FILE, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:
        return
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, []).append(node.lineno)
    duplicates = {n: ls for n, ls in functions.items() if len(ls) > 1}
    if not duplicates:
        report.add_check("Функції", "Дублікати", "ok", "Усі унікальні")
    else:
        report.add_check("Функції", "Дублікати", "warn",
                         f"{len(duplicates)} дублікатів",
                         details=[f"{n}: рядки {ls}" for n, ls in duplicates.items()])


def check_bare_except(report):
    header("Перевірка except")
    if not Path(BOT_FILE).exists():
        return
    with open(BOT_FILE, encoding="utf-8") as f:
        lines = f.readlines()
    bare = [f"Рядок {i}" for i, l in enumerate(lines, 1)
            if l.strip() == "except:" or l.strip().startswith("except:")]
    if not bare:
        report.add_check("Обробка", "Bare except", "ok", "OK")
    else:
        report.add_check("Обробка", "Bare except", "warn",
                         f"{len(bare)} знайдено", details=bare[:10])


def check_print_usage(report):
    header("Перевірка print()")
    if not Path(BOT_FILE).exists():
        return
    with open(BOT_FILE, encoding="utf-8") as f:
        lines = f.readlines()
    prints = [f"Рядок {i}: {l.strip()[:60]}" for i, l in enumerate(lines, 1)
              if re.match(r"\s*print\s*\(", l) and not l.strip().startswith("#")]
    if not prints:
        report.add_check("Стиль", "print()", "ok", "OK")
    else:
        report.add_check("Стиль", "print()", "info",
                         f"{len(prints)} знайдено", details=prints[:10])


def check_db_exists(report):
    header("Перевірка БД")
    if not Path(DB_PATH).exists():
        report.add_check("БД", "Існування", "warn", "БД не існує")
    else:
        size = Path(DB_PATH).stat().st_size
        report.add_check("БД", "Існування", "ok", f"{size} байт")


async def check_db_schema(report):
    header("Перевірка схеми БД")
    if not Path(DB_PATH).exists():
        report.add_check("БД", "Схема", "info", "Пропущено")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cur:
            existing = {r[0] for r in await cur.fetchall()}
        for table, cols in REQUIRED_TABLES.items():
            if table not in existing:
                report.add_check("БД", f"Таблиця {table}", "error", "ВІДСУТНЯ")
                continue
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                existing_cols = {r[1] for r in await cur.fetchall()}
            missing = set(cols) - existing_cols
            if missing:
                report.add_check("БД", f"Таблиця {table}", "error",
                                 f"Відсутні: {', '.join(missing)}")
            else:
                report.add_check("БД", f"Таблиця {table}", "ok",
                                 f"{len(cols)} колонок")


# ---------------------------------------------------------------------------
# АВТОВИПРАВЛЕННЯ
# ---------------------------------------------------------------------------

async def auto_migrate_db(report):
    header("🔧 Автоміграція БД")
    if not Path(DB_PATH).exists():
        report.add_fix("Міграція БД", "info", "БД не існує")
        return
    fixed = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for table, migrations in AUTO_MIGRATIONS.items():
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                existing = {r[1] for r in await cur.fetchall()}
            for col, sql in migrations:
                if col not in existing:
                    try:
                        await db.execute(sql)
                        report.add_fix(f"{table}.{col}", "ok", "Додано")
                        fixed += 1
                    except Exception as e:
                        report.add_fix(f"{table}.{col}", "error", str(e))
        await db.commit()
    if fixed == 0:
        report.add_fix("Міграція БД", "info", "Нічого не потрібно")


def auto_format_code(report):
    header("🔧 Автоформатування")
    if not Path(BOT_FILE).exists():
        report.add_fix("Форматування", "error", "bot.py не існує")
        return
    backup = f"{BOT_FILE}.backup"
    shutil.copy2(BOT_FILE, backup)
    report.add_fix("Бекап", "ok", backup)
    try:
        r = subprocess.run([sys.executable, "-m", "isort", BOT_FILE],
                           capture_output=True, text=True, timeout=30)
        report.add_fix("isort", "ok" if r.returncode == 0 else "warn",
                       "OK" if r.returncode == 0 else r.stderr[:100])
    except FileNotFoundError:
        report.add_fix("isort", "info", "Не встановлено")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "black", BOT_FILE, "--line-length", "100"],
            capture_output=True, text=True, timeout=60)
        report.add_fix("black", "ok" if r.returncode == 0 else "warn",
                       "OK" if r.returncode == 0 else r.stderr[:100])
    except FileNotFoundError:
        report.add_fix("black", "info", "Не встановлено")


# ---------------------------------------------------------------------------
# АВТОТЕСТИ
# ---------------------------------------------------------------------------

async def run_tests(report):
    header("🧪 Автотести")

    t0 = datetime.now()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("SELECT 1")
        report.add_test("Підключення до БД", "ok", "OK",
                        (datetime.now() - t0).total_seconds())
    except Exception as e:
        report.add_test("Підключення до БД", "error", str(e),
                        (datetime.now() - t0).total_seconds())

    t0 = datetime.now()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                count = (await cur.fetchone())[0]
        report.add_test("Читання users", "ok", f"{count} записів",
                        (datetime.now() - t0).total_seconds())
    except Exception as e:
        report.add_test("Читання users", "error", str(e),
                        (datetime.now() - t0).total_seconds())

    t0 = datetime.now()
    test_id = 999999999
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO users (user_id, reputation) VALUES (?, ?)",
                             (test_id, 50))
            await db.commit()
            await db.execute("DELETE FROM users WHERE user_id=?", (test_id,))
            await db.commit()
        report.add_test("Запис/видалення", "ok", "OK",
                        (datetime.now() - t0).total_seconds())
    except Exception as e:
        report.add_test("Запис/видалення", "error", str(e),
                        (datetime.now() - t0).total_seconds())

    t0 = datetime.now()
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if token and len(token) > 30:
        report.add_test("Валідність .env", "ok", f"{len(token)} символів",
                        (datetime.now() - t0).total_seconds())
    else:
        report.add_test("Валідність .env", "error", "Некоректний",
                        (datetime.now() - t0).total_seconds())

    t0 = datetime.now()
    try:
        with open(BOT_FILE, encoding="utf-8") as f:
            source = f.read()
        required = ["async def main", "async def db_init", "Dispatcher", "Router"]
        missing = [r for r in required if r not in source]
        if missing:
            report.add_test("Структура bot.py", "warn",
                            f"Відсутні: {', '.join(missing)}",
                            (datetime.now() - t0).total_seconds())
        else:
            report.add_test("Структура bot.py", "ok", "OK",
                            (datetime.now() - t0).total_seconds())
    except Exception as e:
        report.add_test("Структура bot.py", "error", str(e),
                        (datetime.now() - t0).total_seconds())


# ---------------------------------------------------------------------------
# HTML-ЗВІТ
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<title>🩺 Doctor Report — {timestamp}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
    color: #e0e0e0; min-height: 100vh; padding: 30px 20px;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{
    font-size: 32px; margin-bottom: 10px;
    background: linear-gradient(90deg, #00d2ff, #3a7bd5);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  .subtitle {{ color: #888; margin-bottom: 30px; font-size: 14px; }}
  .score-card {{
    background: rgba(255,255,255,0.05); border-radius: 20px;
    padding: 30px; margin-bottom: 30px; text-align: center;
    border: 1px solid rgba(255,255,255,0.1);
  }}
  .score {{
    font-size: 72px; font-weight: bold; margin-bottom: 10px;
    background: linear-gradient(90deg, #00d2ff, #3a7bd5);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  .score-label {{ color: #aaa; font-size: 14px; text-transform: uppercase; letter-spacing: 2px; }}
  .progress-bar {{
    width: 100%; height: 12px; background: rgba(255,255,255,0.1);
    border-radius: 6px; overflow: hidden; margin: 20px 0;
  }}
  .progress-fill {{
    height: 100%; border-radius: 6px;
    background: linear-gradient(90deg, #00d2ff, #3a7bd5);
  }}
  .stats {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 15px; margin-top: 20px;
  }}
  .stat {{
    background: rgba(0,0,0,0.3); padding: 15px;
    border-radius: 12px; text-align: center;
  }}
  .stat-value {{ font-size: 28px; font-weight: bold; }}
  .stat-label {{ font-size: 12px; color: #888; margin-top: 5px; }}
  .stat.ok .stat-value {{ color: #4ade80; }}
  .stat.warn .stat-value {{ color: #fbbf24; }}
  .stat.error .stat-value {{ color: #f87171; }}
  .stat.info .stat-value {{ color: #60a5fa; }}
  .section {{
    background: rgba(255,255,255,0.03); border-radius: 16px;
    padding: 20px; margin-bottom: 20px;
    border: 1px solid rgba(255,255,255,0.08);
  }}
  .section h2 {{
    font-size: 18px; margin-bottom: 15px; color: #fff;
    display: flex; align-items: center; gap: 10px;
  }}
  .check {{
    display: flex; align-items: flex-start; gap: 12px;
    padding: 12px; border-radius: 10px; margin-bottom: 8px;
    background: rgba(0,0,0,0.2);
  }}
  .check.ok {{ border-left: 3px solid #4ade80; }}
  .check.warn {{ border-left: 3px solid #fbbf24; }}
  .check.error {{ border-left: 3px solid #f87171; }}
  .check.info {{ border-left: 3px solid #60a5fa; }}
  .check-icon {{ font-size: 20px; flex-shrink: 0; }}
  .check-content {{ flex: 1; }}
  .check-name {{ font-weight: 600; margin-bottom: 4px; }}
  .check-message {{ font-size: 13px; color: #aaa; }}
  .check-details {{
    margin-top: 8px; padding: 10px; background: rgba(0,0,0,0.4);
    border-radius: 6px; font-family: monospace; font-size: 12px;
    color: #ccc; max-height: 200px; overflow-y: auto;
  }}
  .check-details div {{ margin-bottom: 3px; }}
  .fix {{
    padding: 10px 14px; border-radius: 8px; margin-bottom: 6px;
    background: rgba(0,0,0,0.2);
    display: flex; justify-content: space-between; align-items: center;
  }}
  .fix-status {{ font-size: 12px; padding: 3px 8px; border-radius: 6px; }}
  .fix-status.ok {{ background: #4ade8022; color: #4ade80; }}
  .fix-status.warn {{ background: #fbbf2422; color: #fbbf24; }}
  .fix-status.error {{ background: #f8717122; color: #f87171; }}
  .fix-status.info {{ background: #60a5fa22; color: #60a5fa; }}
  .footer {{
    text-align: center; color: #666; font-size: 12px;
    margin-top: 40px; padding-top: 20px;
    border-top: 1px solid rgba(255,255,255,0.1);
  }}
  .timestamp {{
    display: inline-block; padding: 4px 10px;
    background: rgba(0,210,255,0.15); border-radius: 6px;
    color: #00d2ff; font-size: 12px;
  }}
</style>
</head>
<body>
<div class="container">
  <h1>🩺 Doctor Report</h1>
  <p class="subtitle">
    Перевірка бота • <span class="timestamp">{timestamp}</span> •
    Тривалість: {duration:.2f}с
  </p>
  <div class="score-card">
    <div class="score">{score}%</div>
    <div class="score-label">Загальний стан</div>
    <div class="progress-bar">
      <div class="progress-fill" style="width: {score}%"></div>
    </div>
    <div class="stats">
      <div class="stat ok"><div class="stat-value">{passed}</div>
        <div class="stat-label">✅ Пройдено</div></div>
      <div class="stat warn"><div class="stat-value">{warnings}</div>
        <div class="stat-label">⚠️ Попередження</div></div>
      <div class="stat error"><div class="stat-value">{errors}</div>
        <div class="stat-label">❌ Помилки</div></div>
      <div class="stat info"><div class="stat-value">{total}</div>
        <div class="stat-label">📊 Всього</div></div>
    </div>
  </div>
  {sections}
  {fixes_section}
  {tests_section}
  <div class="footer">🩺 Doctor v3.0 • Згенеровано {timestamp}</div>
</div>
</body>
</html>
"""


def status_icon(s):
    return {"ok": "✅", "warn": "⚠️", "error": "❌", "info": "ℹ️"}.get(s, "•")


def generate_html_report(report):
    categories = {}
    for c in report.checks:
        categories.setdefault(c["category"], []).append(c)

    sections = []
    for cat, checks in categories.items():
        checks_html = []
        for c in checks:
            details = ""
            if c["details"]:
                details = '<div class="check-details">' + "".join(
                    f"<div>{d}</div>" for d in c["details"]
                ) + "</div>"
            line_info = f' <span style="color:#666">(рядок {c["line"]})</span>' if c.get("line") else ""
            checks_html.append(f"""
            <div class="check {c['status']}">
              <div class="check-icon">{status_icon(c['status'])}</div>
              <div class="check-content">
                <div class="check-name">{c['name']}{line_info}</div>
                <div class="check-message">{c['message']}</div>
                {details}
              </div>
            </div>""")
        sections.append(f"""
        <div class="section">
          <h2>📂 {cat}</h2>
          {''.join(checks_html)}
        </div>""")

    fixes_section = ""
    if report.fixes:
        fixes_html = "".join(f"""
        <div class="fix">
          <div class="fix-name">{f['name']}</div>
          <div class="fix-status {f['status']}">{f['message']}</div>
        </div>""" for f in report.fixes)
        fixes_section = f"""
        <div class="section"><h2>🔧 Автовиправлення</h2>{fixes_html}</div>"""

    tests_section = ""
    if report.tests:
        tests_html = "".join(f"""
        <div class="check {t['status']}">
          <div class="check-icon">{status_icon(t['status'])}</div>
          <div class="check-content">
            <div class="check-name">{t['name']}</div>
            <div class="check-message">{t['message']} ({t['duration']*1000:.0f}мс)</div>
          </div>
        </div>""" for t in report.tests)
        tests_section = f"""
        <div class="section"><h2>🧪 Автотести</h2>{tests_html}</div>"""

    html = HTML_TEMPLATE.format(
        timestamp=report.start_time.strftime("%d.%m.%Y %H:%M:%S"),
        duration=report.duration,
        score=report.score,
        passed=report.passed,
        warnings=report.warnings,
        errors=report.errors,
        total=report.total,
        sections="".join(sections),
        fixes_section=fixes_section,
        tests_section=tests_section,
    )

    with open(HTML_REPORT, "w", encoding="utf-8") as f:
        f.write(html)
    ok(f"HTML-звіт: {HTML_REPORT}")


def generate_json_report(report):
    with open(JSON_REPORT, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    ok(f"JSON-звіт: {JSON_REPORT}")


def save_history(report):
    """Додає поточний звіт в історію."""
    history = []
    if Path(HISTORY_FILE).exists():
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []

    history.append({
        "timestamp": report.start_time.isoformat(),
        "score": report.score,
        "passed": report.passed,
        "warnings": report.warnings,
        "errors": report.errors,
        "duration": report.duration,
    })

    # тримаємо останні 100
    history = history[-100:]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# TELEGRAM-СПОВІЩЕННЯ
# ---------------------------------------------------------------------------

async def send_telegram_report(report, quiet=False):
    """Надсилає звіт адміну в Telegram."""
    if not BOT_TOKEN or not ADMIN_ID:
        warn("BOT_TOKEN або ADMIN_ID не налаштовані — Telegram-сповіщення пропущено")
        return

    # якщо quiet і немає критичних помилок — не турбуємо
    if quiet and not report.critical:
        info("Тихий режим: критичних помилок немає — не надсилаю")
        return

    from aiogram import Bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        # визначаємо статус
        if report.errors > 0:
            emoji = "🚨"
            title = "КРИТИЧНІ ПОМИЛКИ"
        elif report.warnings > 0:
            emoji = "⚠️"
            title = "Є попередження"
        else:
            emoji = "✅"
            title = "Усе ОК"

        # формуємо текст
        text = (
            f"{emoji} <b>Doctor Report</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>{title}</b>\n"
            f"📅 {report.start_time.strftime('%d.%m.%Y %H:%M:%S')}\n"
            f"⏱ Тривалість: <b>{report.duration:.2f}с</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Загальний стан: {report.score}%</b>\n\n"
            f"✅ Пройдено: <b>{report.passed}</b>\n"
            f"⚠️ Попередження: <b>{report.warnings}</b>\n"
            f"❌ Помилки: <b>{report.errors}</b>\n"
            f"📁 Всього: <b>{report.total}</b>\n"
        )

        # додаємо критичні помилки
        if report.critical:
            text += f"\n━━━━━━━━━━━━━━━━━━━━\n"
            text += f"🚨 <b>Критичні:</b>\n"
            for c in report.critical[:5]:
                text += f"• {c['name']}: {c['message']}\n"
            if len(report.critical) > 5:
                text += f"... та ще {len(report.critical) - 5}\n"

        # додаємо тести
        if report.tests:
            tests_ok = sum(1 for t in report.tests if t["status"] == "ok")
            text += f"\n━━━━━━━━━━━━━━━━━━━━\n"
            text += f"🧪 Тести: <b>{tests_ok}/{len(report.tests)}</b>\n"

        # додаємо виправлення
        if report.fixes:
            fixes_ok = sum(1 for f in report.fixes if f["status"] == "ok")
            text += f"🔧 Виправлення: <b>{fixes_ok}/{len(report.fixes)}</b>\n"

        # клавіатура
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📄 HTML-звіт", callback_data="doctor:html"),
                InlineKeyboardButton(text="📊 Деталі", callback_data="doctor:details"),
            ],
            [
                InlineKeyboardButton(text="🔧 Виправити", callback_data="doctor:fix"),
                InlineKeyboardButton(text="🔇 Ігнорувати", callback_data="doctor:ignore"),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 Web-панель",
                    url=f"http://localhost:{DOCTOR_WEB_PORT}?token={DOCTOR_WEB_TOKEN}",
                ),
            ],
        ])

        await bot.send_message(ADMIN_ID, text, reply_markup=keyboard)
        ok(f"Telegram-сповіщення надіслано (id {ADMIN_ID})")

    except Exception as e:
        err(f"Не вдалося надіслати в Telegram: {e}")
    finally:
        await bot.session.close()


# ---------------------------------------------------------------------------
# ГОЛОВНА ЛОГІКА
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="🩺 Doctor v3.0")
    parser.add_argument("--fix", action="store_true", help="Автовиправлення")
    parser.add_argument("--db", action="store_true", help="Міграція БД")
    parser.add_argument("--test", action="store_true", help="Автотести")
    parser.add_argument("--all", action="store_true", help="Усе разом")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-tg", action="store_true", help="Без Telegram")
    parser.add_argument("--quiet", action="store_true", help="Тихо")
    parser.add_argument("--notify-only", action="store_true",
                        help="Тільки надіслати останній звіт")
    parser.add_argument("--web", action="store_true", help="Запустити Web-панель")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # --- Тільки надіслати останній звіт ---
    if args.notify_only:
        if Path(JSON_REPORT).exists():
            with open(JSON_REPORT, encoding="utf-8") as f:
                data = json.load(f)
            # реконструюємо Report
            report = Report()
            report.start_time = datetime.fromisoformat(data["start_time"])
            report.end_time = datetime.fromisoformat(data["end_time"])
            report.checks = data["checks"]
            report.fixes = data["fixes"]
            report.tests = data["tests"]
            await send_telegram_report(report)
        else:
            err(f"{JSON_REPORT} не існує — спочатку запусти doctor.py")
        return

    report = Report()

    print(f"\n{C.BOLD}{C.CYAN}🩺 DOCTOR v3.0{C.RESET}")
    print(f"{C.CYAN}Папка: {Path.cwd()}{C.RESET}")
    print(f"{C.CYAN}Час: {report.start_time.strftime('%d.%m.%Y %H:%M:%S')}{C.RESET}")

    # --- Перевірки ---
    check_files(report)
    check_env(report)
    check_packages(report)
    check_syntax(report)
    check_long_lines(report)
    check_unused_imports(report)
    check_await_usage(report)
    check_duplicate_functions(report)
    check_bare_except(report)
    check_print_usage(report)
    check_db_exists(report)
    await check_db_schema(report)

    # --- Виправлення ---
    if args.all or args.fix:
        auto_format_code(report)
    if args.all or args.db:
        await auto_migrate_db(report)

    # --- Тести ---
    if args.all or args.test:
        await run_tests(report)

    report.finish()

    # --- Звіти ---
    generate_json_report(report)
    if not args.json:
        generate_html_report(report)
    save_history(report)

    # --- Консоль ---
    print(f"\n{C.BOLD}{'═' * 50}{C.RESET}")
    print(f"{C.BOLD}📊 ЗВІТ{C.RESET}")
    print(f"{'═' * 50}")
    print(f"Тривалість: {report.duration:.2f}с")
    print(f"Перевірок: {report.passed}✅ / {report.warnings}⚠️  / {report.errors}❌")
    print(f"Загальний стан: {C.BOLD}{report.score}%{C.RESET}")

    # --- Telegram ---
    if not args.no_tg:
        await send_telegram_report(report, quiet=args.quiet)

    # --- Web-панель ---
    if args.web:
        info(f"Запускаю Web-панель на http://localhost:{DOCTOR_WEB_PORT}")
        from doctor_web import run_web_server
        await run_web_server(port=DOCTOR_WEB_PORT)

    # --- Відкриваємо HTML ---
    elif not args.json and not args.no_browser:
        try:
            html_path = Path(HTML_REPORT).absolute()
            webbrowser.open(f"file://{html_path}")
            info(f"Відкриваю звіт: {html_path}")
        except Exception as e:
            warn(f"Не вдалося відкрити браузер: {e}")

    print(f"\n{C.CYAN}📄 Файли:{C.RESET}")
    print(f"  • {HTML_REPORT}")
    print(f"  • {JSON_REPORT}")
    print(f"  • {HISTORY_FILE}")

    return 0 if report.errors == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Перервано{C.RESET}")
        sys.exit(130)