import os
import json
import base64
import urllib.parse
import requests
from fastapi import FastAPI, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="StoryMagic Platform")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

@app.get("/", response_class=HTMLResponse)
def serve_home():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()

def analyze_photo_features(photo_bytes: bytes, mime_type: str) -> str:
    """Анализирует фото ребёнка через Gemini Vision и возвращает ключевые черты лица/причёски."""
    if not photo_bytes:
        return ""
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    b64_img = base64.b64encode(photo_bytes).decode("utf-8")
    prompt = (
        "Briefly describe only the physical appearance of this child for an illustrator: "
        "hair color, hair style/length, eye color, and facial expression. "
        "Keep it under 25 words in clear English adjectives. Example: "
        "'a 7-year-old child with wavy chestnut brown shoulder-length hair, sparkling dark eyes, and a cheerful smile'."
    )
    
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": b64_img}}
            ]
        }]
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=25)
        res_json = resp.json()
        return res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return ""

@app.post("/api/generate")
async def generate_story(
    hero_name: str = Form("Alice"),
    age: int = Form(7),
    theme: str = Form("Страх темноты и ночных звуков"),
    style: str = Form("Watercolor children book illustration"),
    lang: str = Form("ru"),
    photo: UploadFile = File(None)
):
    if not GEMINI_API_KEY:
        return JSONResponse(
            status_code=400,
            content={"error": "GEMINI_API_KEY is not configured in Railway variables!"}
        )

    # 1. Анализируем фото, если прикреплено
    character_appearance = ""
    if photo and photo.filename:
        photo_bytes = await photo.read()
        if len(photo_bytes) > 0:
            mime = photo.content_type or "image/jpeg"
            character_appearance = analyze_photo_features(photo_bytes, mime)

    if not character_appearance:
        character_appearance = f"a {age}-year-old child named {hero_name}"

    # 2. Генерируем терапевтическую сказку
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}

    lang_instruction = "на русском языке" if lang == "ru" else "in English"

    prompt = f"""
    You are an expert child psychologist and beloved fairy tale author.
    Write a gentle, therapeutic and empowering story for a child named {hero_name} (age: {age} years old).
    Story Theme / Challenge: "{theme}".
    The story must softly and warmly guide the hero to overcome fear, gain confidence, or learn empathy without dry lecturing.
    The story must contain exactly 5 pages.
    Story text language: write {lang_instruction}.

    Return strictly a raw JSON array format:
    [
      {{
        "page_number": 1,
        "text": "Story text for page 1 (2-4 sentences {lang_instruction})...",
        "image_prompt": "Children book illustration of {character_appearance}, {style}, vibrant colors, cute storybook art"
      }}
    ]
    """

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=45)
        res_json = response.json()

        if "error" in res_json:
            error_msg = res_json["error"].get("message", "API Error")
            return JSONResponse(status_code=500, content={"error": f"Gemini API: {error_msg}"})

        candidates = res_json.get("candidates")
        if not candidates:
            return JSONResponse(status_code=500, content={"error": f"No response: {res_json}"})

        raw_text = candidates[0]["content"]["parts"][0]["text"].strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]

        pages = json.loads(raw_text.strip())

        for p in pages:
            clean_prompt = p.get("image_prompt", f"cute storybook {character_appearance}")
            encoded_prompt = urllib.parse.quote(clean_prompt)
            p["image_url"] = f"[https://gen.pollinations.ai/image/](https://gen.pollinations.ai/image/){encoded_prompt}?width=800&height=800&model=flux"

        return {"success": True, "pages": pages}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
