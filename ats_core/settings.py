from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "recruitment",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "ats_core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "ats_core.wsgi.application"
ASGI_APPLICATION = "ats_core.asgi.application"

# --- Database (PostgreSQL via DATABASE_URL) ---
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = 60

# --- Auth ---
AUTH_USER_MODEL = "recruitment.User"
LOGIN_URL = "recruitment:login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- i18n ---
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="UTC")  # interview times are interpreted in this zone
USE_I18N = True
USE_TZ = True

# --- Static & media (media is NEVER served directly; resumes go through an auth-checked view) ---
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedStaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Set HTTPS_ONLY=True behind TLS in production.
if env.bool("HTTPS_ONLY", default=False):
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000

# --- Resume / AI ---
GROQ_API_KEY = env("GROQ_API_KEY", default="")
GROQ_MODEL = env("GROQ_MODEL", default="llama-3.3-70b-versatile")
MAX_RESUME_SIZE_MB = env.int("MAX_RESUME_SIZE_MB", default=5)
RESUME_MAX_CHARS = env.int("RESUME_MAX_CHARS", default=12000)

# --- Celery ---
REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = None            # task state lives in our DB (parse_status / ai_status)
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_EAGER", default=False)  # True = run inline, no worker
CELERY_TASK_ACKS_LATE = True            # a crashed worker's task is redelivered, not lost
CELERY_WORKER_PREFETCH_MULTIPLIER = 1   # don't hoard slow Groq tasks on one worker
CELERY_TASK_SOFT_TIME_LIMIT = 90
CELERY_TASK_TIME_LIMIT = 120
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
