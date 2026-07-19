import os
import json
import time

from dotenv import load_dotenv

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware

from google import genai
from google.genai import types
from google.genai.errors import ClientError, ServerError


load_dotenv()


GEMINI_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_KEY)


# ==========================
# МОДЕЛЬ GEMINI
# ==========================

MODEL_NAME = "gemini-2.5-flash-lite"


# ==========================
# FASTAPI
# ==========================

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================
# РЕЖИМЫ
# ==========================

MODES = {
    "male": "мужской образ (лицо, стиль, ухоженность)",
    "female": "женский образ (лицо, стиль, макияж)",
    "general": "общая привлекательность без привязки к полу",
}


# ==========================
# JSON СХЕМА
# ==========================

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "face_visible": {"type": "boolean"},
        "rating": {"type": "number"},
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
        },
        "advice": {
            "type": "array",
            "items": {"type": "string"},
        },
        "summary": {"type": "string"},
    },
    "required": [
        "face_visible",
        "rating",
        "strengths",
        "advice",
        "summary",
    ],
}


# ==========================
# GEMINI ANALYSIS
# ==========================

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
            profile_line = "Дополнительный контекст: " + ", ".join(parts)

    prompt = (
        "Ты — строгий эксперт по анализу фотографий. "
        "ШАГ 1 (обязательный, выполняется первым): проверь изображение и определи, "
        "есть ли на нём настоящее человеческое лицо, которое хорошо и чётко видно. "
        "Установи face_visible=false и rating=0.0, если выполняется хотя бы одно условие: "
        "это скриншот игры, скриншот интерфейса или соцсети, мем, коллаж с текстом, "
        "рисунок или аватар, фотография предмета, животного или пейзажа без человека, "
        "лицо отсутствует в кадре, лицо слишком маленькое или размытое, "
        "лицо закрыто маской, очками с полной непрозрачностью, руками или иным объектом, "
        "либо человек повернут спиной/затылком к камере. "
        "ШАГ 2: только если лицо реально видно и его можно оценить, установи face_visible=true "
        "и продолжи анализ внешности. "
        f"Фокус анализа: {mode_desc}. "
        f"{profile_line} "
        "Отвечай строго на русском языке, все поля JSON должны быть на русском. "
        "Если face_visible=true, дай реалистичную оценку rating от 1.0 до 10.0 "
        "с одной цифрой после запятой, заполни strengths, advice и summary. "
        "Советы (advice) должны быть конкретными: волосы, кожа, стиль, одежда, поза, освещение. "
        "Если face_visible=false, оставь rating=0.0, strengths и advice пустыми массивами, "
        "а summary пустой строкой. "
        "Оценивай только то, что реально видно на фотографии."
    )

    with open(image_path, "rb") as image:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                prompt,
                types.Part.from_bytes(
                    data=image.read(),
                    mime_type="image/jpeg",
                ),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
            ),
        )

    try:
        data = json.loads(response.text)
    except Exception as e:
        print("JSON ERROR:", e)
        return {
            "error": True,
            "message": "⚠️ AI вернул некорректный ответ.",
        }

    # Серверная проверка: если лицо не обнаружено — не отдаём рейтинг
    if not data.get("face_visible"):
        return {
            "error": True,
            "message": "❌ Лицо не обнаружено. Загрузите фото, где лицо хорошо видно.",
        }

    return data


# ==========================
# API
# ==========================

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
        for attempt in range(3):
            try:
                print(f"Gemini попытка {attempt + 1}/3")

                result = analyze_image(temp_file, mode, profile)

                print("Анализ успешный")

                return result

            except ServerError as e:
                print("Gemini 503:", e)

                if attempt < 2:
                    print("Повтор через 3 секунды...")
                    time.sleep(3)
                else:
                    return {
                        "error": True,
                        "message": "⚠️ AI перегружен. Попробуйте позже.",
                    }

            except ClientError as e:
                print("Gemini API ошибка:", e)

                return {
                    "error": True,
                    "message": "⚠️ Лимит AI запросов закончился.",
                }

            except Exception as e:
                print("Ошибка анализа:", e)

                return {
                    "error": True,
                    "message": "⚠️ Ошибка обработки изображения.",
                }

    finally:
        if os.path.exists(temp_file):
            os.remove(temp_file)


# ==========================
# CHECK SERVER
# ==========================

@app.get("/")
def root():
    return {
        "status": "ok",
        "model": MODEL_NAME,
    }