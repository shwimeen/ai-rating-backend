import os
import json
from dotenv import load_dotenv

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware

from google import genai
from google.genai import types

load_dotenv()

GEMINI_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_KEY)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MODES = {
    "male": "мужской образ (лицо, стиль, ухоженность)",
    "female": "женский образ (лицо, стиль, макияж)",
    "general": "общая привлекательность без привязки к полу",
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "face_visible": {
            "type": "boolean",
            "description": "Виден ли на фото человеческое лицо достаточно чётко для оценки"
        },
        "rating": {
            "type": "integer",
            "description": "Оценка от 1 до 10, либо 0 если лицо не видно"
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1-3 сильные стороны внешности"
        },
        "advice": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1-3 коротких совета по улучшению"
        },
        "summary": {
            "type": "string",
            "description": "Короткий итоговый комментарий (1-2 предложения)"
        }
    },
    "required": [
        "face_visible",
        "rating",
        "strengths",
        "advice",
        "summary"
    ]
}

def analyze_image(image_path, mode_key, profile):

    mode_desc = MODES.get(mode_key, MODES["general"])

    profile_line = ""

    if profile:
        parts = []

        if profile.get("age"):
            parts.append(f"возраст {profile['age']} лет")

        if profile.get("height"):
            parts.append(f"рост {profile['height']} см")

        if profile.get("weight"):
            parts.append(f"вес {profile['weight']} кг")

        if parts:
            profile_line = (
                "Дополнительный контекст: "
                + ", ".join(parts)
            )

    prompt = (
        f"Оцени внешность человека на фото. "
        f"Фокус: {mode_desc}. "
        f"{profile_line}"
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            prompt,
            types.Part.from_bytes(
                data=open(image_path, "rb").read(),
                mime_type="image/jpeg"
            )
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
        )
    )

    return json.loads(response.text)


@app.post("/analyze")
async def analyze(
    photo: UploadFile = File(...),
    mode: str = Form(...),
    age: str = Form(None),
    height: str = Form(None),
    weight: str = Form(None),
):

    temp_file = "temp_photo.jpg"

    with open(temp_file, "wb") as f:
        f.write(await photo.read())

    profile = {
        "age": age,
        "height": height,
        "weight": weight,
    }

    try:
        result = analyze_image(
            temp_file,
            mode,
            profile
        )

        return result

    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)


@app.get("/")
def root():
    return {"status": "ok"}