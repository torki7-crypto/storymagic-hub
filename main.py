import os
import json
import urllib.parse
import requests
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="StoryMagic Platform")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

@app.get("/", response_class=HTMLResponse)
def serve_home():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/generate")
def generate_story(
    hero_name: str = Form("Алиса"),
    age: int = Form(7),
    theme: str = Form("Приключения в зачарованном лесу"),
    style: str = Form("Watercolor storybook")
):
    if not GEMINI_API_KEY:
        return JSONResponse(
            status_code=400,
            content={"error": "GEMINI_API_KEY не задан в переменных Railway!"}
        )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}

    prompt = f"""
    Ты профессиональный автор детских книг.
    Напиши добрую иллюстрированную сказку для ребёнка по имени {hero_name} (возраст: {age} лет).
    Тема: "{theme}".
    Стиль иллюстраций: {style}.
    Сказка должна состоять ровно из 5 страниц.

    Верни ответ СТРОГО в формате чистого JSON-массива:
    [
      {{
        "page_number": 1,
        "text": "Текст первой страницы (2-3 предложения на русском)...",
        "image_prompt": "Children book illustration of {hero_name}, {style}, vibrant colors, cute storybook art"
      }}
    ]
    """

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=40)
        res_json = response.json()

        if "error" in res_json:
            error_msg = res_json["error"].get("message", "API Error")
            return JSONResponse(status_code=500, content={"error": f"Gemini API: {error_msg}"})

        candidates = res_json.get("candidates")
        if not candidates:
            return JSONResponse(status_code=500, content={"error": f"Нет ответа: {res_json}"})

        raw_text = candidates[0]["content"]["parts"][0]["text"].strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]

        pages = json.loads(raw_text.strip())

        for p in pages:
            encoded_prompt = urllib.parse.quote(p.get("image_prompt", f"cute storybook {hero_name}"))
            p["image_url"] = f"https://gen.pollinations.ai/image/{encoded_prompt}?width=800&height=600&model=flux"
        return {"success": True, "pages": pages}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
