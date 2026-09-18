# megamind/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from engine.profile_views.engine_profile import ConnectedServiceViewSet

router = DefaultRouter()
router.register(r"connected-services", ConnectedServiceViewSet, basename="connected-service")

urlpatterns = [
    path("", include(router.urls)),
]
