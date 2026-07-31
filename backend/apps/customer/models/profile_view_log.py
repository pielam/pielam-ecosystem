# apps/customer/models/profile_view_log.py
from django.db import models
from django.conf import settings
from django.utils import timezone


class ProfileViewLog(models.Model):
    profile_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile_views_received",
    )
    viewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="profiles_viewed",
    )
    viewed_at = models.DateTimeField(default=timezone.now, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "profile_view_log"
        ordering = ["-viewed_at"]
        # One row per authenticated viewer — updated on each revisit
        constraints = [
            models.UniqueConstraint(
                fields=["profile_user", "viewer"],
                condition=models.Q(viewer__isnull=False),
                name="unique_profile_view_per_viewer",
            )
        ]

    def __str__(self):
        viewer = self.viewer.email_or_phone if self.viewer else "Anonymous"
        return f"{viewer} → {self.profile_user.email_or_phone} @ {self.viewed_at:%Y-%m-%d %H:%M}"