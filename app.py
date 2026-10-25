import os
import json
import time
import hmac
import hashlib
import secrets
import threading
from pathlib import Path
from urllib.parse import parse_qsl
from io import BytesIO

import requests
import qrcode
from dotenv import load_dotenv

load_dotenv()
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MUSEUMS = DATA / "museums"
STATIC = ROOT / "static"
MUSEUMS.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://127.0.0.1:8000").rstrip("/")
APP_SECRET = os.getenv("APP_SECRET", "change-this-secret")
DEV_ALLOW_NO_TELEGRAM = os.getenv("DEV_ALLOW_NO_TELEGRAM", "0") == "1"

app = FastAPI(title="Muzeycha 3D Museum")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/files", StaticFiles(directory=MUSEUMS), name="files")

def api_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

def tg_call(method, payload=None, timeout=35):
    if not BOT_TOKEN:
        return {}
    try:
        r = requests.post(api_url(method), json=payload or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("Telegram API error:", e)
        return {}

def send_start(chat_id):
    keyboard = {
        "inline_keyboard": [[{
            "text": "ðï¸ Muzey yaratish",
            "web_app": {"url": WEBAPP_URL}
        }]]
    }
    tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": "Assalomu alaykum! ð\nMuzeycha orqali rasmlaringizdan 3D muzey yarating.",
        "reply_markup": keyboard
    })

def polling_loop():
    """Small long-polling bot so the starter project can run as one process."""
    if not BOT_TOKEN:
        print("BOT_TOKEN missing: Telegram bot polling is disabled.")
        return
    offset = None
    print("Telegram bot polling started.")
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(api_url("getUpdates"), params=params, timeout=35)
            r.raise_for_status()
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message") or {}
                text = (msg.get("text") or "").strip()
                chat = msg.get("chat") or {}
                if text.startswith("/start") and chat.get("id"):
                    send_start(chat["id"])
                elif text.startswith("/help") and chat.get("id"):
                    send_start(chat["id"])
        except Exception as e:
            print("Polling error:", e)
            time.sleep(3)

@app.on_event("startup")
def startup():
    threading.Thread(target=polling_loop, daemon=True).start()

def validate_init_data(init_data: str):
    """Validate Telegram WebApp initData signature and return Telegram user data."""
    if not init_data:
        if DEV_ALLOW_NO_TELEGRAM:
            return {"id": 1, "first_name": "Local test"}
        raise HTTPException(401, "Telegram Web App orqali oching.")
    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN sozlanmagan.")
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            raise ValueError("missing hash")
        auth_date = int(pairs.get("auth_date", "0"))
        if abs(time.time() - auth_date) > 24 * 60 * 60:
            raise ValueError("initData expired")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_hash, received_hash):
            raise ValueError("bad signature")
        user = json.loads(pairs.get("user", "{}"))
        if not user.get("id"):
            raise ValueError("missing user")
        return user
    except Exception:
        raise HTTPException(401, "Telegram sessiyasi tasdiqlanmadi. Botni qayta oching.")

def user_from_header(x_telegram_init_data: str | None):
    return validate_init_data(x_telegram_init_data or "")

def museum_path(museum_id):
    # IDs are random URL-safe strings, not user-provided paths.
    if not museum_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in museum_id):
        raise HTTPException(404, "Muzey topilmadi.")
    return MUSEUMS / f"{museum_id}.json"

def read_museum(museum_id):
    p = museum_path(museum_id)
    if not p.exists():
        raise HTTPException(404, "Muzey topilmadi.")
    return json.loads(p.read_text(encoding="utf-8"))

def save_museum(m):
    museum_path(m["id"]).write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")

@app.get("/", response_class=HTMLResponse)
def home():
    return (STATIC / "index.html").read_text(encoding="utf-8")

@app.get("/api/me")
def me(x_telegram_init_data: str | None = Header(default=None)):
    user = user_from_header(x_telegram_init_data)
    return {"id": user["id"], "first_name": user.get("first_name", "Mehmon")}

@app.post("/api/museums")
async def create_museum(
    title: str = Form("Mening muzeyim"),
    photos: list[UploadFile] = File(...),
    x_telegram_init_data: str | None = Header(default=None),
):
    user = user_from_header(x_telegram_init_data)
    if not photos:
        raise HTTPException(400, "Kamida bitta rasm tanlang.")
    if len(photos) > 20:
        raise HTTPException(400, "Bir muzeyga koâpi bilan 20 ta rasm yuklang.")
    museum_id = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12]
    folder = MUSEUMS / museum_id
    folder.mkdir(parents=True, exist_ok=True)
    image_items = []
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    try:
        for i, photo in enumerate(photos):
            ext = allowed.get(photo.content_type or "")
            if not ext:
                raise HTTPException(400, "Faqat JPG, PNG yoki WEBP rasmlar mumkin.")
            content = await photo.read()
            if len(content) > 8 * 1024 * 1024:
                raise HTTPException(400, "Har bir rasm 8 MB dan kichik boâlishi kerak.")
            name = f"{i+1}{ext}"
            (folder / name).write_bytes(content)
            image_items.append({"url": f"/files/{museum_id}/{name}", "caption": photo.filename or f"Rasm {i+1}"})
    except Exception:
        # Keep the code simple; incomplete folders can be deleted manually.
        raise
    museum = {
        "id": museum_id,
        "owner_id": str(user["id"]),
        "title": title.strip()[:80] or "Mening muzeyim",
        "images": image_items,
        "created_at": int(time.time())
    }
    save_museum(museum)
    return {"id": museum_id, "title": museum["title"], "url": f"{WEBAPP_URL}/?museum={museum_id}"}

@app.get("/api/museums")
def list_museums(x_telegram_init_data: str | None = Header(default=None)):
    user = user_from_header(x_telegram_init_data)
    result = []
    for p in MUSEUMS.glob("*.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            if m.get("owner_id") == str(user["id"]):
                result.append({"id": m["id"], "title": m["title"], "url": f"{WEBAPP_URL}/?museum={m['id']}", "count": len(m["images"])})
        except Exception:
            continue
    return {"museums": sorted(result, key=lambda x: x["title"].lower())}

@app.get("/api/museums/{museum_id}")
def get_museum(museum_id: str):
    m = read_museum(museum_id)
    # Public museum endpoint: only public display data is returned.
    return {"id": m["id"], "title": m["title"], "images": m["images"]}

@app.get("/api/museums/{museum_id}/qr.png")
def museum_qr(museum_id: str):
    read_museum(museum_id)
    url = f"{WEBAPP_URL}/?museum={museum_id}"
    img = qrcode.make(url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(buf, media_type="image/png")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
