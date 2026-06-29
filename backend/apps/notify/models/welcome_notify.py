from django.db import models
from django.conf import settings

class WelcomeNotification(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="welcome"
    )
    title = models.CharField(max_length=255, default="Welcome to Pielam!")
    message = models.TextField(default="Your account has been created successfully.")
    is_shown = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Welcome notification for {self.user.email_or_phone}"
