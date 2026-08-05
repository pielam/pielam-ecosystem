from .base import *

DEBUG = True
ALLOWED_HOSTS = ["*"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        'OPTIONS': {
            'timeout': 20,  # seconds to wait for the lock instead of failing immediately
        },
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

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',  # browser session cookie
        'rest_framework.authentication.BasicAuthentication',    # fallback
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}