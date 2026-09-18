# apps/notify/urls.py
from django.urls import path
from apps.notify.views.pulse import (
    NotificationView,
    unread_count,
    latest,
    mark_read,
    mark_all_read,
    go,
)

app_name = 'notify'  # <-- this line registers the 'notify' namespace

urlpatterns = [
    path('', NotificationView, name='notifications'),

    # Polling API
    path('unread-count/', unread_count, name='unread_count'),
    path('latest/', latest, name='latest'),
    path('<uuid:notification_uuid>/read/', mark_read, name='mark_read'),
    path('read-all/', mark_all_read, name='mark_all_read'),

    path('n/<slug:slug>/', go, name='go'),
]