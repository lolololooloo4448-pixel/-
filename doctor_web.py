"""
🌐 Doctor Web Panel — FastAPI-сервер з панеллю перевірок.

Запуск:
    python doctor_web.py
    або
    uvicorn doctor_web:app --host 0.0.0.0 --port 8080

Доступ:
    http://localhost:8080?token=change-me
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
import uvicorn

load_dotenv()

DOCTOR_WEB_PORT = int(os.getenv("DOCTOR_WEB_PORT", "8080"))
DOCTOR_WEB_TOKEN = os.getenv("DOCTOR_WEB_TOKEN", "change-me")

HTML_REPORT = "doctor_report.html"
JSON_REPORT = "doctor_report.json"
HISTORY_FILE = "doctor_history.json"

app = FastAPI(title="🩺 Doctor Web Panel")

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Авторизація
# ---------------------------------------------------------------------------

def check_token(token: str | None):
    if token != DOCTOR_WEB_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------------------------------------------------------------------------
# Головна — HTML-панель
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, token: str = Query(default="")):
    check_token(token)

    # читаємо останній звіт
    report = None
    if Path(JSON_REPORT).exists():
        with open(JSON_REPORT, encoding="utf-8") as f:
            report = json.load(f)

    # читаємо історію
    history = []
    if Path(HISTORY_FILE).exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "token": token,
        "report": report,
        "history": history[-20:],  # останні 20
    })


# ---------------------------------------------------------------------------
# API: отримати останній звіт
# ---------------------------------------------------------------------------

@app.get("/api/report")
async def api_report(token: str = Query(default="")):
    check_token(token)
    if not Path(JSON_REPORT).exists():
        return JSONResponse({"error": "no report"}, status_code=404)
    with open(JSON_REPORT, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# API: історія
# ---------------------------------------------------------------------------

@app.get("/api/history")
async def api_history(token: str = Query(default="")):
    check_token(token)
    if not Path(HISTORY_FILE).exists():
        return []
    with open(HISTORY_FILE, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# API: запустити Doctor
# ---------------------------------------------------------------------------

@app.post("/api/run")
async def api_run(
    token: str = Query(default=""),
    fix: bool = Query(default=False),
    db: bool = Query(default=False),
    test: bool = Query(default=False),
):
    check_token(token)

    cmd = [sys.executable, "doctor.py", "--no-browser", "--json", "--no-tg"]
    if fix:
        cmd.append("--fix")
    if db:
        cmd.append("--db")
    if test:
        cmd.append("--test")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            cwd=Path.cwd(),
        )

        # читаємо оновлений звіт
        report = None
        if Path(JSON_REPORT).exists():
            with open(JSON_REPORT, encoding="utf-8") as f:
                report = json.load(f)

        return {
            "success": result.returncode == 0,
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
            "report": report,
        }
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "timeout"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Завантаження звітів
# ---------------------------------------------------------------------------

@app.get("/download/html")
async def download_html(token: str = Query(default="")):
    check_token(token)
    if not Path(HTML_REPORT).exists():
        raise HTTPException(404, "HTML report not found")
    return FileResponse(HTML_REPORT, filename=f"doctor_{datetime.now():%Y%m%d_%H%M%S}.html")


@app.get("/download/json")
async def download_json(token: str = Query(default="")):
    check_token(token)
    if not Path(JSON_REPORT).exists():
        raise HTTPException(404, "JSON report not found")
    return FileResponse(JSON_REPORT, filename=f"doctor_{datetime.now():%Y%m%d_%H%M%S}.json")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def run_web_server(port: int = DOCTOR_WEB_PORT):
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    print(f"🌐 Doctor Web Panel")
    print(f"   URL: http://localhost:{DOCTOR_WEB_PORT}?token={DOCTOR_WEB_TOKEN}")
    print(f"   Ctrl+C для зупинки\n")
    uvicorn.run(app, host="0.0.0.0", port=DOCTOR_WEB_PORT)