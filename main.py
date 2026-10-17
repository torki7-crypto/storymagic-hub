import os
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional

# --- 1. Автоматическая защита: гарантируем наличие нужных папок ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

app = FastAPI(title="StoryMagic Hub")

# Монтируем статику безопасно
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# --- 2. Модели данных ---
class ExportEpubRequest(BaseModel):
    story_id: str
    is_paid: bool = False

# --- 3. Роуты приложения ---
@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "StoryMagic Hub"}

@app.post("/api/export-epub")
async def export_epub(payload: ExportEpubRequest):
    # Пейволл: проверка оплаты перед выдачей ePub файла
    if not payload.is_paid:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Экспорт книги доступен только в платных тарифах (Starter / Unlimited)."
        )
    
    # Генерация/поиск ePub файла (заглушка или сборка книги)
    epub_filename = f"StoryMagic_{payload.story_id}.epub"
    epub_path = os.path.join("/tmp", epub_filename)
    
    # Если файл еще не создан — создаем базовый заглушечный файл
    if not os.path.exists(epub_path):
        with open(epub_path, "wb") as f:
            f.write(b"StoryMagic Hub ePub Content")
            
    return FileResponse(
        epub_path,
        media_type="application/epub+zip",
        filename=epub_filename
    )
