import os
import pielam
from .base import *

DEBUG = True
ALLOWED_HOSTS = ["*"]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        'OPTIONS': {
            'timeout': 20,  # seconds to wait for the lock instead of failing immediately
        },
    }
}


### Removing SQLite db and adding PostgreSQL ====>>
## create db manually ====>>

# CREATE DATABASE pielam_db;
# CREATE USER pielam_user WITH PASSWORD 'password';
# ALTER ROLE pielam_user SET client_encoding TO 'utf8';
# ALTER ROLE pielam_user SET default_transaction_isolation TO 'read committed';
# ALTER ROLE pielam_user SET timezone TO 'UTC';
# GRANT ALL PRIVILEGES ON DATABASE pielam_db TO pielam_user;

# DATABASES = {
#     "default": {
#         "ENGINE": "django.db.backends.postgresql",
#         "NAME": os.getenv("POSTGRES_DB"),
#         "USER": os.getenv("POSTGRES_DB_USER"),
#         "PASSWORD": os.getenv("POSTGRES_DB_PASSWORD"),
#         "HOST": os.getenv("POSTGRES_DB_HOST", "localhost"),
#         "PORT": os.getenv("POSTGRES_DB_PORT", "5435"),
#     }
# }


STATIC_URL = '/static/'

STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'assets')]

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# ── Email ─────────────────────────────────────────────────────────────
# Defaults to the console backend so local dev never sends real email
# by accident. Set EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
# in the environment (staging/prod, or explicitly when you want to test
# real delivery locally) to switch to SMTP.
EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend",
)

EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

EMAIL_HOST = 'mail.pielam.com'
EMAIL_PORT = 465
EMAIL_USE_TLS = False      # TLS ON
EMAIL_USE_SSL = True     # SSL must be OFF

EMAIL_HOST_USER = 'no-reply@pielam.com'
EMAIL_HOST_PASSWORD = 'sm0*}(3h0t-z.rJ8'   # NOT your Gmail login password

DEFAULT_FROM_EMAIL = EMAIL_HOST_USER



# Add this to your settings.py
# (merge with existing REST_FRAMEWORK dict if you already have one)

REST_FRAMEWORK = REST_FRAMEWORK.copy()

REST_FRAMEWORK.update({
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
})