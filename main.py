import os
import io
import uuid
import urllib.parse
from typing import List, Optional
from pydantic import BaseModel
from ebooklib import epub

from fastapi import FastAPI, Response, HTTPException, Form, File, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

# Подключение Gemini API
from google import genai
from google.genai import types

app = FastAPI(title="StoryMagic Hub Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))


# ==========================================
# 1. Модели данных
# ==========================================

class ChapterData(BaseModel):
    chapter_number: int
    title: str
    paragraphs: List[str]
    image_prompt: Optional[str] = ""

class StoryBookData(BaseModel):
    title: str
    language_code: str = "en"
    author: str = "StoryMagic Hub"
    description: Optional[str] = ""
    chapters: List[ChapterData]
    is_paid: Optional[bool] = False


# ==========================================
# 2. Модуль сборки ePub3
# ==========================================

class EpubBuilder:
    CSS_STYLES = """
    body { font-family: sans-serif; line-height: 1.6; margin: 5%; color: #2D3748; }
    h1 { color: #4A5568; text-align: center; }
    p { text-indent: 1.2em; margin-bottom: 0.8em; }
    """

    @classmethod
    def create_epub(cls, story: StoryBookData) -> bytes:
        book = epub.EpubBook()
        lang = (story.language_code or "en").lower()
        book.set_identifier(str(uuid.uuid4()))
        book.set_title(story.title or "StoryMagic Book")
        book.set_language(lang)
        book.add_author(story.author)

        css_item = epub.EpubItem(uid="style_main", file_name="style/main.css", media_type="text/css", content=cls.CSS_STYLES.encode("utf-8"))
        book.add_item(css_item)

        spine = ["nav"]
        toc = []

        for ch in story.chapters:
            paragraphs_html = "".join([f"<p>{p.strip()}</p>" for p in ch.paragraphs if p.strip()])
            chapter_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{ch.title}</title><link rel="stylesheet" href="style/main.css" type="text/css"/></head>
<body><h1>{ch.title}</h1>{paragraphs_html}</body>
</html>"""
            chapter_item = epub.EpubHtml(title=ch.title, file_name=f"chapter_{ch.chapter_number}.xhtml", lang=lang, uid=f"chapter_{ch.chapter_number}")
            chapter_item.content = chapter_xhtml.encode("utf-8")
            chapter_item.add_item(css_item)
            book.add_item(chapter_item)
            spine.append(chapter_item)
            toc.append(chapter_item)

        book.toc = tuple(toc)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = spine

        buffer = io.BytesIO()
        epub.write_epub(buffer, book, {})
        return buffer.getvalue()


# ==========================================
# 2.5. Модуль генерации иллюстраций
# ==========================================

async def generate_illustration(prompt: str, style: str, hero_name: str, age: int) -> str:
    """Генерирует стабильную иллюстрацию в нужном стиле."""
    try:
        clean_desc = f"{style}, a {age}-year-old child named {hero_name}, {prompt}"
        encoded_prompt = urllib.parse.quote(clean_desc)
        seed = abs(hash(prompt)) % 99999
        # Используем стабильный генератор качественных иллюстраций
        return f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=800&height=800&seed={seed}&nologo=true"
    except Exception as e:
        print(f"Image Error: {e}")
        return "https://picsum.photos/seed/magictale/800/800"


# ==========================================
# 3. Маршруты API
# ==========================================

@app.post("/api/generate")
async def generate_book_form(
    hero_name: str = Form("Alice"),
    age: int = Form(7),
    theme: str = Form("Magical journey"),
    style: str = Form("Watercolor children book illustration"),
    lang: str = Form("en"),
    photo: Optional[UploadFile] = File(None)
):
    try:
        lang_names = {
            "en": "English", "ru": "Russian", "pl": "Polish",
            "ro": "Romanian", "it": "Italian", "tr": "Turkish"
        }
        target_lang = lang_names.get(lang, "English")

        # Читаем фото (просто фиксируем факт загрузки для соответствия GDPR)
        if photo and photo.filename:
            await photo.read()

        # Строгий промпт с обязательным возрастом и именем
        prompt = f"""
You are a professional children's therapeutic book author.
Write a warm bedtime story for the main character named {hero_name} who is EXACTLY {age} years old.
Story Theme: {theme}.
Language: Write the ENTIRE story (title, chapters, text) strictly in {target_lang}.

CRITICAL RULE: The child MUST be explicitly referred to as {age}-years-old throughout the text. Do not change the age.

Requirements:
1. Exactly 4 chapters.
2. Each chapter must have a title, 2-3 paragraphs, and a short English 'image_prompt' describing the scene for an illustration.
"""

        gemini_response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StoryBookData,
                temperature=0.7
            )
        )

        story_data = StoryBookData.model_validate_json(gemini_response.text)

        pages = []
        for idx, ch in enumerate(story_data.chapters):
            full_text = f"«{ch.title}»\n\n" + "\n\n".join(ch.paragraphs)
            scene_desc = ch.image_prompt or f"{style} of {hero_name}, chapter {idx+1}"
            
            image_url = await generate_illustration(scene_desc, style, hero_name, age)

            pages.append({
                "page_number": idx + 1,
                "title": ch.title,
                "text": full_text,
                "image_url": image_url
            })

        return {"success": True, "title": story_data.title, "pages": pages}

    except Exception as e:
        print(f"Generation Error: {str(e)}")
        return {"success": False, "error": str(e)}


@app.post("/api/export-epub")
async def export_epub(story: StoryBookData):
    if not story.is_paid:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Payment Required")
    epub_bytes = EpubBuilder.create_epub(story)
    return Response(content=epub_bytes, media_type="application/epub+zip", headers={"Content-Disposition": 'attachment; filename="book.epub"'})


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    if os.path.exists("templates/privacy.html"):
        return FileResponse("templates/privacy.html")
    return HTMLResponse("<h1>privacy.html not found</h1>", status_code=404)
