# apps/your_app/models/recovery.py

import uuid
from datetime import timedelta
from django.db import models
from django.utils import timezone
from apps.customer.models import User


class UserRecovery(models.Model):
    """
    Represents a password/account recovery request initiated via verified email or phone.
    Only users with verified contact methods can create recovery requests.
    """
    recovery_uuid = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        db_index=True
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='recovery_requests',
        help_text="User requesting account recovery"
    )
    
    # Snapshot of user's email at time of recovery request
    email = models.EmailField(
        max_length=150,
        blank=True,
        null=True,
        db_index=True,
        help_text="Verified email (if identity is phone or secondary email)"
    )
    is_email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    # Snapshot of user's phone at time of recovery request
    phone = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        db_index=True,
        help_text="E.164 phone number (if identity is email or secondary phone)"
    )
    is_phone_verified = models.BooleanField(default=False)
    phone_verified_at = models.DateTimeField(null=True, blank=True)

    # New: Explicit field to store which contact was used (fixes __str__ and index)
    contact_used = models.CharField(
        max_length=150,
        blank=True,
        help_text="Email or phone number used for this recovery attempt"
    )

    RECOVERY_METHOD_CHOICES = (
        ('email', 'Email'),
        ('phone', 'Phone'),
    )

    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('expired', 'Expired'),
    )

    method = models.CharField(
        max_length=10,
        choices=RECOVERY_METHOD_CHOICES,
        db_index=True,
        help_text="Recovery method used: email or phone"
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True
    )

    attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of verification attempts"
    )

    max_attempts = models.PositiveSmallIntegerField(
        default=3,
        help_text="Maximum allowed verification attempts"
    )

    created_at = models.DateTimeField(
        default=timezone.now,
        db_index=True
    )

    expires_at = models.DateTimeField(
        db_index=True,
        help_text="Token/OTP expiration time"
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True
    )

    class Meta:
        db_table = 'user_recovery_table'
        verbose_name = 'User Recovery'
        verbose_name_plural = 'Recovery Tables'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['method', 'contact_used']),  # Now valid
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"Recovery ({self.method}) for {self.contact_used} – {self.status}"

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = self.created_at + timedelta(minutes=15)
        # Ensure contact_used is set if not provided
        if not self.contact_used:
            if self.method == 'email' and self.email:
                self.contact_used = self.email
            elif self.method == 'phone' and self.phone:
                self.contact_used = self.phone
        super().save(*args, **kwargs)