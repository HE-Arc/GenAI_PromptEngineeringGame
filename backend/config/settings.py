import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure")
DEBUG = True
ALLOWED_HOSTS = ["*"]

# Signed cookies avoid the sessions table; no django.contrib.sessions needed.
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

INSTALLED_APPS = ["game"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
LANGUAGE_CODE = "fr"
TIME_ZONE = "Europe/Paris"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "genai")
