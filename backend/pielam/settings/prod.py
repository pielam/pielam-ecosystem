from .base import *

DEBUG = False

ALLOWED_HOSTS = ["pielam.com", "www.pielam.com"]

CSRF_TRUSTED_ORIGINS = [
    "https://pielam.com",
    "https://www.pielam.com",
]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.1/howto/static-files/

STATIC_URL = '/static/'


STATICFILES_DIRS = [ os.path.join(BASE_DIR, 'assets') ]

STATIC_ROOT = "/home/pielamco/public_html/static"



# Media Files (Image)
MEDIA_URL = '/media/'

MEDIA_ROOT = "/home/pielamco/public_html/media"

SECURE_HSTS_SECONDS = 31536000
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True


EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

EMAIL_HOST = 'mail.pielam.com'
EMAIL_PORT = 465
EMAIL_USE_TLS = False      # TLS ON
EMAIL_USE_SSL = True     # SSL must be OFF

EMAIL_HOST_USER = 'no-reply@pielam.com'
EMAIL_HOST_PASSWORD = '-RuUCUOE*=4YyKE9'   # NOT your Gmail login password

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