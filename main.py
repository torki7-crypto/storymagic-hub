import asyncio
import base64
import copy
import hashlib
import hmac
import io
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import bcrypt
import edge_tts
import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from google import genai
from google.genai import types
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

import models
from books import (ChapterData, EpubBuilder, StoryBookData,
                   generate_cover_pdf_bytes, generate_print_pdf_bytes)
from database import Base, engine, get_db

app = FastAPI(title="SuperMeStory Backend")
SessionLocal = sessionmaker(bind=engine, autoflush=False)  # for background jobs that outlive a request

Base.metadata.create_all(bind=engine)
with engine.connect() as conn:  # adds columns to a table created by an older version
    try:
        for col in ("stripe_session_id VARCHAR", "plan_tier VARCHAR DEFAULT 'digital'",
                    "paid_tier VARCHAR", "lulu_job_id VARCHAR", "print_status VARCHAR", "shipping_json JSON"):
            conn.execute(text(f"ALTER TABLE saved_stories ADD COLUMN IF NOT EXISTS {col};"))
        conn.commit()
    except Exception as e:
        print(f"Schema check warning: {e}")

# ---------- configuration ----------

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://supermestory.com").rstrip("/")
SESSION_SECRET = os.environ["SESSION_SECRET"]
COOKIE = "sms_session"
FREE_PAGES = 2
DAILY_STORY_LIMIT = int(os.environ.get("DAILY_STORY_LIMIT", "3"))
UNPAID_RETENTION_DAYS = int(os.environ.get("UNPAID_RETENTION_DAYS", "30"))  # unpaid previews are deleted after this
ALLOWED_PRINT_COUNTRIES = {"GB"}
# Books whose first pages are shown to everybody as the example on the Create page (comma-separated ids).
SAMPLE_STORY_IDS = [int(x) for x in re.findall(r"\d+", os.environ.get("SAMPLE_STORY_IDS", ""))]

app.add_middleware(CORSMiddleware, allow_origins=[PUBLIC_BASE_URL], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
TEXT_MODEL = os.environ.get("TEXT_MODEL", "gemini-3.8-flash")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gemini-2.5-flash-image")
IMAGE_MODEL_FALLBACK = os.environ.get("IMAGE_MODEL_FALLBACK", "")

SQUARE_ACCESS_TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN", "")
SQUARE_LOCATION_ID = os.environ.get("SQUARE_LOCATION_ID", "")
SQUARE_BASE_URL = ("https://connect.squareupsandbox.com"
                   if os.environ.get("SQUARE_ENVIRONMENT", "sandbox").lower() == "sandbox"
                   else "https://connect.squareup.com")

# Prices in pence (GBP). Change them here only.
TIERS_CONFIG = {
    "digital": {"name": "SuperMeStory: Full Story Unlock + ePub", "amount": 499, "shipping": False},
    "audio": {"name": "SuperMeStory: Full Story + Audio Narration + ePub", "amount": 649, "shipping": False},
    "hardcover": {"name": "SuperMeStory: Printed Book + ePub", "amount": 1499, "shipping": True},
    "vip": {"name": "SuperMeStory: Printed Book + Audio + ePub", "amount": 1649, "shipping": True},
}

LANG_NAMES = {"en": "English", "ru": "Russian", "pl": "Polish", "ro": "Romanian", "it": "Italian", "tr": "Turkish"}
PSYCH_TITLES = {"ru": "💡 Поговорим по душам", "en": "💡 Heart-to-Heart Talk", "pl": "💡 Rozmowa od serca",
                "ro": "💡 Discuție de la suflet la suflet", "it": "💡 Conversazione a cuore aperto",
                "tr": "💡 Kalpten Kalbe Sohbet"}
VOICES = {"en": "en-US-AnaNeural", "ru": "ru-RU-SvetlanaNeural", "pl": "pl-PL-ZofiaNeural",
          "ro": "ro-RO-AlinaNeural", "it": "it-IT-ElsaNeural", "tr": "tr-TR-EmelNeural"}


# ---------- request/response schemas ----------

class GeneratedBook(BaseModel):  # what the text model returns (no payment or service fields)
    title: str
    dedication_text: str = ""
    psychology_questions: List[str] = []
    chapters: List[ChapterData]


class TTSRequest(BaseModel):
    story_id: int
    page_index: int


class LuluOrderRequest(BaseModel):
    story_id: int
    contact_email: str
    shipping_address: dict


class AuthRequest(BaseModel):
    name: Optional[str] = ""
    email: str
    password: str


class CheckoutRequest(BaseModel):
    story_id: int
    tier: str = "digital"
    contact_email: Optional[str] = ""          # only for print editions
    shipping_address: Optional[dict] = None    # only for print editions


# ---------- sessions and access helpers ----------

def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def set_session_cookie(resp: Response, user_id: int):
    exp = int(time.time()) + 30 * 86400
    body = f"{user_id}.{exp}"
    resp.set_cookie(COOKIE, f"{body}.{_sign(body)}", max_age=30 * 86400,
                    httponly=True, secure=True, samesite="lax")


def current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    try:
        uid, exp, sig = (request.cookies.get(COOKIE) or "").split(".")
        ok = hmac.compare_digest(sig, _sign(f"{uid}.{exp}")) and int(exp) > time.time()
    except Exception:
        ok = False
    user = db.get(models.User, int(uid)) if ok else None
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in")
    return user


def owned_story(story_id: int, user: models.User, db: Session) -> models.SavedStory:
    s = db.get(models.SavedStory, story_id)
    if not s or s.user_id != user.id:
        raise HTTPException(status_code=404, detail="Story not found")
    return s


def merge_tier(old: Optional[str], new: str) -> str:
    audio = old in ("audio", "vip") or new in ("audio", "vip")
    prn = old in ("hardcover", "vip") or new in ("hardcover", "vip")
    return "vip" if (audio and prn) else "hardcover" if prn else "audio" if audio else "digital"


def can(story: models.SavedStory, feature: str) -> bool:
    t = story.paid_tier
    if not story.is_paid or not t:
        return False
    return {"epub": True, "audio": t in ("audio", "vip"), "print": t in ("hardcover", "vip")}[feature]


def tier_prices(paid_tier: Optional[str]) -> dict:
    """What each edition costs this book right now: the full price, or only the difference once something is owned."""
    base = TIERS_CONFIG[paid_tier]["amount"] if paid_tier in TIERS_CONFIG else 0
    out = {}
    for key in TIERS_CONFIG:
        target = merge_tier(paid_tier, key)
        due = TIERS_CONFIG[target]["amount"] - base
        out[key] = {"due": max(0, due), "owned": bool(paid_tier) and target == paid_tier}
    return out


def clean_shipping(addr: Optional[dict], email: Optional[str]) -> dict:
    """Validates a UK delivery address for Lulu; raises 400 with a readable message."""
    addr = addr or {}
    a = {k: str(addr.get(k, "") or "").strip()[:100]
         for k in ("name", "street1", "street2", "city", "postcode", "country_code", "phone_number")}
    a["country_code"] = (a["country_code"] or "GB").upper()
    if a["country_code"] not in ALLOWED_PRINT_COUNTRIES:
        raise HTTPException(status_code=400, detail="Printing is currently available in the UK only")
    if not all(a[k] for k in ("name", "street1", "city", "postcode", "phone_number")):
        raise HTTPException(status_code=400, detail="Please fill in the name, phone, street, city and postcode")
    a["street1"], a["street2"] = a["street1"][:30], a["street2"][:30]
    if not a["street2"]:
        del a["street2"]
    email = (email or "").strip()[:200]
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Please enter a valid contact email")
    return {"contact_email": email, "shipping_address": a}


def images_ready(s: models.SavedStory) -> bool:
    chapters = (s.story_data or {}).get("chapters", [])
    return bool(chapters) and all(c.get("image_url") for c in chapters)


def images_done(s: models.SavedStory) -> int:
    return sum(1 for c in (s.story_data or {}).get("chapters", []) if c.get("image_url"))


def story_pages(s: models.SavedStory) -> list:
    d = s.story_data or {}
    pages = [{"page_number": i + 1, "title": c["title"],
              "text": f"«{c['title']}»\n\n" + "\n\n".join(c["paragraphs"]),
              "paragraphs": c["paragraphs"], "image_url": c.get("image_url", "")}
             for i, c in enumerate(d.get("chapters", []))]
    qs = d.get("psychology_questions") or []
    if qs and pages:
        t = PSYCH_TITLES.get(s.language_code, PSYCH_TITLES["en"])
        pages.append({"page_number": len(pages) + 1, "title": t,
                      "text": "\n\n".join(f"✨ {q}" for q in qs),
                      "paragraphs": qs, "image_url": pages[-1]["image_url"]})
    return pages


def lock_pages(pages: list, is_paid: bool) -> list:
    """Unpaid books only expose the first FREE_PAGES pages; the rest never leave the server."""
    if is_paid:
        return pages
    cover = pages[0]["image_url"] if pages else ""
    return pages[:FREE_PAGES] + [{"page_number": p["page_number"], "title": "", "text": "",
                                  "paragraphs": [], "image_url": cover, "locked": True}
                                 for p in pages[FREE_PAGES:]]


# ---------- auth ----------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))


@app.post("/api/auth/register")
async def auth_register(req: AuthRequest, response: Response, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    if "@" not in email or len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Enter a valid email and a password of at least 8 characters")
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(status_code=400, detail="User already exists")
    user = models.User(email=email, name=(req.name or email.split("@")[0])[:60],
                       hashed_password=hash_password(req.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    set_session_cookie(response, user.id)
    return {"success": True, "user_id": user.id, "email": user.email, "name": user.name}


@app.post("/api/auth/login")
async def auth_login(req: AuthRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == req.email.strip().lower()).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    set_session_cookie(response, user.id)
    return {"success": True, "user_id": user.id, "email": user.email, "name": user.name}


@app.get("/api/auth/me")
async def auth_me(user: models.User = Depends(current_user)):
    return {"success": True, "user_id": user.id, "email": user.email, "name": user.name}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"success": True}


# ---------- illustrations ----------

image_semaphore = asyncio.Semaphore(3)


def to_jpeg_data_url(raw: bytes, mime: Optional[str]) -> str:
    """Pictures are stored as ~150-250 KB JPEGs instead of ~1.5 MB PNGs: the database stays small and the
    ePub and print PDFs get lighter. Falls back to the original picture if conversion is impossible."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((1280, 1280))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"Image compression skipped: {e}")
        return f"data:{mime or 'image/png'};base64," + base64.b64encode(raw).decode("utf-8")


async def generate_book_illustration(prompt: str, style: str, index: int, hero_name: str, age: int,
                                     photo_bytes: Optional[bytes] = None, photo_mime: str = "image/jpeg",
                                     reference_is_photo: bool = True) -> str:
    scene = prompt.replace('"', "").replace("'", "")
    full_prompt = (
        f"Create a high-quality square children's book illustration in {style} style. "
        f"Main character: cute {age}-year-old child named {hero_name}. Scene: {scene}. "
        "Anatomically correct proportions, natural human and animal anatomy, strictly two arms and two legs "
        "for the child, four legs for animals, clean composition, vibrant warm colors, masterpiece storybook "
        "art, no text or watermarks. It must look like a hand-painted illustration, clearly NOT a photograph "
        "and NOT photorealistic."
    )
    contents = []
    if photo_bytes:
        contents.append(types.Part.from_bytes(data=photo_bytes, mime_type=photo_mime))
        if reference_is_photo:
            full_prompt += (" CRITICAL: The attached photo is ONLY a reference for the child's face, hair color, "
                            "hairstyle and eye color. Keep that likeness, but do NOT copy the photo's background, "
                            "clothing, objects or pose, and do not make the result look like a photo.")
        else:  # the reference is an earlier illustration of this same book
            full_prompt += (" CRITICAL: The attached image is an illustration from the same book. Draw the SAME "
                            "main character: identical face, hair color, hairstyle, eye color, skin tone, age and "
                            "the same art style and color palette. Only the scene and pose change.")
    contents.append(full_prompt)
    models_to_try = [m for m in (IMAGE_MODEL, IMAGE_MODEL_FALLBACK) if m]

    async with image_semaphore:
        await asyncio.sleep(index * 0.4)
        for attempt in range(2):
            for model_name in models_to_try:
                try:
                    resp = await asyncio.to_thread(
                        client.models.generate_content, model=model_name, contents=contents,
                        config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"], temperature=0.7))
                    parts = resp.candidates[0].content.parts if resp.candidates and resp.candidates[0].content else []
                    for part in parts or []:
                        if part.inline_data and part.inline_data.data:
                            return to_jpeg_data_url(part.inline_data.data, part.inline_data.mime_type)
                except Exception as e:
                    print(f"Image error ({model_name}, illustration {index + 1}): {e}")
            await asyncio.sleep(2)
    raise RuntimeError(f"Illustration {index + 1} could not be generated")


def parse_data_url(url: str):
    try:
        head, b64 = url.split(",", 1)
        return base64.b64decode(b64), (head[5:].split(";")[0] or "image/png")
    except Exception:
        return None, "image/png"


_story_locks: Dict[int, asyncio.Lock] = {}
PRINT_RUNNING: set = set()


def story_lock(story_id: int) -> asyncio.Lock:
    return _story_locks.setdefault(story_id, asyncio.Lock())


def is_busy(story_id: int) -> bool:
    return story_lock(story_id).locked() or story_id in PRINT_RUNNING


async def finish_illustrations(story_id: int):
    """After payment: paints the chapters that were left without a picture (the free preview has only 2).
    The first picture of the book is the character reference, so no child photo needs to be stored."""
    async with story_lock(story_id):
        try:
            with SessionLocal() as db:
                s = db.get(models.SavedStory, story_id)
                if not s or not s.is_paid:
                    return
                data = copy.deepcopy(s.story_data)
                chapters = data.get("chapters", [])
                missing = [i for i, c in enumerate(chapters) if not c.get("image_url")]
                if not missing:
                    return
                ref_bytes, ref_mime = parse_data_url(chapters[0].get("image_url", ""))
                results = await asyncio.gather(*[
                    generate_book_illustration(chapters[i].get("image_prompt") or f"{s.style} scene {i + 1}",
                                               s.style or "storybook", i, s.hero_name or "the child", s.age or 7,
                                               ref_bytes, ref_mime, reference_is_photo=False)
                    for i in missing], return_exceptions=True)
                for i, r in zip(missing, results):
                    if isinstance(r, str):
                        chapters[i]["image_url"], chapters[i]["image_prompt"] = r, ""
                    else:
                        print(f"Story {story_id}: illustration {i + 1} failed: {r}")
                s.story_data = data
                db.commit()
        except Exception as e:
            print(f"finish_illustrations error (story {story_id}): {e}")


# ---------- story generation ----------

@app.post("/api/generate")
async def generate_story(
    hero_name: str = Form(...), age: int = Form(...), theme: str = Form(...), style: str = Form(...),
    lang: str = Form("en"), custom_dedication: Optional[str] = Form(None),
    photo: Optional[UploadFile] = File(None),
    user: models.User = Depends(current_user), db: Session = Depends(get_db),
):
    hero_name, theme = hero_name.strip()[:40], theme.strip()[:200]
    age = max(2, min(age, 12))
    dedication = (custom_dedication or "").strip()[:300]
    if db.query(models.SavedStory).filter(
            models.SavedStory.user_id == user.id,
            models.SavedStory.created_at >= datetime.utcnow() - timedelta(days=1)).count() >= DAILY_STORY_LIMIT:
        return {"success": False, "error": "Daily limit reached. Please try again tomorrow."}

    try:
        photo_bytes, photo_mime, contents, photo_rule = None, "image/jpeg", [], ""
        if photo and photo.filename:
            raw = await photo.read()
            if len(raw) > 8 * 1024 * 1024:
                return {"success": False, "error": "Photo is too large (max 8 MB)."}
            if raw:
                photo_bytes, photo_mime = raw, photo.content_type or "image/jpeg"
                contents.append(types.Part.from_bytes(data=photo_bytes, mime_type=photo_mime))
                photo_rule = ("4. PHOTO LIKENESS (CRITICAL): Look closely at the attached photo of the child. "
                              "Identify their exact physical appearance: gender, hair color, hairstyle/length, eye "
                              "color, skin tone, and any distinctive features. Include this exact physical "
                              "description at the beginning of EVERY chapter's 'image_prompt'! Describe ONLY "
                              "the face, hair, eyes and skin tone. Do NOT copy the clothing, objects, background "
                              "or location from the photo, and do NOT set chapter 1 where the photo was taken: "
                              "invent an imaginative fairy-tale scene that fits the theme. Give the child ONE "
                              "simple storybook outfit (describe it in words) and repeat exactly that outfit in "
                              "EVERY image_prompt so it stays the same. The story text may mention at most one "
                              "small real detail from the photo, such as a favourite accessory.")

        ded_rule = (f"Dedication text provided by parents: '{dedication}'." if dedication else
                    "Create a short, touching, warm parent dedication text for field 'dedication_text'.")
        contents.append(f"""
You are an award-winning children's book author and illustrator. Write a single, cohesive, heartwarming story featuring a {age}-year-old main character named {hero_name}.
Story Theme: {theme}.
Visual Style: {style}.
Language: Write the ENTIRE book strictly in {LANG_NAMES.get(lang, "English")}.

ADDITIONAL SECTIONS TO GENERATE:
1. {ded_rule}
2. 'psychology_questions': exactly 3 gentle, open-ended questions for parents to discuss with a {age}-year-old child at the end of the story, based on the theme (written in {LANG_NAMES.get(lang, "English")}).

CRITICAL VISUAL & ANATOMY RULES FOR IMAGE PROMPTS:
1. The book must consist of EXACTLY 8 chapters forming ONE continuous plot. Number them 1 to 8.
2. Every chapter MUST include a short, vivid, descriptive English 'image_prompt'.
3. The image_prompts must ensure strict visual continuity and adhere to the chosen {style}.
{photo_rule}
5. In every 'image_prompt', describe simple, clear, natural poses.
6. Every 'image_prompt' describes an imaginative painted storybook scene, never a photograph or a realistic portrait.

Requirements for each chapter: a captivating title, 2-3 rich paragraphs of storytelling text, and a short vivid English 'image_prompt'.
""")
        resp = await asyncio.to_thread(
            client.models.generate_content, model=TEXT_MODEL, contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=GeneratedBook, temperature=0.7))
        gen = GeneratedBook.model_validate_json(resp.text)
        if len(gen.chapters) != 8:
            raise ValueError(f"Expected 8 chapters, got {len(gen.chapters)}")

        # Only the free preview pages get a picture now; the rest are painted after payment (finish_illustrations).
        urls = await asyncio.gather(*[
            generate_book_illustration(gen.chapters[i].image_prompt or f"{style} scene {i + 1}", style, i,
                                       hero_name, age, photo_bytes, photo_mime)
            for i in range(FREE_PAGES)])
        for i, ch in enumerate(gen.chapters):
            ch.chapter_number = i + 1
            if i < FREE_PAGES:
                ch.image_url, ch.image_prompt = urls[i], ""
            else:
                ch.image_url = ""  # painted after payment; the scene prompt is kept until then

        book = StoryBookData(title=gen.title or f"Story for {hero_name}", language_code=lang,
                             dedication_text=dedication or gen.dedication_text,
                             psychology_questions=gen.psychology_questions, chapters=gen.chapters)
        story = models.SavedStory(title=book.title, hero_name=hero_name, age=age, theme=theme,
                                  user_id=user.id, style=style, language_code=lang, is_paid=False,
                                  story_data=book.model_dump())
        db.add(story)
        db.commit()
        db.refresh(story)
        return {"success": True, "story_id": story.id, "title": book.title,
                "dedication_text": book.dedication_text, "psychology_questions": book.psychology_questions,
                "prices": tier_prices(None), "pages": lock_pages(story_pages(story), False)}
    except Exception as e:
        print(f"Generation Error: {e}")
        return {"success": False, "error": "We couldn't create the book this time. Please try again."}


@app.get("/api/stories/{story_id}")
async def get_saved_story(story_id: int, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    s = owned_story(story_id, user, db)
    d = s.story_data or {}
    return {"success": True, "id": s.id, "title": s.title, "hero_name": s.hero_name,
            "is_paid": s.is_paid, "paid_tier": s.paid_tier,
            "prices": tier_prices(s.paid_tier if s.is_paid else None),
            "images_ready": images_ready(s), "busy": is_busy(s.id),
            "images_done": images_done(s), "images_total": len((s.story_data or {}).get("chapters", [])),
            "print_status": s.print_status, "print_order": s.lulu_job_id,
            "dedication_text": d.get("dedication_text", ""),
            "psychology_questions": d.get("psychology_questions", []),
            "pages": lock_pages(story_pages(s), s.is_paid)}


@app.get("/api/my-stories")
async def get_my_stories(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    rows = (db.query(models.SavedStory).filter(models.SavedStory.user_id == user.id)
            .order_by(models.SavedStory.created_at.desc()).limit(20).all())
    out = []
    for s in rows:
        first = ((s.story_data or {}).get("chapters") or [{}])[0]
        out.append({"id": s.id, "title": s.title, "hero_name": s.hero_name, "age": s.age, "theme": s.theme,
                    "is_paid": s.is_paid, "cover_image": first.get("image_url", ""),
                    "created_at": s.created_at.strftime("%d.%m.%Y %H:%M") if s.created_at else ""})
    return {"success": True, "stories": out}


class DeleteAccountRequest(BaseModel):
    confirm_email: str


@app.delete("/api/stories/{story_id}")
async def delete_story(story_id: int, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    s = owned_story(story_id, user, db)
    if is_busy(s.id):
        raise HTTPException(status_code=409, detail="This book is still being prepared. Please try again in a minute.")
    db.delete(s)
    db.commit()
    return {"success": True}


@app.post("/api/account/delete")
async def delete_account(req: DeleteAccountRequest, response: Response,
                         user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    """Deletes the account and every book in it. The user has to type their email to confirm."""
    if req.confirm_email.strip().lower() != (user.email or "").lower():
        raise HTTPException(status_code=400, detail="The email you typed does not match your account")
    stories = db.query(models.SavedStory).filter(models.SavedStory.user_id == user.id).all()
    if any(is_busy(x.id) for x in stories):
        raise HTTPException(status_code=409, detail="A book is still being prepared. Please try again in a minute.")
    for x in stories:
        db.delete(x)
    db.delete(user)
    db.commit()
    response.delete_cookie(COOKIE)
    return {"success": True}


# ---------- automatic clean-up of old data ----------

def cleanup_old_data() -> None:
    """Unpaid previews and undelivered delivery addresses are not kept longer than UNPAID_RETENTION_DAYS."""
    cutoff = datetime.utcnow() - timedelta(days=UNPAID_RETENTION_DAYS)
    with engine.begin() as conn:
        n = conn.execute(text("DELETE FROM saved_stories WHERE COALESCE(is_paid, false) = false AND created_at < :c "
                              "AND NOT (id = ANY(CAST(:keep AS integer[])))"),
                         {"c": cutoff, "keep": SAMPLE_STORY_IDS}).rowcount
        m = conn.execute(text("UPDATE saved_stories SET shipping_json = NULL "
                              "WHERE shipping_json IS NOT NULL AND created_at < :c"), {"c": cutoff}).rowcount
    print(f"Clean-up: {n} unpaid books deleted, {m} old delivery addresses removed")


async def _cleanup_loop():
    while True:
        try:
            await asyncio.to_thread(cleanup_old_data)
        except Exception as e:
            print(f"Clean-up error: {e}")
        await asyncio.sleep(24 * 3600)


_cleanup_task = None


@app.on_event("startup")
async def start_cleanup():
    global _cleanup_task
    _cleanup_task = asyncio.create_task(_cleanup_loop())


# ---------- payments (Square) ----------

@app.post("/api/create-checkout")
async def create_checkout_session(req: CheckoutRequest, user: models.User = Depends(current_user),
                                  db: Session = Depends(get_db)):
    story = owned_story(req.story_id, user, db)
    tier_key = req.tier.lower()
    if tier_key not in TIERS_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid plan tier selected")
    if not SQUARE_LOCATION_ID or not SQUARE_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="Square credentials not set on server")

    paid_before = story.paid_tier if story.is_paid else None
    target = merge_tier(paid_before, tier_key)
    due = tier_prices(paid_before)[tier_key]["due"]  # full price, or only the difference for an upgrade
    if due <= 0:
        raise HTTPException(status_code=400, detail="You already have this edition")
    if target in ("hardcover", "vip") and not can(story, "print"):
        # the delivery address is kept only until the print order has been sent to Lulu
        story.shipping_json = clean_shipping(req.shipping_address, req.contact_email)

    tier = TIERS_CONFIG[target]
    label = f"Upgrade to {tier['name']}" if paid_before else tier["name"]
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "quick_pay": {"name": f"{story.title} - {label}"[:255],
                      "price_money": {"amount": due, "currency": "GBP"},
                      "location_id": SQUARE_LOCATION_ID},
        "checkout_options": {"redirect_url": f"{PUBLIC_BASE_URL}/?payment=success&story_id={story.id}&from={paid_before or ''}"},
    }
    headers = {"Square-Version": "2024-01-18", "Authorization": f"Bearer {SQUARE_ACCESS_TOKEN}",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as http:
        res = await http.post(f"{SQUARE_BASE_URL}/v2/online-checkout/payment-links", json=body, headers=headers)
    data = res.json()
    if res.status_code in (200, 201) and "payment_link" in data:
        link = data["payment_link"]
        story.stripe_session_id = link.get("order_id")  # the webhook matches payments by this order id
        story.plan_tier = tier_key                     # chosen tier; becomes paid_tier only after verified payment
        db.commit()
        return {"success": True, "checkout_url": link.get("url") or link.get("long_url")}
    print(f"Square checkout error: {data}")
    return {"success": False, "error": "Could not start the payment. Please try again."}


def verify_square_signature(body: bytes, signature: str) -> bool:
    key = os.environ.get("SQUARE_WEBHOOK_SIGNATURE_KEY", "")
    url = os.environ.get("SQUARE_WEBHOOK_URL", f"{PUBLIC_BASE_URL}/api/square-webhook")
    digest = hmac.new(key.encode(), url.encode() + body, hashlib.sha256).digest()
    return bool(key) and hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


@app.post("/api/square-webhook")
async def square_webhook(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    body = await request.body()
    if not verify_square_signature(body, request.headers.get("x-square-hmacsha256-signature", "")):
        raise HTTPException(status_code=403, detail="Bad signature")
    event = json.loads(body)
    pid = event.get("data", {}).get("object", {}).get("payment", {}).get("id")
    if event.get("type") != "payment.updated" or not pid:
        return {"success": True}
    async with httpx.AsyncClient(timeout=20.0) as http:  # ask Square itself instead of trusting the body
        r = await http.get(f"{SQUARE_BASE_URL}/v2/payments/{pid}",
                           headers={"Authorization": f"Bearer {SQUARE_ACCESS_TOKEN}", "Square-Version": "2024-01-18"})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Square lookup failed")  # Square will retry
    pay = r.json().get("payment", {})
    if pay.get("status") != "COMPLETED":
        return {"success": True}
    story = db.query(models.SavedStory).filter(models.SavedStory.stripe_session_id == pay.get("order_id")).first()
    money = pay.get("amount_money", {})
    if not story or story.plan_tier not in TIERS_CONFIG:
        print(f"Square payment {pid} matches no book (order {pay.get('order_id')})")
        return {"success": True}
    paid_before = story.paid_tier if story.is_paid else None
    due = tier_prices(paid_before)[story.plan_tier]["due"]
    if due > 0 and money.get("currency") == "GBP" and money.get("amount") == due:
        story.paid_tier = merge_tier(paid_before, story.plan_tier)
        story.is_paid = True
        wants_print = can(story, "print") and bool(story.shipping_json) and not story.lulu_job_id
        if wants_print:
            story.print_status = "queued"
        db.commit()
        # the remaining pictures are painted, and a print order is sent to Lulu, in the background
        background.add_task(place_print_order if wants_print else finish_illustrations, story.id)
    elif due > 0:
        print(f"Square payment {pid}: amount {money} does not match the {due} due for story {story.id}")
    return {"success": True}


# ---------- paid features ----------

@app.post("/api/tts")
async def generate_tts(req: TTSRequest, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    s = owned_story(req.story_id, user, db)
    if not can(s, "audio"):
        raise HTTPException(status_code=402, detail="Audio edition required")
    pages = story_pages(s)
    if not 0 <= req.page_index < len(pages):
        raise HTTPException(status_code=404, detail="Page not found")
    clean = pages[req.page_index]["text"][:6000].replace("«", "").replace("»", ". ")
    stream = io.BytesIO()
    async for chunk in edge_tts.Communicate(clean, VOICES.get(s.language_code, VOICES["en"]), rate="-10%").stream():
        if chunk["type"] == "audio":
            stream.write(chunk["data"])
    return Response(content=stream.getvalue(), media_type="audio/mpeg")


@app.get("/api/stories/{story_id}/epub")
async def export_epub(story_id: int, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    s = owned_story(story_id, user, db)
    if not can(s, "epub"):
        raise HTTPException(status_code=402, detail="Payment Required")
    if not images_ready(s):
        raise HTTPException(status_code=409, detail="Your illustrations are still being painted. Please try again in a minute.")
    data = await EpubBuilder.create_epub(StoryBookData(**{**s.story_data, "is_paid": True, "hero_name": s.hero_name}))
    return Response(content=data, media_type="application/epub+zip",
                    headers={"Content-Disposition": 'attachment; filename="SuperMeStory_Book.epub"'})


# ---------- printing (Lulu): A5 saddle stitch, 12 pages ----------

LULU_PDF_STORAGE: Dict[str, bytes] = {}
# A5, Premium Color, saddle stitch, 80# coated white, glossy cover. Can be changed in Railway without code.
LULU_POD_PACKAGE_ID = os.environ.get("LULU_POD_PACKAGE_ID", "0583X0827.FC.PRE.SS.080CW444.GXX")


@app.get("/api/lulu-files/{file_id}.pdf")
async def serve_lulu_pdf(file_id: str):
    pdf = LULU_PDF_STORAGE.get(file_id)
    if not pdf:
        raise HTTPException(status_code=404, detail="PDF file not found")
    return Response(content=pdf, media_type="application/pdf")


@app.get("/api/stories/{story_id}/print-pdf")
async def download_print_pdf(story_id: int, part: str = "interior", user: models.User = Depends(current_user),
                             db: Session = Depends(get_db)):
    """The exact PDF files that go to the printer, so the owner can look at them (open in the browser while signed in)."""
    s = owned_story(story_id, user, db)
    if part not in ("interior", "cover"):
        raise HTTPException(status_code=400, detail="part must be 'interior' or 'cover'")
    if not s.is_paid or s.paid_tier not in ("hardcover", "vip"):
        raise HTTPException(status_code=403, detail="The print files are available for books with the print edition.")
    if not images_ready(s):
        raise HTTPException(status_code=409, detail="The illustrations are still being painted. Please try again in a minute.")
    story = StoryBookData(**{**s.story_data, "is_paid": True, "hero_name": s.hero_name})
    make = generate_print_pdf_bytes if part == "interior" else generate_cover_pdf_bytes
    pdf = await asyncio.to_thread(make, story)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="book-{story_id}-{part}.pdf"'})


async def get_lulu_access_token() -> str:
    sandbox = os.environ.get("LULU_ENV", "sandbox") == "sandbox"
    url = ("https://api.sandbox.lulu.com/auth/realms/glasstree/protocol/openid-connect/token" if sandbox
           else "https://api.lulu.com/auth/realms/producer/protocol/openid-connect/token")
    creds = f"{os.environ.get('LULU_CLIENT_KEY', '')}:{os.environ.get('LULU_CLIENT_SECRET', '')}"
    headers = {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode(),
               "Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient() as http:
        res = await http.post(url, headers=headers, data={"grant_type": "client_credentials"})
    if res.status_code != 200:
        print(f"Lulu Token Error: {res.text}")
        raise RuntimeError("Failed to connect to Lulu API")
    return res.json().get("access_token")


async def send_to_lulu(s: models.SavedStory) -> Optional[str]:
    """Builds the A5 interior and cover PDFs, creates the Lulu print job and returns its id (None on failure)."""
    ship = s.shipping_json or {}
    story = StoryBookData(**{**s.story_data, "is_paid": True, "hero_name": s.hero_name})
    token = await get_lulu_access_token()
    sandbox = os.environ.get("LULU_ENV", "sandbox") == "sandbox"
    orders_url = "https://api.sandbox.lulu.com/print-jobs/" if sandbox else "https://api.lulu.com/print-jobs/"

    interior_id, cover_id = f"interior_{uuid.uuid4().hex}", f"cover_{uuid.uuid4().hex}"
    LULU_PDF_STORAGE[interior_id] = await asyncio.to_thread(generate_print_pdf_bytes, story)
    LULU_PDF_STORAGE[cover_id] = await asyncio.to_thread(generate_cover_pdf_bytes, story)
    while len(LULU_PDF_STORAGE) > 60:  # keep memory bounded (Lulu downloads the files shortly after the order)
        LULU_PDF_STORAGE.pop(next(iter(LULU_PDF_STORAGE)))

    payload = {
        "external_id": f"story-{s.id}",
        "contact_email": ship.get("contact_email", ""),
        "shipping_level": "MAIL",
        "shipping_address": ship.get("shipping_address", {}),
        "line_items": [{
            "title": story.title[:255], "quantity": 1,
            "pod_package_id": LULU_POD_PACKAGE_ID,
            "cover": f"{PUBLIC_BASE_URL}/api/lulu-files/{cover_id}.pdf",
            "interior": f"{PUBLIC_BASE_URL}/api/lulu-files/{interior_id}.pdf",
        }],
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Lulu-Source": "SuperMeStory"}
    async with httpx.AsyncClient(timeout=60.0) as http:
        res = await http.post(orders_url, headers=headers, json=payload)
    if res.status_code in (200, 201):
        return str(res.json().get("id"))
    print(f"Lulu Order Error (story {s.id}): {res.status_code} {res.text}")
    return None


async def place_print_order(story_id: int):
    """Runs in the background after payment: makes sure all pictures exist, then orders the print from Lulu.
    The delivery address is deleted as soon as Lulu has accepted the order."""
    if story_id in PRINT_RUNNING:
        return
    PRINT_RUNNING.add(story_id)
    try:
        await finish_illustrations(story_id)
        with SessionLocal() as db:
            s = db.get(models.SavedStory, story_id)
            if not s or not can(s, "print") or s.lulu_job_id or not s.shipping_json:
                return
            job_id = await send_to_lulu(s) if images_ready(s) else None
            if job_id:
                s.lulu_job_id, s.print_status, s.shipping_json = job_id, "placed", None
            else:
                s.print_status = "failed"
            db.commit()
    except Exception as e:
        print(f"place_print_order error (story {story_id}): {e}")
        try:
            with SessionLocal() as db:
                s = db.get(models.SavedStory, story_id)
                if s and not s.lulu_job_id:
                    s.print_status = "failed"
                    db.commit()
        except Exception as e2:
            print(f"place_print_order cleanup error: {e2}")
    finally:
        PRINT_RUNNING.discard(story_id)


@app.post("/api/lulu-create-order")
async def lulu_create_order(req: LuluOrderRequest, background: BackgroundTasks,
                            user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    """For a book whose print edition is already paid: saves the address and sends the order in the background."""
    s = owned_story(req.story_id, user, db)
    if not can(s, "print"):
        raise HTTPException(status_code=402, detail="Print edition required")
    if s.lulu_job_id:
        raise HTTPException(status_code=409, detail="Print order already placed")
    if is_busy(s.id):
        return {"success": True, "status": "queued"}
    s.shipping_json = clean_shipping(req.shipping_address, req.contact_email)
    s.print_status = "queued"
    db.commit()
    background.add_task(place_print_order, s.id)
    return {"success": True, "status": "queued"}


@app.post("/api/stories/{story_id}/resume")
async def resume_story(story_id: int, background: BackgroundTasks,
                       user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    """Restarts unfinished paid work (pictures, print order), e.g. after the server was restarted."""
    s = owned_story(story_id, user, db)
    if not s.is_paid or is_busy(s.id):
        return {"success": True, "started": False}
    if can(s, "print") and s.shipping_json and not s.lulu_job_id:
        s.print_status = "queued"
        db.commit()
        background.add_task(place_print_order, s.id)
        return {"success": True, "started": True}
    if not images_ready(s):
        background.add_task(finish_illustrations, s.id)
        return {"success": True, "started": True}
    return {"success": True, "started": False}


# ---------- example book shown on the Create page ----------

_sample_cache: Dict[tuple, list] = {}
_sample_locks: Dict[tuple, asyncio.Lock] = {}


class SampleChapter(BaseModel):
    title: str
    paragraphs: List[str]


class SampleTranslation(BaseModel):
    chapters: List[SampleChapter]


def pick_sample(db: Session, lang: str) -> Optional[models.SavedStory]:
    """The sample book written in the visitor's language if there is one, otherwise the first available."""
    found = []
    for sid in SAMPLE_STORY_IDS:
        s = db.get(models.SavedStory, sid)
        if s and (s.story_data or {}).get("chapters"):
            found.append(s)
    for s in found:
        if s.language_code == lang:
            return s
    return found[0] if found else None


async def translate_sample(chapters: list, src: str, lang: str) -> Optional[list]:
    payload = [{"title": c["title"], "paragraphs": c["paragraphs"]} for c in chapters]
    prompt = (f"Translate this children's storybook excerpt from {LANG_NAMES.get(src, src)} into {LANG_NAMES[lang]}. "
              "Keep the warm, simple storybook style, keep the same number of chapters and paragraphs, and write the "
              "character names the way they are normally written in the target language. Return JSON only.\n\n"
              + json.dumps(payload, ensure_ascii=False))
    resp = await asyncio.to_thread(
        client.models.generate_content, model=TEXT_MODEL, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json",
                                           response_schema=SampleTranslation, temperature=0.2))
    out = SampleTranslation.model_validate_json(resp.text).chapters
    if len(out) != len(chapters) or any(not c.paragraphs for c in out):
        return None
    return [{"title": c.title, "paragraphs": c.paragraphs} for c in out]


@app.get("/api/sample")
async def get_sample(lang: str = "en", db: Session = Depends(get_db)):
    """First pages of a real book for everybody to look at. The text is translated into the visitor's language
    once and kept in memory; the pictures are the same for every language."""
    lang = lang if lang in LANG_NAMES else "en"
    s = pick_sample(db, lang)
    if not s:
        return {"success": False}
    story_id, src = s.id, (s.language_code or "en")
    chapters = [{"title": c["title"], "paragraphs": c["paragraphs"]}
                for c in (s.story_data or {}).get("chapters", [])[:FREE_PAGES]]
    with_image = [bool(c.get("image_url")) for c in (s.story_data or {}).get("chapters", [])[:FREE_PAGES]]
    db.close()  # do not hold a database connection while the translation runs

    texts, translated = chapters, False
    if lang != src:
        key = (story_id, lang)
        if key not in _sample_cache:
            async with _sample_locks.setdefault(key, asyncio.Lock()):
                if key not in _sample_cache:
                    try:
                        tr = await translate_sample(chapters, src, lang)
                        if tr:
                            _sample_cache[key] = tr
                    except Exception as e:
                        print(f"Sample translation error ({lang}): {e}")
        if key in _sample_cache:
            texts, translated = _sample_cache[key], True

    pages = [{"page_number": i + 1, "title": c["title"],
              "text": f"«{c['title']}»\n\n" + "\n\n".join(c["paragraphs"]),
              "image_url": f"/api/sample/image/{story_id}/{i}" if with_image[i] else ""}
             for i, c in enumerate(texts)]
    return {"success": True, "lang": lang if translated else src, "translated": translated, "pages": pages}


@app.get("/api/sample/image/{story_id}/{n}")
async def sample_image(story_id: int, n: int, db: Session = Depends(get_db)):
    if story_id not in SAMPLE_STORY_IDS or not 0 <= n < FREE_PAGES:
        raise HTTPException(status_code=404, detail="Not found")
    s = db.get(models.SavedStory, story_id)
    chapters = (s.story_data or {}).get("chapters", []) if s else []
    raw, mime = parse_data_url(chapters[n].get("image_url", "")) if n < len(chapters) else (None, "")
    if not raw:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=raw, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})


# ---------- pages ----------

def _page(name: str) -> HTMLResponse:
    path = f"templates/{name}.html"
    return FileResponse(path) if os.path.exists(path) else HTMLResponse(f"<h1>{name}.html not found</h1>", status_code=404)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return _page("index")


@app.get("/castle.jpg")
async def castle_picture():
    """Default picture of the reader before a book exists (used when no example book is set)."""
    for path in ("castle.jpg", "templates/castle.jpg", "static/castle.jpg"):
        if os.path.exists(path):
            return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/media/{name}")
async def media_file(name: str):
    """Intro video for the home page (files live in the repo folder 'media')."""
    allowed = {"hero-wide.mp4": "video/mp4", "hero-square.mp4": "video/mp4",
               "hero-poster-wide.jpg": "image/jpeg", "hero-poster-square.jpg": "image/jpeg"}
    if name not in allowed:
        raise HTTPException(status_code=404, detail="Not found")
    for base in ("media", "static/media", "templates/media", "templates", "."):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return FileResponse(path, media_type=allowed[name], headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    return _page("privacy")


@app.get("/terms", response_class=HTMLResponse)
async def terms_policy():
    return _page("terms")
