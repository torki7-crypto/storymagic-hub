import os
import io
import uuid
from typing import List, Optional
from pydantic import BaseModel, Field
from PIL import Image
from ebooklib import epub

from fastapi import FastAPI, Response, HTTPException, Form, UploadFile, File
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

class ChapterData(BaseModel):
    chapter_number: int
    title: str
    paragraphs: List[str]

class StoryBookData(BaseModel):
    title: str
    language_code: str = "en"
    author: str = "StoryMagic Hub"
    description: Optional[str] = ""
    chapters: List[ChapterData]


# ==========================================
# 2. Модуль сборки ePub3
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

        book_id = str(uuid.uuid4())
        book.set_identifier(f"urn:uuid:{book_id}")
        book.set_title(story.title)
        book.set_language(lang)
        book.add_author(story.author)

        if story.description:
            book.add_metadata("DC", "description", story.description)

        css_item = epub.EpubItem(
            uid="style_main",
            file_name="style/main.css",
            media_type="text/css",
            content=cls.CSS_STYLES.encode("utf-8")
        )
        book.add_item(css_item)

        spine = ["nav"]
        toc = []

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

        buffer = io.BytesIO()
        epub.write_epub(buffer, book, {})
        return buffer.getvalue()


# ==========================================
# 3. Маршруты API для формы и экспорта
# ==========================================

@app.post("/api/generate")
async def generate_book(
    hero_name: str = Form(...),
    age: int = Form(...),
    theme: str = Form(...),
    style: str = Form("Watercolor / Fairy Tale"),
    lang: str = Form("en"),
    photo: Optional[UploadFile] = File(None)
):
    """Принимает FormData с фронтенда, генерирует страницы сказки через Gemini и отдает разворот книги."""
    try:
        lang_names = {
            "en": "English",
            "ru": "Russian",
            "pl": "Polish",
            "ro": "Romanian",
            "it": "Italian",
            "tr": "Turkish"
        }
        target_lang = lang_names.get(lang.lower(), "English")

        prompt = f"""
You are a warm, imaginative children's book author. Write an engaging, age-appropriate therapeutic bedtime story for a {age}-year-old child named {hero_name}.
Story Theme / Emotional Growth: {theme}.
Art Style: {style}.
Language: Write the ENTIRE story (title, chapter names, and all text) strictly in {target_lang}.

Requirements:
1. Divide the story into 4 connected pages/chapters (Page 1 is the beginning/cover setup, Pages 2-3 are the adventure, Page 4 is the gentle triumph/resolution).
2. Each page must contain 2 to 3 comforting, beautifully phrased paragraphs suitable for reading aloud to a child.
3. Keep the tone gentle, encouraging, and magical.
"""

        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StoryBookData,
                temperature=0.7
            )
        )

        story_data = StoryBookData.model_validate_json(response.text)

        # Стандартные красивые иллюстрации под выбранную тему для разворота книги
        theme_images = [
            "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?auto=format&fit=crop&w=1200&q=80",
            "https://images.unsplash.com/photo-1509198397868-475647b2a1e5?auto=format&fit=crop&w=1200&q=80",
            "https://images.unsplash.com/photo-1534447677768-be436bb09401?auto=format&fit=crop&w=1200&q=80",
            "https://images.unsplash.com/photo-1519681393784-d120267933ba?auto=format&fit=crop&w=1200&q=80"
        ]

        pages = []
        for idx, ch in enumerate(story_data.chapters):
            img_url = theme_images[idx % len(theme_images)]
            full_text = "\n\n".join(ch.paragraphs)
            pages.append({
                "page_number": idx + 1,
                "title": ch.title,
                "text": full_text,
                "image_url": img_url
            })

        return {
            "success": True,
            "title": story_data.title,
            "pages": pages
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


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
# 4. Раздача фронтенда (templates/index.html)
# ==========================================

@app.get("/")
async def serve_index():
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "index.html not found"}
