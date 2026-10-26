import asyncio
import base64
import io
import os
from typing import List, Optional
import uuid
from ebooklib import epub
import edge_tts
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
# Подключение Google GenAI
from google import genai
from google.genai import types
import httpx
from pydantic import BaseModel

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
# 1. Модели данных (8 глав)
# ==========================================


class ChapterData(BaseModel):
    chapter_number: int
    title: str
    paragraphs: List[str]
    image_prompt: Optional[str] = ""
    image_url: Optional[str] = ""


class StoryBookData(BaseModel):
    title: str
    language_code: str = "en"
    author: str = "StoryMagic Hub"
    description: Optional[str] = ""
    chapters: List[ChapterData]
    is_paid: Optional[bool] = False


class TTSRequest(BaseModel):
    text: str
    lang: str = "en"


# ==========================================
# 2. Модуль сборки ePub3 (С ПОДДЕРЖКОЙ NANO BANANA И BASE64!)
# ==========================================


class EpubBuilder:
    CSS_STYLES = """
    body { font-family: Georgia, serif; line-height: 1.6; margin: 5%; color: #2D3748; }
    h1 { color: #4A5568; text-align: center; margin-bottom: 1em; }
    .img-container { text-align: center; margin: 1.5em 0; }
    img { max-width: 100%; height: auto; border-radius: 12px; }
    p { text-indent: 1.2em; margin-bottom: 0.8em; font-size: 1.1em; }
    """

    @classmethod
    async def create_epub(cls, story: StoryBookData) -> bytes:
        book = epub.EpubBook()
        lang = (story.language_code or "en").lower()
        book.set_identifier(str(uuid.uuid4()))
        book.set_title(story.title or "StoryMagic Book")
        book.set_language(lang)
        book.add_author(story.author)

        css_item = epub.EpubItem(
            uid="style_main",
            file_name="style/main.css",
            media_type="text/css",
            content=cls.CSS_STYLES.encode("utf-8"),
        )
        book.add_item(css_item)

        spine = ["nav"]
        toc = []

        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True
        ) as http_client:
            for idx, ch in enumerate(story.chapters):
                ch_num = ch.chapter_number or (idx + 1)
                img_html = ""
                img_bytes = None

                # 1. Если картинка пришла от Nano Banana в формате data:image/...;base64
                if ch.image_url and ch.image_url.startswith("data:image"):
                    try:
                        header, b64_data = ch.image_url.split(",", 1)
                        img_bytes = base64.b64decode(b64_data)
                    except Exception as e:
                        print(
                            f"Failed to decode base64 image for chapter {ch_num}: {e}"
                        )

                # 2. Если картинка — обычная http-ссылка (запасной вариант)
                elif ch.image_url and ch.image_url.startswith("http"):
                    try:
                        img_res = await http_client.get(ch.image_url)
                        if img_res.status_code == 200:
                            img_bytes = img_res.content
                    except Exception as e:
                        print(
                            f"Failed to download image for ePub chapter {ch_num}: {e}"
                        )

                if img_bytes:
                    img_filename = f"images/chapter_{ch_num}.jpg"
                    img_item = epub.EpubItem(
                        uid=f"img_{ch_num}",
                        file_name=img_filename,
                        media_type="image/jpeg",
                        content=img_bytes,
                    )
                    book.add_item(img_item)
                    img_html = f'<div class="img-container"><img src="{img_filename}" alt="{ch.title}"/></div>'

                    if idx == 0:
                        book.set_cover("cover.jpg", img_bytes)

                paragraphs_html = "".join(
                    [f"<p>{p.strip()}</p>" for p in ch.paragraphs if p.strip()]
                )
                chapter_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{ch.title}</title><link rel="stylesheet" href="style/main.css" type="text/css"/></head>
<body>
    <h1>{ch.title}</h1>
    {img_html}
    {paragraphs_html}
</body>
</html>"""
                chapter_item = epub.EpubHtml(
                    title=ch.title,
                    file_name=f"chapter_{ch_num}.xhtml",
                    lang=lang,
                    uid=f"chapter_{ch_num}",
                )
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
# 2.5. Генератор иллюстраций Nano Banana (gemini-2.5-flash-image)
# ==========================================

image_semaphore = asyncio.Semaphore(3)


async def generate_book_illustration(
    prompt: str,
    style: str,
    index: int,
    hero_name: str,
    age: int,
    photo_bytes: Optional[bytes] = None,
    photo_mime: str = "image/jpeg",
) -> str:
    """Генерирует иллюстрацию через Nano Banana (gemini-2.5-flash-image) с поддержкой фото ребенка."""
    safe_prompt = prompt.replace('"', "").replace("'", "")
    full_prompt = (
        f"Create a high-quality square children's book illustration in {style} style. "
        f"Main character: cute {age}-year-old child named {hero_name}. "
        f"Scene: {safe_prompt}. "
        f"Anatomically correct proportions, natural human and animal anatomy, "
        f"strictly two arms and two legs for the child, four legs for animals, "
        f"clean composition, vibrant warm colors, masterpiece storybook art, no text or watermarks."
    )

    async with image_semaphore:
        await asyncio.sleep(index * 0.4)
        try:
            print(f"Generating Nano Banana image for chapter {index+1}...")
            contents = []

            # Если загружено фото ребенка — передаем его напрямую в генератор для сходства!
            if photo_bytes:
                contents.append(
                    types.Part.from_bytes(
                        data=photo_bytes, mime_type=photo_mime
                    )
                )
                full_prompt += (
                    " CRITICAL: Preserve the child's exact facial features, hair color, hairstyle, "
                    "and eye color from the attached reference photo while rendering them in the requested illustration style!"
                )

            contents.append(full_prompt)

            response = await asyncio.to_thread(
                client.models.generate_content,
                model="gemini-2.5-flash-image",
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    temperature=0.7,
                ),
            )

            if response.candidates and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        b64_img = base64.b64encode(
                            part.inline_data.data
                        ).decode("utf-8")
                        mime = part.inline_data.mime_type or "image/jpeg"
                        return f"data:{mime};base64,{b64_img}"

        except Exception as e:
            print(f"Nano Banana Chapter {index+1} Exception: {str(e)}")

    return "https://raw.githubusercontent.com/torki7-crypto/storymagic-hub/main/castle.jpg"


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
    photo: Optional[UploadFile] = File(None),
):
    try:
        lang_names = {
            "en": "English",
            "ru": "Russian",
            "pl": "Polish",
            "ro": "Romanian",
            "it": "Italian",
            "tr": "Turkish",
        }
        target_lang = lang_names.get(lang, "English")

        contents_for_gemini = []
        photo_instruction = ""
        photo_bytes = None
        photo_mime = "image/jpeg"

        if photo and photo.filename:
            raw_bytes = await photo.read()
            if raw_bytes:
                photo_bytes = raw_bytes
                photo_mime = photo.content_type or "image/jpeg"
                contents_for_gemini.append(
                    types.Part.from_bytes(
                        data=photo_bytes, mime_type=photo_mime
                    )
                )
                photo_instruction = """
4. PHOTO LIKENESS (CRITICAL): Look closely at the attached photo of the child. Identify their exact physical appearance: gender, hair color, hairstyle/length, eye color, skin tone, and any cute distinctive features (like glasses, curls, freckles). You MUST include this exact physical description at the beginning of EVERY SINGLE chapter's 'image_prompt' so the illustrated character looks just like the child in the photo across all 8 chapters!
"""

        prompt = f"""
You are an award-winning children's book author and illustrator. Write a single, cohesive, heartwarming story featuring a {age}-year-old main character named {hero_name}.
Story Theme: {theme}.
Visual Style: {style} (e.g., Watercolor, Fairy Tale).
Language: Write the ENTIRE book strictly in {target_lang}.

CRITICAL VISUAL & ANATOMY RULES FOR IMAGE PROMPTS:
1. The book must consist of EXACTLY 8 chapters forming ONE continuous plot.
2. Every chapter MUST include a short, vivid, descriptive English 'image_prompt'.
3. The image_prompts must ensure strict visual continuity and adhere to the chosen {style}.
{photo_instruction}
5. ANATOMY & POSE CLARITY: In every 'image_prompt', describe simple, clear, natural poses (e.g., "standing calmly", "sitting at a wooden table", "walking on a forest path"). Avoid complex tangled poses. Explicitly mention clear anatomical structure so the image generator draws the exact number of legs and arms.

Requirements for each chapter:
- A captivating chapter title.
- 2-3 rich paragraphs of storytelling text.
- A short, vivid English 'image_prompt' describing the character's appearance, simple natural pose, and the visual scene.
"""
        contents_for_gemini.append(prompt)

        gemini_response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents_for_gemini,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StoryBookData,
                temperature=0.7,
            ),
        )

        story_data = StoryBookData.model_validate_json(gemini_response.text)

        tasks = []
        for idx, ch in enumerate(story_data.chapters):
            scene_desc = (
                ch.image_prompt or f"{style} scene {idx+1} for {hero_name}"
            )
            tasks.append(
                generate_book_illustration(
                    scene_desc,
                    style,
                    idx,
                    hero_name,
                    age,
                    photo_bytes,
                    photo_mime,
                )
            )

        image_urls = await asyncio.gather(*tasks)

        pages = []
        for idx, ch in enumerate(story_data.chapters):
            full_text = f"«{ch.title}»\n\n" + "\n\n".join(ch.paragraphs)
            pages.append(
                {
                    "page_number": idx + 1,
                    "title": ch.title,
                    "text": full_text,
                    "paragraphs": ch.paragraphs,
                    "image_url": image_urls[idx],
                }
            )

        return {"success": True, "title": story_data.title, "pages": pages}

    except Exception as e:
        print(f"Generation Error: {str(e)}")
        return {"success": False, "error": str(e)}


@app.post("/api/tts")
async def generate_tts(req: TTSRequest):
    """Генерирует живую сказочную озвучку с мягким темпом речи."""
    voices = {
        "en": "en-US-AnaNeural",
        "ru": "ru-RU-SvetlanaNeural",
        "pl": "pl-PL-ZofiaNeural",
        "ro": "ro-RO-AlinaNeural",
        "it": "it-IT-ElsaNeural",
        "tr": "tr-TR-EmelNeural",
    }
    voice = voices.get(req.lang.lower(), "en-US-AnaNeural")

    clean_text = req.text.replace("«", "").replace("»", ". ")

    communicate = edge_tts.Communicate(clean_text, voice, rate="-10%")
    audio_stream = io.BytesIO()

    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_stream.write(chunk["data"])

    return Response(content=audio_stream.getvalue(), media_type="audio/mpeg")


@app.post("/api/export-epub")
async def export_epub(story: StoryBookData):
    if not story.is_paid:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment Required",
        )
    epub_bytes = await EpubBuilder.create_epub(story)
    return Response(
        content=epub_bytes,
        media_type="application/epub+zip",
        headers={
            "Content-Disposition": 'attachment; filename="StoryMagic_Book.epub"'
        },
    )


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
