import asyncio
import base64
import io
import os
from typing import Dict, List, Optional
import urllib.parse
import uuid

import bcrypt
from ebooklib import epub
import edge_tts
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from google import genai
from google.genai import types
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from weasyprint import HTML

from database import Base, engine, get_db
import models

app = FastAPI(title="SuperMeStory Backend")

# Создает таблицы в PostgreSQL при запуске сервиса
Base.metadata.create_all(bind=engine)

# Автоматическое добавление колонок, если таблица была создана ранее без них
with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE saved_stories ADD COLUMN IF NOT EXISTS stripe_session_id VARCHAR;"))
        conn.execute(text("ALTER TABLE saved_stories ADD COLUMN IF NOT EXISTS plan_tier VARCHAR DEFAULT 'digital';"))
        conn.commit()
    except Exception as e:
        print(f"Schema check warning: {e}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

SQUARE_ACCESS_TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN", "")
SQUARE_ENVIRONMENT = os.environ.get("SQUARE_ENVIRONMENT", "sandbox")
SQUARE_LOCATION_ID = os.environ.get("SQUARE_LOCATION_ID", "")

SQUARE_BASE_URL = (
    "https://connect.squareupsandbox.com"
    if SQUARE_ENVIRONMENT.lower() == "sandbox"
    else "https://connect.squareup.com"
)

# Тарифная сетка SuperMeStory в пенсах (GBP)
TIERS_CONFIG = {
    "digital": {
        "name": "SuperMeStory: Full Story Unlock + ePub",
        "amount": 499,   # £4.99
        "shipping": False,
    },
    "audio": {
        "name": "SuperMeStory: Full Story + Audio Narration + ePub",
        "amount": 649,   # £6.49 (Базовая £4.99 + £1.50 за аудио)
        "shipping": False,
    },
    "hardcover": {
        "name": "SuperMeStory: Hardcover Print + Digital + ePub",
        "amount": 1799,  # £17.99
        "shipping": True,
    },
    "vip": {
        "name": "SuperMeStory VIP: Hardcover + Audio + Digital",
        "amount": 1699,  # £16.99 (Спецпредложение)
        "shipping": True,
    },
}


# ==========================================
# 1. Модели данных
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
    author: str = "SuperMeStory"
    description: Optional[str] = Field(default="")
    dedication_text: Optional[str] = Field(default="")
    psychology_questions: List[str] = Field(default_factory=list)
    chapters: List[ChapterData]
    is_paid: Optional[bool] = False


class TTSRequest(BaseModel):
    text: str
    lang: str = "en"


class LuluOrderRequest(BaseModel):
    story: StoryBookData
    contact_email: str
    shipping_address: dict


class AuthRequest(BaseModel):
    name: Optional[str] = ""
    email: str
    password: str


class CheckoutRequest(BaseModel):
    story_id: int
    tier: str = "digital"


class ConfirmPaymentRequest(BaseModel):
    story_id: int


# ==========================================
# 2. Модуль сборки ePub3
# ==========================================


class EpubBuilder:
    CSS_STYLES = """
    body { font-family: Georgia, serif; line-height: 1.6; margin: 5%; color: #2D3748; }
    h1 { color: #4A5568; font-size: 1.3em; text-align: left; margin-bottom: 0.6em; margin-top: 0.2em; border-bottom: 1px solid #E2E8F0; padding-bottom: 0.3em; }
    h2 { color: #4A5568; text-align: center; margin-top: 1.5em; }
    .img-container { text-align: center; margin: 1.2em 0; }
    img { max-width: 100%; height: auto; border-radius: 12px; }
    p { text-indent: 1.2em; margin-bottom: 0.8em; font-size: 1.05em; }
    .dedication-page { text-align: center; font-style: italic; margin-top: 4em; padding: 2em; background: #FAF5FF; border-radius: 12px; }
    .psychology-page { background: #F0FFF4; padding: 1.5em; border-radius: 12px; margin-top: 2em; }
    """

    @classmethod
    async def create_epub(cls, story: StoryBookData) -> bytes:
        book = epub.EpubBook()
        lang = (story.language_code or "en").lower()
        book.set_identifier(str(uuid.uuid4()))
        book.set_title(story.title or "SuperMeStory Book")
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

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
            dedication = (story.dedication_text or "").strip()
            if dedication:
                safe_dedication = dedication.replace("\n", "<br/>")
                dedication_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Dedication</title><link rel="stylesheet" href="style/main.css" type="text/css"/></head>
<body>
    <div class="dedication-page">
        <h2>❤️</h2>
        <p>{safe_dedication}</p>
    </div>
</body>
</html>"""
                dedication_item = epub.EpubHtml(
                    title="Dedication",
                    file_name="dedication.xhtml",
                    lang=lang,
                    uid="dedication",
                )
                dedication_item.content = dedication_xhtml.encode("utf-8")
                dedication_item.add_item(css_item)
                book.add_item(dedication_item)
                spine.append(dedication_item)
                toc.append(dedication_item)

            for idx, ch in enumerate(story.chapters):
                ch_num = ch.chapter_number or (idx + 1)
                img_html = ""
                img_bytes = None

                if ch.image_url and ch.image_url.startswith("data:image"):
                    try:
                        _, b64_data = ch.image_url.split(",", 1)
                        img_bytes = base64.b64decode(b64_data)
                    except Exception as e:
                        print(f"Failed to decode base64 image for chapter {ch_num}: {e}")

                elif ch.image_url and ch.image_url.startswith("http"):
                    try:
                        img_res = await http_client.get(ch.image_url)
                        if img_res.status_code == 200:
                            img_bytes = img_res.content
                    except Exception as e:
                        print(f"Failed to download image for ePub chapter {ch_num}: {e}")

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

                    if idx == 0 and not dedication:
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

            if story.psychology_questions:
                psych_headers = {
                    "ru": (
                        "💡 Вопросы для душевного разговора",
                        "Эти вопросы помогут обсудить сказку с ребенком и закрепить ее терапевтический эффект:",
                    ),
                    "pl": (
                        "💡 Pytania do szczerej rozmowy",
                        "Te pytania pomogą omówić bajkę z dzieckiem i utrwalić jej efekt terapeutyczny:",
                    ),
                    "ro": (
                        "💡 Întrebări pentru o discuție sinceră",
                        "Aceste întrebări vă vor ajuta să discutați povestea cu copilul și să-i consolidați efectul terapeutic:",
                    ),
                    "it": (
                        "💡 Domande per una conversazione intima",
                        "Queste domande aiuteranno a discutere la fiaba con il bambino e a consolidare il suo effetto terapeutico:",
                    ),
                    "tr": (
                        "💡 İçten Bir Sohbet İçin Sorular",
                        "Bu sorular, masalı çocuğunuzla tartışmanıza ve terapötik etkisini pekiştirmenize yardımcı olacaktır:",
                    ),
                }
                default_header = (
                    "💡 Questions for a Heart-to-Heart Talk",
                    "These questions will help discuss the story with your child and reinforce its therapeutic effect:",
                )

                h_title, h_desc = psych_headers.get(lang, default_header)
                questions_html = "".join([f"<li>{q}</li>" for q in story.psychology_questions])
                psych_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Talk with Child</title><link rel="stylesheet" href="style/main.css" type="text/css"/></head>
<body>
    <div class="psychology-page">
        <h2>{h_title}</h2>
        <p>{h_desc}</p>
        <ul>{questions_html}</ul>
    </div>
</body>
</html>"""
                psych_item = epub.EpubHtml(
                    title="Questions for Discussion",
                    file_name="psychology_questions.xhtml",
                    lang=lang,
                    uid="psychology",
                )
                psych_item.content = psych_xhtml.encode("utf-8")
                psych_item.add_item(css_item)
                book.add_item(psych_item)
                spine.append(psych_item)
                toc.append(psych_item)

        book.toc = tuple(toc)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = spine

        buffer = io.BytesIO()
        epub.write_epub(buffer, book, {})
        return buffer.getvalue()


# ==========================================
# 2.3. Генератор Print-Ready PDF для Lulu
# ==========================================

LULU_PDF_STORAGE: Dict[str, bytes] = {}


@app.get("/api/lulu-files/{file_id}.pdf")
async def serve_lulu_pdf(file_id: str):
    pdf_data = LULU_PDF_STORAGE.get(file_id)
    if not pdf_data:
        raise HTTPException(status_code=404, detail="PDF file not found")
    return Response(content=pdf_data, media_type="application/pdf")


def generate_print_pdf_bytes(story: StoryBookData) -> bytes:
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @page {{
                size: 6.25in 9.25in;
                margin-top: 0.75in;
                margin-bottom: 0.75in;
                margin-left: 0.75in;
                margin-right: 0.75in;
                @bottom-center {{
                    content: counter(page);
                    font-family: Georgia, serif;
                    font-size: 10pt;
                    color: #4A5568;
                }}
            }}
            body {{
                font-family: Georgia, serif;
                font-size: 12pt;
                line-height: 1.6;
                color: #2D3748;
            }}
            .page-break {{
                page-break-after: always;
            }}
            h1 {{
                font-size: 16pt;
                color: #2D3748;
                margin-bottom: 0.8em;
                border-bottom: 1px solid #CBD5E1;
                padding-bottom: 0.3em;
            }}
            .img-container {{
                text-align: center;
                margin: 1.0em 0;
            }}
            img {{
                max-width: 100%;
                max-height: 3.8in;
                object-fit: cover;
                border-radius: 8px;
            }}
            p {{
                text-indent: 1.5em;
                margin-bottom: 0.8em;
            }}
            .dedication-page, .psychology-page {{
                page-break-after: always;
                text-align: center;
                padding-top: 2in;
            }}
        </style>
    </head>
    <body>
    """

    dedication = (story.dedication_text or "").strip()
    if dedication:
        html_content += f"""
        <div class="dedication-page">
            <h2>❤️</h2>
            <p style="font-style: italic; font-size: 14pt; text-indent: 0;">{dedication.replace(chr(10), '<br/>')}</p>
        </div>
        """

    for idx, ch in enumerate(story.chapters):
        is_last_chapter = (idx == len(story.chapters) - 1) and not story.psychology_questions
        break_class = "" if is_last_chapter else ' class="page-break"'
        html_content += f"<div{break_class}><h1>{ch.title}</h1>"
        if ch.image_url:
            html_content += f'<div class="img-container"><img src="{ch.image_url}" alt="{ch.title}" /></div>'
        for p in ch.paragraphs:
            html_content += f"<p>{p}</p>"
        html_content += "</div>"

    if story.psychology_questions:
        questions_li = "".join([f"<li>{q}</li>" for q in story.psychology_questions])
        html_content += f"""
        <div class="psychology-page" style="page-break-after: avoid;">
            <h2>💡 Questions & Discussion</h2>
            <ul style="text-align: left; display: inline-block;">{questions_li}</ul>
        </div>
        """

    html_content += "</body></html>"
    return HTML(string=html_content).write_pdf()


def generate_cover_pdf_bytes(story: StoryBookData) -> bytes:
    cover_img = ""
    if story.chapters and story.chapters[0].image_url:
        cover_img = f'<img src="{story.chapters[0].image_url}" style="width: 4.2in; height: 4.2in; object-fit: cover; border-radius: 12px; margin-top: 0.4in;" />'

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @page {{
                size: 12.25in 9.25in;
                margin: 0;
            }}
            body {{
                margin: 0;
                padding: 0;
                font-family: Georgia, serif;
                background: #1e1533;
                color: #ffffff;
                width: 12.25in;
                height: 9.25in;
            }}
            .back-cover {{
                width: 6.125in;
                height: 9.25in;
                padding: 1in;
                box-sizing: border-box;
                text-align: center;
                float: left;
            }}
            .front-cover {{
                width: 6.125in;
                height: 9.25in;
                padding: 1in;
                box-sizing: border-box;
                text-align: center;
                float: left;
            }}
        </style>
    </head>
    <body>
        <div class="back-cover">
            <h3 style="margin-top: 2.5in; color: #fecfef;">SuperMeStory</h3>
            <p style="font-size: 12pt; color: #cbd5e1; line-height: 1.6;">
                A personalized therapeutic story crafted especially to inspire courage, kindness, and confidence.
            </p>
        </div>
        <div class="front-cover">
            <h1 style="font-size: 22pt; color: #ff9a9e; margin-top: 0.5in;">{story.title}</h1>
            {cover_img}
            <p style="margin-top: 0.5in; font-size: 12pt; color: #f1e4ff;">{story.author}</p>
        </div>
    </body>
    </html>
    """
    return HTML(string=html_content).write_pdf()


# ==========================================
# 2.4. Интеграция с Lulu Print API
# ==========================================


async def get_lulu_access_token() -> str:
    is_sandbox = os.environ.get("LULU_ENV", "sandbox") == "sandbox"
    token_url = (
        "https://api.sandbox.lulu.com/auth/realms/glasstree/protocol/openid-connect/token"
        if is_sandbox
        else "https://api.lulu.com/auth/realms/producer/protocol/openid-connect/token"
    )

    client_key = os.environ.get("LULU_CLIENT_KEY", "")
    client_secret = os.environ.get("LULU_CLIENT_SECRET", "")

    credentials = f"{client_key}:{client_secret}"
    encoded_creds = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")

    headers = {
        "Authorization": f"Basic {encoded_creds}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials"}

    async with httpx.AsyncClient() as client_http:
        res = await client_http.post(token_url, headers=headers, data=data)
        if res.status_code == 200:
            return res.json().get("access_token")
        else:
            print(f"Lulu Token Error: {res.text}")
            raise HTTPException(status_code=500, detail="Failed to connect to Lulu API")


# ==========================================
# 2.5. Генерация изображений + Fallback
# ==========================================

image_semaphore = asyncio.Semaphore(3)
CACHED_IMAGE_MODELS: Optional[List[str]] = None


def get_available_image_models() -> List[str]:
    global CACHED_IMAGE_MODELS
    if CACHED_IMAGE_MODELS is not None:
        return CACHED_IMAGE_MODELS

    found = []
    try:
        for m in client.models.list():
            name = getattr(m, "name", "")
            clean_name = name.replace("models/", "")
            if "image" in clean_name.lower() or "imagen" in clean_name.lower():
                found.append(clean_name)
    except Exception as e:
        print(f"Error listing Google models: {e}")

    if not found:
        found = ["gemini-3.8-flash", "imagen-3.0-generate-002"]

    CACHED_IMAGE_MODELS = found
    return found


async def generate_book_illustration(
    prompt: str,
    style: str,
    index: int,
    hero_name: str,
    age: int,
    photo_bytes: Optional[bytes] = None,
    photo_mime: str = "image/jpeg",
) -> str:
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

        contents = []
        if photo_bytes:
            contents.append(types.Part.from_bytes(data=photo_bytes, mime_type=photo_mime))
            full_prompt += (
                " CRITICAL: Preserve the child's exact facial features, hair color, hairstyle, "
                "and eye color from the attached reference photo while rendering them in the requested illustration style!"
            )
        contents.append(full_prompt)

        google_models = await asyncio.to_thread(get_available_image_models)

        for model_name in google_models:
            for modalities in [["TEXT", "IMAGE"], ["IMAGE"]]:
                try:
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model_name,
                        contents=(contents if "gemini" in model_name else [full_prompt]),
                        config=types.GenerateContentConfig(
                            response_modalities=modalities,
                            temperature=0.7,
                        ),
                    )

                    if (
                        response.candidates
                        and response.candidates[0].content
                        and response.candidates[0].content.parts
                    ):
                        for part in response.candidates[0].content.parts:
                            if part.inline_data and part.inline_data.data:
                                if photo_bytes and len(part.inline_data.data) == len(photo_bytes):
                                    continue
                                b64_img = base64.b64encode(part.inline_data.data).decode("utf-8")
                                mime = part.inline_data.mime_type or "image/jpeg"
                                return f"data:{mime};base64,{b64_img}"
                except Exception as e:
                    err_str = str(e)
                    if "404" in err_str or "NOT_FOUND" in err_str:
                        break

        rep_token = os.environ.get("REPLICATE_API_TOKEN")
        if rep_token:
            try:
                headers = {
                    "Authorization": f"Bearer {rep_token}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "input": {
                        "prompt": full_prompt,
                        "aspect_ratio": "1:1",
                        "output_format": "jpg",
                        "output_quality": 90,
                    }
                }
                async with httpx.AsyncClient(timeout=45.0) as client_http:
                    res = await client_http.post(
                        "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
                        headers=headers,
                        json=payload,
                    )
                    if res.status_code in [200, 201, 202]:
                        data = res.json()
                        out = data.get("output")
                        if out:
                            return out[0] if isinstance(out, list) else out
                        poll_url = data.get("urls", {}).get("get")
                        if poll_url:
                            for _ in range(25):
                                await asyncio.sleep(1.5)
                                p_res = await client_http.get(poll_url, headers=headers)
                                if p_res.status_code == 200:
                                    p_data = p_res.json()
                                    if p_data.get("status") == "succeeded":
                                        out = p_data.get("output")
                                        return out[0] if isinstance(out, list) else out
                                    elif p_data.get("status") in ["failed", "canceled"]:
                                        break
            except Exception as e:
                print(f"[Replicate fallback] error: {e}")

        try:
            encoded = urllib.parse.quote(full_prompt[:400])
            flux_url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&seed={index+101}&nologo=true&model=flux"
            async with httpx.AsyncClient(timeout=35.0, follow_redirects=True) as client_http:
                f_res = await client_http.get(flux_url)
                if f_res.status_code == 200 and f_res.content:
                    b64_img = base64.b64encode(f_res.content).decode("utf-8")
                    return f"data:image/jpeg;base64,{b64_img}"
        except Exception as e:
            print(f"[Flux fallback] error: {e}")

    return "https://raw.githubusercontent.com/torki7-crypto/storymagic-hub/main/castle.jpg"


# ==========================================
# 3. Маршруты API
# ==========================================


@app.post("/api/generate")
async def generate_story(
    hero_name: str = Form(...),
    user_id: Optional[int] = Form(None),
    age: int = Form(...),
    theme: str = Form(...),
    style: str = Form(...),
    lang: str = Form("en"),
    custom_dedication: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
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
                    types.Part.from_bytes(data=photo_bytes, mime_type=photo_mime)
                )
                photo_instruction = """
4. PHOTO LIKENESS (CRITICAL): Look closely at the attached photo of the child. Identify their exact physical appearance: gender, hair color, hairstyle/length, eye color, skin tone, and any cute distinctive features. You MUST include this exact physical description at the beginning of EVERY SINGLE chapter's 'image_prompt'!
"""

        dedication_prompt_rule = (
            f"Dedication text provided by parents: '{custom_dedication}'."
            if custom_dedication
            else "Create a short, touching, warm parent dedication text for field 'dedication_text'."
        )

        prompt = f"""
You are an award-winning children's book author and illustrator. Write a single, cohesive, heartwarming story featuring a {age}-year-old main character named {hero_name}.
Story Theme: {theme}.
Visual Style: {style}.
Language: Write the ENTIRE book strictly in {target_lang}.

ADDITIONAL SECTIONS TO GENERATE:
1. {dedication_prompt_rule}
2. 'psychology_questions': Generate exactly 3 gentle, open-ended psychological questions for parents to discuss with a {age}-year-old child at the end of the story based on the theme (written in {target_lang}).

CRITICAL VISUAL & ANATOMY RULES FOR IMAGE PROMPTS:
1. The book must consist of EXACTLY 8 chapters forming ONE continuous plot.
2. Every chapter MUST include a short, vivid, descriptive English 'image_prompt'.
3. The image_prompts must ensure strict visual continuity and adhere to the chosen {style}.
{photo_instruction}
5. ANATOMY & POSE CLARITY: In every 'image_prompt', describe simple, clear, natural poses.

Requirements for each chapter:
- A captivating chapter title.
- 2-3 rich paragraphs of storytelling text.
- A short, vivid English 'image_prompt'.
"""
        contents_for_gemini.append(prompt)

        gemini_response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=contents_for_gemini,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StoryBookData,
                temperature=0.7,
            ),
        )

        story_data = StoryBookData.model_validate_json(gemini_response.text)

        if custom_dedication and custom_dedication.strip():
            story_data.dedication_text = custom_dedication.strip()

        tasks = []
        for idx, ch in enumerate(story_data.chapters):
            scene_desc = ch.image_prompt or f"{style} scene {idx+1} for {hero_name}"
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

        for idx, ch in enumerate(story_data.chapters):
            ch.image_url = image_urls[idx]

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

        # 9-я страница: Терапевтическая беседа и вопросы для родителя
        if story_data.psychology_questions:
            psych_titles = {
                "ru": "💡 Поговорим по душам",
                "en": "💡 Heart-to-Heart Talk",
                "pl": "💡 Rozmowa od serca",
                "ro": "💡 Discuție de la suflet la suflet",
                "it": "💡 Conversazione a cuore aperto",
                "tr": "💡 Kalpten Kalbe Sohbet",
            }
            p_title = psych_titles.get(lang, "💡 Heart-to-Heart Talk")
            questions_text = "\n\n".join([f"✨ {q}" for q in story_data.psychology_questions])
            conclusion_img = image_urls[-1] if image_urls else image_urls[0]

            pages.append(
                {
                    "page_number": len(pages) + 1,
                    "title": p_title,
                    "text": f"«{p_title}»\n\n{questions_text}",
                    "paragraphs": story_data.psychology_questions,
                    "image_url": conclusion_img,
                }
            )

        new_db_story = models.SavedStory(
            title=story_data.title or f"Story for {hero_name}",
            hero_name=hero_name,
            age=age,
            theme=theme,
            user_id=user_id,
            style=style,
            language_code=lang,
            is_paid=False,
            story_data=story_data.model_dump(),
        )
        db.add(new_db_story)
        db.commit()
        db.refresh(new_db_story)

        return {
            "success": True,
            "story_id": new_db_story.id,
            "title": story_data.title,
            "dedication_text": story_data.dedication_text,
            "psychology_questions": story_data.psychology_questions,
            "pages": pages,
        }

    except Exception as e:
        print(f"Generation Error: {str(e)}")
        return {"success": False, "error": str(e)}


@app.get("/api/stories/{story_id}")
async def get_saved_story(story_id: int, db: Session = Depends(get_db)):
    story = db.query(models.SavedStory).filter(models.SavedStory.id == story_id).first()
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    return {
        "success": True,
        "id": story.id,
        "title": story.title,
        "hero_name": story.hero_name,
        "user_id": story.user_id,
        "age": story.age,
        "is_paid": story.is_paid,
        "created_at": story.created_at.isoformat() if story.created_at else "",
        "story_data": story.story_data,
    }


@app.post("/api/create-checkout")
async def create_checkout_session(req: CheckoutRequest, db: Session = Depends(get_db)):
    story = db.query(models.SavedStory).filter(models.SavedStory.id == req.story_id).first()
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")

    tier_key = req.tier.lower()
    if tier_key not in TIERS_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid plan tier selected")

    location_id = os.environ.get("SQUARE_LOCATION_ID")
    access_token = os.environ.get("SQUARE_ACCESS_TOKEN")
    env = os.environ.get("SQUARE_ENVIRONMENT", "sandbox")

    if not location_id or not access_token:
        raise HTTPException(status_code=500, detail="Square credentials not set on server")

    base_api_url = (
        "https://connect.squareupsandbox.com"
        if env.lower() == "sandbox"
        else "https://connect.squareup.com"
    )

    tier_info = TIERS_CONFIG[tier_key]
    base_url = os.environ.get("PUBLIC_BASE_URL", "https://supermestory.com").rstrip("/")

    body = {
        "idempotency_key": str(uuid.uuid4()),
        "quick_pay": {
            "name": f"{story.title} - {tier_info['name']}",
            "price_money": {
                "amount": tier_info["amount"],
                "currency": "GBP",
            },
            "location_id": location_id,
        },
        "checkout_options": {
            "redirect_url": f"{base_url}/?payment=success&story_id={story.id}&tier={tier_key}",
            "ask_for_shipping_address": tier_info["shipping"],
        },
    }

    headers = {
        "Square-Version": "2024-01-18",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as http_client:
        res = await http_client.post(
            f"{base_api_url}/v2/online-checkout/payment-links",
            json=body,
            headers=headers,
        )

    data = res.json()
    if res.status_code in [200, 201] and "payment_link" in data:
        payment_link = data["payment_link"]
        story.stripe_session_id = payment_link.get("id")
        story.plan_tier = tier_key
        db.commit()
        return {
            "success": True,
            "checkout_url": payment_link.get("url") or payment_link.get("long_url"),
        }
    else:
        return {"success": False, "error": data}


@app.post("/api/square-webhook")
async def square_webhook(request: Request, db: Session = Depends(get_db)):
    """Вебхук Square для надежного подтверждения оплаты без участия редиректа браузера"""
    try:
        payload = await request.json()
        event_type = payload.get("type")

        if event_type in ["payment.updated", "order.updated"]:
            payment_obj = payload.get("data", {}).get("object", {}).get("payment", {})
            order_id = payment_obj.get("order_id") or payload.get("data", {}).get("id")
            payment_status = payment_obj.get("status")

            if payment_status == "COMPLETED" or event_type == "order.updated":
                story = db.query(models.SavedStory).filter(
                    models.SavedStory.stripe_session_id == order_id
                ).first()

                if story and not story.is_paid:
                    story.is_paid = True
                    if story.story_data and isinstance(story.story_data, dict):
                        updated = dict(story.story_data)
                        updated["is_paid"] = True
                        story.story_data = updated
                    db.commit()
                    print(f"✅ Webhook: Story {story.id} successfully marked as PAID")

        return {"success": True}
    except Exception as e:
        print(f"⚠️ Webhook error: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/confirm-payment-manual")
async def confirm_payment_manual(req: ConfirmPaymentRequest, db: Session = Depends(get_db)):
    story = db.query(models.SavedStory).filter(models.SavedStory.id == req.story_id).first()
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")

    story.is_paid = True
    if story.story_data and isinstance(story.story_data, dict):
        updated_data = dict(story.story_data)
        updated_data["is_paid"] = True
        story.story_data = updated_data

    db.commit()
    return {"success": True, "message": "Payment confirmed, story unlocked"}


@app.get("/api/my-stories")
async def get_my_stories(user_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(models.SavedStory)
    if user_id:
        query = query.filter(models.SavedStory.user_id == user_id)

    stories = query.order_by(models.SavedStory.created_at.desc()).limit(20).all()
    result = []
    for s in stories:
        cover_image = ""
        if s.story_data and "chapters" in s.story_data and len(s.story_data["chapters"]) > 0:
            cover_image = s.story_data["chapters"][0].get("image_url", "")

        result.append(
            {
                "id": s.id,
                "title": s.title,
                "hero_name": s.hero_name,
                "age": s.age,
                "theme": s.theme,
                "created_at": s.created_at.strftime("%d.%m.%Y %H:%M") if s.created_at else "",
                "cover_image": cover_image,
                "is_paid": s.is_paid,
            }
        )
    return {"success": True, "stories": result}


@app.post("/api/tts")
async def generate_tts(req: TTSRequest):
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
        headers={"Content-Disposition": 'attachment; filename="SuperMeStory_Book.epub"'},
    )


@app.post("/api/export-print-pdf")
async def export_print_pdf(story: StoryBookData):
    if not story.is_paid:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment Required",
        )
    pdf_bytes = generate_print_pdf_bytes(story)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="SuperMeStory_Print_Ready_6x9.pdf"'
        },
    )


@app.post("/api/lulu-create-order")
async def lulu_create_order(req: LuluOrderRequest):
    if not req.story.is_paid:
        raise HTTPException(status_code=402, detail="Payment Required")

    token = await get_lulu_access_token()
    is_sandbox = os.environ.get("LULU_ENV", "sandbox") == "sandbox"
    orders_url = (
        "https://api.sandbox.lulu.com/print-jobs/"
        if is_sandbox
        else "https://api.lulu.com/print-jobs/"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Lulu-Source": "SuperMeStory",
    }

    interior_id = f"interior_{uuid.uuid4().hex}"
    cover_id = f"cover_{uuid.uuid4().hex}"

    LULU_PDF_STORAGE[interior_id] = generate_print_pdf_bytes(req.story)
    LULU_PDF_STORAGE[cover_id] = generate_cover_pdf_bytes(req.story)

    base_url = os.environ.get("PUBLIC_BASE_URL", "https://supermestory.com").rstrip("/")
    interior_url = f"{base_url}/api/lulu-files/{interior_id}.pdf"
    cover_url = f"{base_url}/api/lulu-files/{cover_id}.pdf"

    addr = dict(req.shipping_address)
    if "street1" in addr and addr["street1"]:
        addr["street1"] = str(addr["street1"])[:30]
    if "street2" in addr and addr["street2"]:
        addr["street2"] = str(addr["street2"])[:30]

    pod_package_id = os.environ.get("LULU_POD_PACKAGE_ID", "0600X0900.FC.PRE.SS.080CW444.GXX")

    payload = {
        "contact_email": req.contact_email,
        "shipping_level": "MAIL",
        "shipping_address": addr,
        "line_items": [
            {
                "title": req.story.title,
                "quantity": 1,
                "pod_package_id": pod_package_id,
                "cover": cover_url,
                "interior": interior_url,
            }
        ],
    }

    async with httpx.AsyncClient() as client_http:
        res = await client_http.post(orders_url, headers=headers, json=payload)
        if res.status_code in [200, 201]:
            return {"success": True, "lulu_response": res.json()}
        else:
            print(f"Lulu Order Error: {res.text}")
            raise HTTPException(status_code=400, detail=f"Lulu API Error: {res.text}")


def hash_password(password: str) -> str:
    pwd_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    pwd_bytes = plain_password.encode("utf-8")[:72]
    hash_bytes = hashed_password.encode("utf-8")
    return bcrypt.checkpw(pwd_bytes, hash_bytes)


async def _handle_register(req: AuthRequest, db: Session):
    existing_user = (
        db.query(models.User).filter(models.User.email == req.email.lower()).first()
    )
    if existing_user:
        raise HTTPException(status_code=400, detail="User already exists")

    new_user = models.User(
        email=req.email.lower(),
        name=req.name or req.email.split("@")[0],
        hashed_password=hash_password(req.password),
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return {
        "success": True,
        "user_id": new_user.id,
        "email": new_user.email,
        "name": new_user.name,
    }


async def _handle_login(req: AuthRequest, db: Session):
    user = (
        db.query(models.User).filter(models.User.email == req.email.lower()).first()
    )
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return {
        "success": True,
        "user_id": user.id,
        "email": user.email,
        "name": user.name,
    }


@app.post("/api/register")
@app.post("/api/auth/register")
async def auth_register(req: AuthRequest, db: Session = Depends(get_db)):
    return await _handle_register(req, db)


@app.post("/api/login")
@app.post("/api/auth/login")
async def auth_login(req: AuthRequest, db: Session = Depends(get_db)):
    return await _handle_login(req, db)


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


@app.get("/terms", response_class=HTMLResponse)
async def terms_policy():
    if os.path.exists("templates/terms.html"):
        return FileResponse("templates/terms.html")
    return HTMLResponse("<h1>terms.html not found</h1>", status_code=404)
