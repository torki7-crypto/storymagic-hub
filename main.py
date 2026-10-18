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
    image_prompt: Optional[str] = ""

class StoryBookData(BaseModel):
    title: str
    language_code: str = "en"
    author: str = "StoryMagic Hub"
    description: Optional[str] = ""
    chapters: List[ChapterData]
    is_paid: Optional[bool] = False


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
        text-indent: 1.3em;
        margin: 0 0 0.8em 0;
        font-size: 1.15em;
    }
    """

    @classmethod
    def create_epub(cls, story: StoryBookData) -> bytes:
        book = epub.EpubBook()
        lang = (story.language_code or "en").lower()

        book_id = str(uuid.uuid4())
        book.set_identifier(f"urn:uuid:{book_id}")
        book.set_title(story.title or "StoryMagic Book")
        book.set_language(lang)
        book.add_author(story.author or "StoryMagic Hub")

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
# 3. Маршруты API (Генерация и Экспорт)
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
    """Основной маршрут для фронтенда (принимает FormData и возвращает страницы книги)."""
    try:
        lang_names = {
            "en": "English",
            "ru": "Russian",
            "pl": "Polish",
            "ro": "Romanian",
            "it": "Italian",
            "tr": "Turkish"
        }
        target_lang = lang_names.get(lang, "English")

        # Если загружено фото ребенка — можем передать его в мультимодальный промпт Gemini
        contents_list = []
        if photo and photo.filename:
            photo_bytes = await photo.read()
            if photo_bytes:
                contents_list.append(
                    types.Part.from_bytes(
                        data=photo_bytes,
                        mime_type=photo.content_type or "image/jpeg"
                    )
                )

        prompt = f"""
You are a warm, imaginative children's therapeutic book author.
Write an engaging, comforting bedtime story for a {age}-year-old child named {hero_name}.
Story Theme: {theme}.
Visual Art Style: {style}.
Language: Write the ENTIRE story (title, chapter titles, and paragraphs) strictly in {target_lang}.

Requirements:
1. Divide the story into exactly 4 chapters (episodes).
2. Each chapter must have a warm title, 2-3 paragraphs of story text, and a short English 'image_prompt' describing the scene for an illustration.
3. Make the final 4th chapter an inspiring climax and happy resolution where {hero_name} overcomes the challenge.
"""
        contents_list.append(prompt)

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents_list,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StoryBookData,
                temperature=0.7
            )
        )

        story_data = StoryBookData.model_validate_json(response.text)

        pages = []
        for idx, ch in enumerate(story_data.chapters):
            full_text = f"«{ch.title}»\n\n" + "\n\n".join(ch.paragraphs)
            # Генерируем яркую иллюстрацию по промпту сцены
            scene_desc = ch.image_prompt or f"{style} children book illustration of {hero_name} in {theme}, chapter {idx+1}"
            encoded_prompt = urllib.parse.quote(f"{style}, cute children book art, {scene_desc}")
            seed = abs(hash(f"{hero_name}_{theme}_{idx}")) % 99999
            img_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=800&height=800&seed={seed}&nologo=true"

            pages.append({
                "page_number": idx + 1,
                "title": ch.title,
                "text": full_text,
                "image_url": img_url
            })

        return {"success": True, "title": story_data.title, "pages": pages}

    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/export-epub")
async def export_epub(story: StoryBookData):
    """Превращает полученную сказку в скачиваемый файл ePub3 (защищено пейволлом)."""
    if not story.is_paid:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment Required: Upgrade to Starter or Unlimited plan to export ePub."
        )

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
# 4. Раздача фронтенда и юридических страниц
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Надежная раздача главной страницы index.html"""
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    return HTMLResponse("<h1>index.html not found in templates folder</h1>", status_code=404)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    """Отдает страницу политики конфиденциальности из папки templates."""
    if os.path.exists("templates/privacy.html"):
        return FileResponse("templates/privacy.html")
    return HTMLResponse("<h1>privacy.html not found in templates folder</h1>", status_code=404)
