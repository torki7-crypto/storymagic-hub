import os
import json
import urllib.parse
import requests
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="StoryMagic Platform")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

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
            status_code=500,
            content={"error": "GEMINI_API_KEY не настроен в переменных Railway"}
        )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}

    prompt = f"""
    Ты профессиональный детский писатель и сказкотерапевт.
    Напиши сказку для ребёнка по имени {hero_name} ({age} лет).
    Тема: "{theme}".
    Сказка должна состоять ровно из 5 страниц.

    Верни ответ СТРОГО в формате чистого JSON:
    [
      {{
        "page_number": 1,
        "text": "Текст страницы (2-3 добрых, интересных предложения на русском)...",
        "image_prompt": "Detailed English prompt for illustration, {style} style, depicting {hero_name}..."
      }}
    ]
    """

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        res_json = response.json()
        raw_text = res_json["candidates"][0]["content"]["parts"][0]["text"]
        pages = json.loads(raw_text)

        # Подставляем ссылки на картинки по готовым промптам
        for p in pages:
            encoded_prompt = urllib.parse.quote(p["image_prompt"])
            p["image_url"] = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=800&height=600&nologo=true"

        return {"success": True, "pages": pages}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
