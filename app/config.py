import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-5.4-mini")

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Pexels
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")

# WordPress
WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")

# App
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
UPLOAD_DIR = BASE_DIR / os.getenv("UPLOAD_DIR", "uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

DATABASE_URL = f"sqlite:///{BASE_DIR / 'database.sqlite'}"

# Prompt
PROMPT_FILE = BASE_DIR / "prompt.txt"
