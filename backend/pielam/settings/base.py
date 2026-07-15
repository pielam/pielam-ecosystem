import os
from pathlib import Path

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # Adjust accordingly


SECRET_KEY = os.environ.get("'django-insecure-o@6wohmtl^b*nyy90+mz92_yywtm(ik6ykikzxt%q)(&l-4t4y'", "dev-secret-key")

DEBUG = False

INSTALLED_APPS = [
    # Django default
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",   # ✅ REQUIRED
    "django.contrib.humanize",

    # Installed Apps
    'corsheaders',
    'rest_framework',
    'rest_framework_simplejwt',   
    'rest_framework.authtoken',

    #oauth
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",

    

    # My Appa
    "apps.ponno",
    "apps.customer",
    "apps.kobutor",
    "apps.core",
    "apps.tomal",
    "apps.notify",

        # My Apps
    'megamind.apps.MegamindConfig',
    'dispos.apps.DisposConfig',
    'engine.apps.EngineConfig',

   
]


MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",



    "allauth.account.middleware.AccountMiddleware",

]

ROOT_URLCONF = "pielam.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
         'DIRS': [os.path.join(BASE_DIR, 'templates')],  # Optional if you want a global templates folder
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "pielam.wsgi.application"
ASGI_APPLICATION = "pielam.asgi.application"



AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Dhaka"
USE_I18N = True
USE_TZ = True


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = 'customer.User'


REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
}

#recaptcha part

# settings.py

RECAPTCHA_SITE_KEY = "6LckfiosAAAAAA61A01yUAKzdTFm8HpeledcRM8_"
RECAPTCHA_SECRET_KEY = "6LckfiosAAAAANfbG-yoek-VdMjo2VCP6ofMad6o"

# -------------------------------------------------
# SITES
# -------------------------------------------------
SITE_ID = 1


# -------------------------------------------------
# AUTHENTICATION BACKENDS
# -------------------------------------------------
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]


# -------------------------------------------------
# LOGIN / LOGOUT REDIRECTS
# -------------------------------------------------
from django.urls import reverse_lazy

LOGIN_REDIRECT_URL = reverse_lazy("customer:profile")
LOGOUT_REDIRECT_URL = reverse_lazy("customer:signin")


# -------------------------------------------------
# DJANGO-ALLAUTH (NO USERNAME FIELD)
# -------------------------------------------------

ACCOUNT_USER_MODEL_USERNAME_FIELD = None

ACCOUNT_LOGIN_METHODS = {"email"}

ACCOUNT_SIGNUP_FIELDS = [
    "email*",
    "password1*",
    "password2*",
]

# "none" lets Google-authenticated users skip email verification
# since Google already verified the email
ACCOUNT_EMAIL_VERIFICATION = "none"


# True = automatically create a user record on first Google login
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_LOGIN_ON_GET = True


# -------------------------------------------------
# CUSTOM SOCIAL ACCOUNT ADAPTER
# -------------------------------------------------
SOCIALACCOUNT_ADAPTER = "apps.customer.adapters.SocialAccountAdapter"


# -------------------------------------------------
# GOOGLE PROVIDER CONFIG
# -------------------------------------------------
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": [
            "profile",
            "email",
        ],
        "AUTH_PARAMS": {
            "access_type": "online",
        },
    }
}
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'ERROR',
            'class': 'logging.FileHandler',
            'filename': os.path.join(BASE_DIR, 'django_error.log'),
            'formatter': 'verbose',
        },
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['file'],
            'level': 'ERROR',
            'propagate': True,
        },
        'django.server': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'apps.customer.views.profile': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}

CELERY_ENABLED = False