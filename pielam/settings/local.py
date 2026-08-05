import pielam
from .base import *

DEBUG = True
ALLOWED_HOSTS = ["*"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# DATABASES = {
#     'default': {
#         'ENGINE': 'django.db.backends.sqlite3',
#         'NAME': BASE_DIR / 'db.sqlite3',
#         'OPTIONS': {
#             'timeout': 20,  # seconds to wait for the lock instead of failing immediately
#         },
#     }
# }


### Removing SQLite db and adding PostgreSQL ====>>
## create db manually ====>>

# CREATE DATABASE pielam_db;
# CREATE USER pielam_user WITH PASSWORD 'password';
# ALTER ROLE pielam_user SET client_encoding TO 'utf8';
# ALTER ROLE pielam_user SET default_transaction_isolation TO 'read committed';
# ALTER ROLE pielam_user SET timezone TO 'UTC';
# GRANT ALL PRIVILEGES ON DATABASE pielam_db TO pielam_user;

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB"),
        "USER": os.getenv("POSTGRES_DB_USER"),
        "PASSWORD": os.getenv("POSTGRES_DB_PASSWORD"),
        "HOST": os.getenv("POSTGRES_DB_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_DB_PORT", "5435"),
    }
}


STATIC_URL = '/static/'

STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [ os.path.join(BASE_DIR, 'assets') ]

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True      # TLS ON
EMAIL_USE_SSL = False     # SSL must be OFF

EMAIL_HOST_USER = 'alifelectronics365@gmail.com'
EMAIL_HOST_PASSWORD = 'hoko wuop ermh wuvn'   # NOT your Gmail login password

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
