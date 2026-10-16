import os
import uuid
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

app = FastAPI(title="StoryMagic Hub")

# Монтирование статики и шаблонов
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Хранилище сессий в памяти (для production рекомендуется Redis/PostgreSQL)
STORY_DATABASE = {}

class Chapter(BaseModel):
    title: str
    text: str
    image_url: Optional[str] = None
    is_locked: bool = False

class StoryPayload(BaseModel):
    child_name: str
    age: int
    theme: str
    language: str = "ru"

@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/generate-story")
async def generate_story(payload: StoryPayload):
    story_id = str(uuid.uuid4())
    
    # Пример структуры, генерируемой Gemini API:
    # 4 главы: 3 открытые, 4-я (финал) маркируется как locked для бесплатных пользователей
    mock_chapters = [
        Chapter(
            title="Глава 1: Волшебный лес",
            text=f"В один прекрасный день {payload.child_name} отправился на поиски древних тайн...",
            image_url="https://images.unsplash.com/photo-1518709268805-4e9042af9f23?w=800&auto=format&fit=crop",
            is_locked=False
        ),
        Chapter(
            title="Глава 2: Хранитель ключей",
            text="Среди вековых дубов послышался тихий шелест крыльев мудрого филина...",
            image_url="https://images.unsplash.com/photo-1544716278-ca5e3f4abd8c?w=800&auto=format&fit=crop",
            is_locked=False
        ),
        Chapter(
            title="Глава 3: Испытание храбрости",
            text="Мост через хрустальную реку требовал разгадать загадку ночного неба...",
            image_url="https://images.unsplash.com/photo-1507525428034-b723cf961d3e?w=800&auto=format&fit=crop",
            is_locked=False
        ),
        Chapter(
            title="Эпилог: Великая победа",
            text=f"И тогда {payload.child_name} понял главное правило дружбы и спас королевство...",
            image_url="https://images.unsplash.com/photo-1451187580459-43490279c0fa?w=800&auto=format&fit=crop",
            is_locked=True # Блокируется до оплаты
        )
    ]
    
    STORY_DATABASE[story_id] = {
        "id": story_id,
        "payload": payload.model_dump(),
        "chapters": [c.model_dump() for c in mock_chapters],
        "is_paid": False
    }
    
    return STORY_DATABASE[story_id]

@app.post("/api/verify-payment/{story_id}")
async def verify_payment(story_id: str):
    if story_id not in STORY_DATABASE:
        raise HTTPException(status_code=404, detail="Книга не найдена")
    
    # Снимаем блокировку после успешного вебхука / подтверждения Stripe/PayPal
    STORY_DATABASE[story_id]["is_paid"] = True
    for chapter in STORY_DATABASE[story_id]["chapters"]:
        chapter["is_locked"] = False
        
    return {"status": "success", "is_paid": True}

@app.get("/api/export-epub/{story_id}")
async def export_epub(story_id: str):
    story = STORY_DATABASE.get(story_id)
    if not story:
        raise HTTPException(status_code=404, detail="Книга не найдена")
        
    if not story.get("is_paid", False):
        raise HTTPException(status_code=402, detail="Требуется оплата тарифа для скачивания ePub")

    # Здесь вызывается ваш модуль компиляции ePub3
    # epub_path = generate_epub_file(story)
    # return FileResponse(epub_path, media_type="application/epub+zip", filename=f"{story['payload']['child_name']}_story.epub")
    return {"status": "ready", "download_url": f"/static/exports/{story_id}.epub"}
