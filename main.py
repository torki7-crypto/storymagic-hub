import os
import io
import uuid
import base64
import json
from typing import List, Optional
from pydantic import BaseModel
from ebooklib import epub

from fastapi import FastAPI, Response, HTTPException, Form, File, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

# Подключение Google GenAI и Vertex AI
from google import genai
from google.genai import types
import vertexai
from vertexai.preview.vision_models import ImageGenerationModel, Image as VertexImage

app = FastAPI(title="StoryMagic Hub Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Инициализация авторизации Google Cloud из переменной окружения на Railway
CREDENTIALS_JSON = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if CREDENTIALS_JSON:
    try:
        cred_dict = json.loads(CREDENTIALS_JSON)
        project_id = cred_dict.get("project_id")
        
        creds_path = "/tmp/gcp_creds.json"
        with open(creds_path, "w") as f:
            f.write(CREDENTIALS_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
        
        if project_id:
            vertexai.init(project=project_id, location="us-central1")
    except Exception as e:
        print(f"Credentials Init Error: {e}")

# Клиент Gemini для текста
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))


# ==========================================
# 1. Модели данных (8 глав)
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
# 2.5. Генерация через Vertex AI Imagen 3 с фото
# ==========================================

async def generate_vertex_image(prompt: str, style: str, hero_name: str, age: int, photo_bytes: Optional[bytes] = None) -> Optional[str]:
    """Генерирует иллюстрацию через Vertex AI Imagen 3 с учетом загруженного фото."""
    try:
        full_prompt = f"{style}, a {age}-year-old child named {hero_name}, children's book illustration. Scene: {prompt}"
        model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-001")
        
        if photo_bytes:
            source_image = VertexImage(photo_bytes)
            result = model.edit_image(
                base_image=source_image,
                prompt=full_prompt,
                number_of_images=1,
                aspect_ratio="1:1",
                safety_filter_level="block_medium_and_above",
            )
        else:
            result = model.generate_images(
                prompt=full_prompt,
                number_of_images=1,
                aspect_ratio="1:1",
                safety_filter_level="block_medium_and_above",
            )
        
        if result and len(result) > 0:
            image_bytes = result[0]._image_bytes
            encoded_image = base64.b64encode(image_bytes).decode('utf-8')
            return f"data:image/png;base64,{encoded_image}"
            
    except Exception as e:
        print(f"Vertex Imagen 3 Error: {e}")
        return None

    return None


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

        # Читаем байты загруженного фото ВНУТРИ функции
        raw_photo_bytes = None
        if photo and photo.filename:
            raw_photo_bytes = await photo.read()

        prompt = f"""
You are a professional children's therapeutic book author.
Write a warm bedtime story for the main character named {hero_name} who is EXACTLY {age} years old.
Story Theme: {theme}.
Language: Write the ENTIRE story (title, chapters, text) strictly in {target_lang}.

CRITICAL RULES:
1. The child MUST be explicitly referred to as {age}-years-old throughout the text.
2. Divide the story into EXACTLY 8 chapters (episodes) to make a rich, complete book.

Requirements for each chapter:
- A warm title.
- 2 paragraphs of story text.
- A short English 'image_prompt' describing the scene for an illustration featuring {hero_name}.
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
            
            # Передаем байты фото в генератор Vertex AI
            image_url = await generate_vertex_image(scene_desc, style, hero_name, age, raw_photo_bytes)
            
            if not image_url:
                image_url = f"https://picsum.photos/seed/story_{idx}/900/900"

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
