# apps/notify/models/ring_bell.py

"""
Notification Model
-------------------
In-app notification system with optional email delivery.

Features:
- Typed notifications (follow, message, comment, mention, order, system)
- Read / unread tracking
- Email delivery tracking
- Priority levels
- Soft delete with reason
- Manager with common query helpers
- GDPR-style data export
- Field validation on save
"""

import uuid

from django.db import models
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError


# ====================================================================
# NOTIFICATION MANAGER
# ====================================================================

class NotificationManager(models.Manager):
    """Custom manager for Notification with optimized queries"""

    def for_user(self, user):
        """Get all non-deleted notifications for a user"""
        return self.filter(
            recipient=user,
            deleted_at__isnull=True
        ).select_related('actor').order_by('-created_at')

    def unread_for(self, user):
        """Get unread notifications for a user"""
        return self.for_user(user).filter(is_read=False)

    def read_for(self, user):
        """Get read notifications for a user"""
        return self.for_user(user).filter(is_read=True)

    def unread_count(self, user) -> int:
        """Get unread notification count for a user"""
        return self.unread_for(user).count()

    def mark_all_read(self, user):
        """Mark all of a user's unread notifications as read"""
        return self.unread_for(user).update(is_read=True, read_at=timezone.now())

    def by_type(self, notification_type):
        """Filter notifications by type"""
        return self.filter(
            notification_type=notification_type,
            deleted_at__isnull=True
        )

    def by_priority(self, priority):
        """Filter notifications by priority"""
        return self.filter(
            priority=priority,
            deleted_at__isnull=True
        )

    def recent(self, days=30):
        """Get notifications from the last N days"""
        threshold = timezone.now() - timezone.timedelta(days=days)
        return self.filter(
            created_at__gte=threshold,
            deleted_at__isnull=True
        )

    def pending_email(self):
        """Get notifications that still need an email sent"""
        return self.filter(
            is_email_sent=False,
            deleted_at__isnull=True
        )

    def search(self, query):
        """Search notifications by title or message"""
        return self.filter(
            models.Q(title__icontains=query) | models.Q(message__icontains=query),
            deleted_at__isnull=True
        )


# ====================================================================
# NOTIFICATION MODEL
# ====================================================================

class Notification(models.Model):
    """
    Notification Model

    Represents a single in-app notification sent to a user, with
    optional accompanying email delivery and priority classification.
    """

    # ================================================================
    # CHOICES
    # ================================================================

    class NotificationType(models.TextChoices):
        FOLLOW  = 'follow',  _('New Follower')
        MESSAGE = 'message', _('New Message')
        COMMENT = 'comment', _('New Comment')
        MENTION = 'mention', _('Mention')
        ORDER   = 'order',   _('Order Update')
        SYSTEM  = 'system',  _('System')

    class Priority(models.TextChoices):
        LOW    = 'low',    _('Low')
        NORMAL = 'normal', _('Normal')
        HIGH   = 'high',   _('High')
        URGENT = 'urgent', _('Urgent')

    # ================================================================
    # IDENTIFICATION
    # ================================================================

    uuid = models.UUIDField(
        _("UUID"),
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier for external API references")
    )

    # ================================================================
    # RELATIONSHIPS
    # ================================================================

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
        help_text=_("User who receives this notification")
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='actions_sent',
        help_text=_("User who triggered this notification, if any")
    )

    # ================================================================
    # CONTENT
    # ================================================================

    notification_type = models.CharField(
        _("Notification Type"),
        max_length=20,
        choices=NotificationType.choices,
        default=NotificationType.SYSTEM,
        db_index=True,
        help_text=_("Category of notification")
    )

    priority = models.CharField(
        _("Priority"),
        max_length=10,
        choices=Priority.choices,
        default=Priority.NORMAL,
        db_index=True,
        help_text=_("Notification priority level")
    )

    title = models.CharField(
        _("Title"),
        max_length=150,
        help_text=_("Short notification headline")
    )

    message = models.TextField(
        _("Message"),
        max_length=500,
        blank=True,
        null=True,
        help_text=_("Full notification body text")
    )

    action_url = models.CharField(
        _("Action URL"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Relative or absolute URL to navigate to on click")
    )

    icon = models.CharField(
        _("Icon"),
        max_length=50,
        blank=True,
        null=True,
        help_text=_("Icon name/key for frontend display")
    )

    # ================================================================
    # READ TRACKING
    # ================================================================

    is_read = models.BooleanField(
        _("Read"),
        default=False,
        db_index=True,
        help_text=_("Whether the recipient has read this notification")
    )

    read_at = models.DateTimeField(
        _("Read At"),
        null=True,
        blank=True,
        help_text=_("Timestamp when notification was marked read")
    )

    # ================================================================
    # EMAIL DELIVERY TRACKING
    # ================================================================

    send_email = models.BooleanField(
        _("Send Email"),
        default=False,
        help_text=_("Whether this notification should also be emailed")
    )

    is_email_sent = models.BooleanField(
        _("Email Sent"),
        default=False,
        help_text=_("Whether an email copy of this notification was sent")
    )

    email_sent_at = models.DateTimeField(
        _("Email Sent At"),
        null=True,
        blank=True,
        help_text=_("Timestamp when the email was sent")
    )

    email_error = models.TextField(
        _("Email Error"),
        blank=True,
        null=True,
        help_text=_("Error message if email delivery failed")
    )

    # ================================================================
    # EXPIRY
    # ================================================================

    expires_at = models.DateTimeField(
        _("Expires At"),
        null=True,
        blank=True,
        help_text=_("Optional expiry — notification can be hidden/pruned after this time")
    )

    # ================================================================
    # TIMESTAMPS
    # ================================================================

    created_at = models.DateTimeField(
        _("Created At"),
        auto_now_add=True,
        db_index=True,
        help_text=_("When the notification was created")
    )

    updated_at = models.DateTimeField(
        _("Updated At"),
        auto_now=True,
        help_text=_("Last time this notification record was updated")
    )

    # ================================================================
    # SOFT DELETE
    # ================================================================

    deleted_at = models.DateTimeField(
        _("Deleted At"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Soft delete timestamp")
    )

    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_notifications',
        help_text=_("User who deleted this notification, if applicable")
    )

    deletion_reason = models.CharField(
        _("Deletion Reason"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Why this notification was removed")
    )

    # ================================================================
    # METADATA
    # ================================================================

    data = models.JSONField(
        _("Data"),
        default=dict,
        blank=True,
        help_text=_("Generic small payload for frontend use (e.g. related object IDs)")
    )

    metadata = models.JSONField(
        _("Metadata"),
        default=dict,
        blank=True,
        help_text=_("Additional notification metadata in JSON format")
    )

    # ================================================================
    # DISPLAY
    # ================================================================

    image = models.CharField(
        _("Image"),
        max_length=500,
        blank=True,
        null=True,
        help_text=_("Image shown alongside this notification (actor avatar, product thumbnail, etc.)")
    )

    slug = models.SlugField(
        _("Slug"),
        max_length=64,
        unique=True,
        db_index=True,
        blank=True,
        help_text=_("Unique, stable permalink slug for this notification")
    )

    # ================================================================
    # MANAGER
    # ================================================================

    objects = NotificationManager()

    # ================================================================
    # META
    # ================================================================

    class Meta:
        verbose_name = _("Notification")
        verbose_name_plural = _("Notifications")
        db_table = 'notification'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['uuid']),
            models.Index(fields=['recipient', 'is_read']),
            models.Index(fields=['recipient', 'created_at']),
            models.Index(fields=['notification_type']),
            models.Index(fields=['priority']),
            models.Index(fields=['created_at']),
            models.Index(fields=['deleted_at']),
            models.Index(fields=['expires_at']),
        ]

    # ================================================================
    # STRING REPRESENTATION
    # ================================================================

    def __str__(self) -> str:
        return f"{self.title} → {self.recipient.email_or_phone}"

    def __repr__(self) -> str:
        return f"<Notification: type={self.notification_type} recipient={self.recipient_id}>"

    # ================================================================
    # PROPERTIES
    # ================================================================

    @property
    def is_deleted(self) -> bool:
        """Check if this notification has been soft deleted"""
        return self.deleted_at is not None

    @property
    def is_from_system(self) -> bool:
        """Check if this notification has no actor (system-generated)"""
        return self.actor_id is None

    @property
    def is_expired(self) -> bool:
        """Check if this notification has passed its expiry time"""
        if self.expires_at is None:
            return False
        return timezone.now() > self.expires_at

    @property
    def is_high_priority(self) -> bool:
        """Check if priority is high or urgent"""
        return self.priority in [self.Priority.HIGH, self.Priority.URGENT]

    @property
    def needs_email(self) -> bool:
        """Check if an email still needs to be sent"""
        return self.send_email and not self.is_email_sent

    # ================================================================
    # READ METHODS
    # ================================================================

    def mark_read(self, save: bool = True) -> None:
        """Mark this notification as read"""
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            if save:
                self.save(update_fields=['is_read', 'read_at', 'updated_at'])

    def mark_unread(self, save: bool = True) -> None:
        """Mark this notification as unread"""
        if self.is_read:
            self.is_read = False
            self.read_at = None
            if save:
                self.save(update_fields=['is_read', 'read_at', 'updated_at'])

    # ================================================================
    # EMAIL METHODS
    # ================================================================

    def mark_email_sent(self, save: bool = True) -> None:
        """Mark that the email copy of this notification was sent"""
        self.is_email_sent = True
        self.email_sent_at = timezone.now()
        self.email_error = None
        if save:
            self.save(update_fields=['is_email_sent', 'email_sent_at', 'email_error', 'updated_at'])

    def mark_email_failed(self, error: str, save: bool = True) -> None:
        """Record an email delivery failure"""
        self.is_email_sent = False
        self.email_error = error[:1000]
        if save:
            self.save(update_fields=['is_email_sent', 'email_error', 'updated_at'])

    # ================================================================
    # SOFT DELETE
    # ================================================================

    def soft_delete(
        self,
        deleted_by_user=None,
        reason: str = None,
        save: bool = True
    ) -> None:
        """Soft delete this notification"""
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by_user
        self.deletion_reason = reason

        if save:
            self.save()

    def restore(self, save: bool = True) -> None:
        """Restore a soft-deleted notification"""
        self.deleted_at = None
        self.deleted_by = None
        self.deletion_reason = None

        if save:
            self.save()

    def delete(self, *args, **kwargs):
        """Override delete to use soft delete by default"""
        if kwargs.pop('hard_delete', False):
            super().delete(*args, **kwargs)
        else:
            self.soft_delete()

    # ================================================================
    # DATA EXPORT (GDPR)
    # ================================================================

    def export_data(self) -> dict:
        """Export notification data for GDPR compliance"""
        return {
            'uuid': str(self.uuid),
            'notification_type': self.notification_type,
            'priority': self.priority,
            'title': self.title,
            'message': self.message,
            'action_url': self.action_url,
            'is_read': self.is_read,
            'read_at': self.read_at.isoformat() if self.read_at else None,
            'created_at': self.created_at.isoformat(),
            'data': self.data,
            'metadata': self.metadata,
        }

    # ================================================================
    # VALIDATION
    # ================================================================

    def clean(self) -> None:
        """Validate fields before saving"""
        super().clean()

        if not self.title or not self.title.strip():
            raise ValidationError(_("Notification title cannot be empty"))

        if self.actor_id and self.actor_id == self.recipient_id:
            raise ValidationError(_("Recipient cannot be the actor of their own notification"))

        if self.expires_at and self.expires_at <= self.created_at:
            # created_at may not be set yet on first save — guard against None
            if self.created_at and self.expires_at <= self.created_at:
                raise ValidationError(_("Expiry time must be after creation time"))

    def save(self, *args, **kwargs):
        """Override save to run validation and assign a slug"""
        if not self.slug:
            self.slug = uuid.uuid4().hex[:12]
        self.full_clean()
        super().save(*args, **kwargs)