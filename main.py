import asyncio
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
# 2. Модуль сборки ePub3 (С КАРТИНКАМИ!)
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

        # Асинхронно скачиваем все иллюстрации, чтобы вшить их внутрь файла ePub
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True
        ) as http_client:
            for idx, ch in enumerate(story.chapters):
                ch_num = ch.chapter_number or (idx + 1)
                img_html = ""

                if ch.image_url and ch.image_url.startswith("http"):
                    try:
                        img_res = await http_client.get(ch.image_url)
                        if img_res.status_code == 200:
                            img_filename = f"images/chapter_{ch_num}.jpg"
                            img_item = epub.EpubItem(
                                uid=f"img_{ch_num}",
                                file_name=img_filename,
                                media_type="image/jpeg",
                                content=img_res.content,
                            )
                            book.add_item(img_item)
                            img_html = f'<div class="img-container"><img src="{img_filename}" alt="{ch.title}"/></div>'

                            # Ставим первую картинку как обложку книги
                            if idx == 0:
                                book.set_cover("cover.jpg", img_res.content)
                    except Exception as e:
                        print(
                            f"Failed to download image for ePub chapter {ch_num}: {e}"
                        )

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
# 2.5. Быстрый асинхронный генератор Replicate (С ЗАЩИТОЙ АНАТОМИИ)
# ==========================================

replicate_semaphore = asyncio.Semaphore(4)

# Позитивные правила анатомии специально для архитектуры Flux-Schnell
ANATOMY_PROMPT_BOOST = (
    "anatomically correct proportions, natural human and animal anatomy, "
    "child with two arms and two legs in a natural pose, "
    "animals with four well-defined legs, "
    "clean silhouette, clear separation between characters, "
    "masterpiece children's book art, high detail, crisp clean lines"
)


async def generate_book_illustration(
    prompt: str, style: str, index: int, hero_name: str, age: int
) -> str:
    """Генерирует иллюстрацию через Replicate API с поддержкой статуса 202 и защитой пропорций."""
    token = os.environ.get("REPLICATE_API_TOKEN")
    if not token:
        print("ERROR: REPLICATE_API_TOKEN is missing!")
        return "https://images.unsplash.com/photo-1579783902614-a3fb3927b675?w=900&auto=format&fit=crop&q=60"

    safe_prompt = prompt.replace('"', "").replace("'", "")
    full_prompt = (
        f"Children's book illustration in {style} style. "
        f"Main character: cute {age}-year-old child named {hero_name}. "
        f"Scene: {safe_prompt}. "
        f"{ANATOMY_PROMPT_BOOST}, magical storybook atmosphere, vibrant warm colors."
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "input": {
            "prompt": full_prompt,
            "aspect_ratio": "1:1",
            "num_inference_steps": 4,
            "output_format": "jpg",
            "output_quality": 90,
            "go_fast": True,
        }
    }

    async with replicate_semaphore:
        await asyncio.sleep(index * 0.3)

        try:
            print(f"Sending request to Replicate for chapter {index+1}...")
            async with httpx.AsyncClient(timeout=45.0) as client_http:
                response = await client_http.post(
                    "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
                    headers=headers,
                    json=payload,
                )

                if response.status_code in [200, 201, 202]:
                    data = response.json()
                    output = data.get("output")

                    if output:
                        return (
                            output[0] if isinstance(output, list) else output
                        )

                    poll_url = data.get("urls", {}).get("get")
                    if poll_url:
                        for attempt in range(25):
                            await asyncio.sleep(1.5)
                            poll_res = await client_http.get(
                                poll_url, headers=headers
                            )
                            if poll_res.status_code == 200:
                                poll_data = poll_res.json()
                                status_str = poll_data.get("status")
                                if status_str == "succeeded":
                                    out = poll_data.get("output")
                                    return (
                                        out[0]
                                        if isinstance(out, list)
                                        else out
                                    )
                                elif status_str in ["failed", "canceled"]:
                                    break
        except Exception as e:
            print(f"Chapter {index+1} Exception: {str(e)}")

    return "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=900&auto=format&fit=crop&q=60"


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

        if photo and photo.filename:
            photo_bytes = await photo.read()
            if photo_bytes:
                mime_type = photo.content_type or "image/jpeg"
                contents_for_gemini.append(
                    types.Part.from_bytes(data=photo_bytes, mime_type=mime_type)
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
5. ANATOMY & POSE CLARITY: In every 'image_prompt', describe simple, clear, natural poses (e.g., "standing calmly", "sitting at a wooden table", "walking on a forest path", "portrait from waist up"). Avoid complex tangled action poses or overlapping limbs. Explicitly mention clear anatomical structure so the image generator draws the exact number of legs and arms.

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
                    scene_desc, style, idx, hero_name, age
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
