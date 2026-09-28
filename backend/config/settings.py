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
    # SessionMiddleware is what puts `request.session` on the request. It is middleware, not the
    # app: with the signed_cookies engine it needs no INSTALLED_APPS entry, no table, no migration.
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
]
APPEND_SLASH = False
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
