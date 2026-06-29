from django.urls import path
from apps.notify.views.notification import NotificationView

app_name = "notify"

urlpatterns = [
    path('', NotificationView, name='notification'),
    

    
]
