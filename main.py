import os
import io
import uuid
import json
from typing import List, Optional
from pydantic import BaseModel, Field
from PIL import Image
from ebooklib import epub

from fastapi import FastAPI, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# Подключение Gemini API
from google import genai
from google.genai import types

app = FastAPI(title="StoryMagic Hub Backend")

# Включаем CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Инициализация клиента Gemini
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))


# ==========================================
# 1. Pydantic-модели данных
# ==========================================

class StoryGenerateRequest(BaseModel):
    child_name: str
    child_age: int
    language_code: str = "en"
    theme: Optional[str] = ""

class ChapterData(BaseModel):
    chapter_number: int
    title: str
    paragraphs: List[str]

class StoryBookData(BaseModel):
    title: str
    language_code: str
    author: str = "StoryMagic Hub"
    description: Optional[str] = ""
    chapters: List[ChapterData]


# ==========================================
# 2. Модуль сборки ePub3 книги
# ==========================================

class EpubBuilder:
    CSS_STYLES = """
    @namespace epub "http://www.idpf.org/2007/ops";
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        line-height: 1.65;
        margin: 5%;
        color: #2D3748;
        -webkit-hyphens: auto;
        -moz-hyphens: auto;
        -ms-hyphens: auto;
        hyphens: auto;
        text-align: justify;
    }
    h1 {
        color: #4A5568;
        text-align: center;
        margin-top: 1.2em;
        margin-bottom: 0.8em;
        font-size: 1.6em;
        font-weight: 700;
        page-break-after: avoid;
    }
    p {
        orphans: 2;
        widows: 2;
        text-indent: 1.3em;
        margin: 0 0 0.8em 0;
        font-size: 1.15em;
    }
    """

    @classmethod
    def create_epub(cls, story: StoryBookData) -> bytes:
        book = epub.EpubBook()
        lang = story.language_code.lower()

        # Метаданные книги
        book_id = str(uuid.uuid4())
        book.set_identifier(f"urn:uuid:{book_id}")
        book.set_title(story.title)
        book.set_language(lang)
        book.add_author(story.author)

        if story.description:
            book.add_metadata("DC", "description", story.description)

        # Стили
        css_item = epub.EpubItem(
            uid="style_main",
            file_name="style/main.css",
            media_type="text/css",
            content=cls.CSS_STYLES.encode("utf-8")
        )
        book.add_item(css_item)

        spine = ["nav"]
        toc = []

        # Сборка глав
        for ch in story.chapters:
            paragraphs_html = "".join([f"<p>{p.strip()}</p>" for p in ch.paragraphs if p.strip()])
            chapter_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" lang="{lang}">
<head>
    <meta charset="utf-8"/>
    <title>{ch.title}</title>
    <link rel="stylesheet" href="style/main.css" type="text/css"/>
</head>
<body>
    <h1>{ch.title}</h1>
    {paragraphs_html}
</body>
</html>
"""
            chapter_item = epub.EpubHtml(
                title=ch.title,
                file_name=f"chapter_{ch.chapter_number}.xhtml",
                lang=lang,
                uid=f"chapter_{ch.chapter_number}"
            )
            chapter_item.content = chapter_xhtml.encode("utf-8")
            chapter_item.add_item(css_item)

            book.add_item(chapter_item)
            spine.append(chapter_item)
            toc.append(chapter_item)

        book.toc = tuple(toc)
        book.add_item(epub.EpubNcx())
        
        nav = epub.EpubNav()
        nav.add_item(css_item)
        book.add_item(nav)
        
        book.spine = spine

        # Запись в буфер байтов
        buffer = io.BytesIO()
        epub.write_epub(buffer, book, {})
        return buffer.getvalue()


# ==========================================
# 3. Маршруты API (Endpoints)
# ==========================================

@app.post("/api/generate-story", response_model=StoryBookData)
async def generate_story(req: StoryGenerateRequest):
    """Генерация структурированного текста сказки через Gemini API."""
    try:
        lang_names = {
            "en": "English",
            "ru": "Russian",
            "pl": "Polish",
            "ro": "Romanian",
            "it": "Italian",
            "tr": "Turkish"
        }
        target_lang = lang_names.get(req.language_code, "English")

        prompt = f"""
You are a warm, imaginative children's book author. Write an engaging, age-appropriate bedtime story for a {req.child_age}-year-old child named {req.child_name}.
Topic/Adventure: {req.theme or 'A magical journey with loyal animal friends'}.
Language: Write the ENTIRE story (title, chapter names, and all text) strictly in {target_lang}.

Requirements:
1. Divide the story into 3 or 4 chapters.
2. Each chapter must have a creative title and 2 to 4 paragraphs.
3. Keep the tone gentle, encouraging, and magical.
"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StoryBookData,
                temperature=0.7
            )
        )

        story_data = StoryBookData.model_validate_json(response.text)
        story_data.language_code = req.language_code
        return story_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Story generation failed: {str(e)}")


@app.post("/api/export-epub")
async def export_epub(story: StoryBookData):
    """Превращает полученную сказку в скачиваемый файл ePub3."""
    try:
        epub_bytes = EpubBuilder.create_epub(story)
        safe_title = "".join(c for c in story.title if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        filename = f"{safe_title or 'magical_story'}.epub"

        return Response(
            content=epub_bytes,
            media_type="application/epub+zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ePub build failed: {str(e)}")


# ==========================================
# 4. Раздача фронтенда (поддержка и templates/, и корня)
# ==========================================

@app.get("/")
async def serve_index():
    # Проверяем в папке templates/
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    # Проверяем в корне
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "index.html not found"}
