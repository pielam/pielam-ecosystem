from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

admin.site.site_title  = "PIELAM Administration"
admin.site.site_header = "PIELAM Admin"
admin.site.index_title = "pielam"

urlpatterns = [
    path('admin/', admin.site.urls),

    path('', include('apps.ponno.web_urls')),
    path('engine/', include('dispos.web_urls')),
    path('customer/',   include('apps.customer.web_urls')),
    path('notifications/', include('apps.notify.web_urls')),

    path("accounts/", include("allauth.urls")),

    
    path('tomal/',         include('apps.tomal.urls')),
    
    path('kobutor/',       include('apps.kobutor.urls')),

    # ── Megamind API ──────────────────────────────────────────────────────
    # Registers:
    #   POST /api/connected-services/{id}/refresh/  ← used by dashboard HTML
    #   POST /api/connected-services/{id}/fetch/    ← DRF scrape endpoint
    #   GET  /api/connected-services/{id}/preview/  ← cached data
    #   + full CRUD on /api/connected-services/
    path('api/', include('megamind.urls')),
    # ─────────────────────────────────────────────────────────────────────
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL,  document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)