from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from megamind.models.recovery import UserRecovery


@admin.register(UserRecovery)
class UserRecoveryAdmin(admin.ModelAdmin):
    """
    Secure admin interface for account recovery requests.
    """

    # ========= LIST VIEW =========
    list_display = (
        "recovery_uuid",
        "user",
        "method",
        "contact_used",
        "status_badge",
        "attempts_display",
        "expires_display",
        "created_at",
    )

    list_select_related = ("user",)
    list_filter = ("method", "status", "created_at")
    search_fields = (
        "recovery_uuid",
        "user__email_or_phone",
        "contact_used",
    )
    ordering = ("-created_at",)

    # ========= READ-ONLY =========
    readonly_fields = (
        "recovery_uuid",
        "created_at",
        "expires_at",
        "completed_at",
        "status_summary",
    )

    fieldsets = (
        (_("Recovery Info"), {
            "fields": (
                "recovery_uuid",
                "user",
                "method",
                "contact_used",
                "status",
            ),
        }),
        (_("Email Snapshot"), {
            "fields": (
                "email",
                "is_email_verified",
                "email_verified_at",
            ),
            "classes": ("collapse",),
        }),
        (_("Phone Snapshot"), {
            "fields": (
                "phone",
                "is_phone_verified",
                "phone_verified_at",
            ),
            "classes": ("collapse",),
        }),
        (_("Attempts"), {
            "fields": ("attempts", "max_attempts"),
        }),
        (_("Timing"), {
            "fields": (
                "created_at",
                "expires_at",
                "completed_at",
            ),
        }),
        (_("Status Summary"), {
            "fields": ("status_summary",),
        }),
    )

    # ========= PERMISSIONS =========
    def has_add_permission(self, request):
        # Recovery entries must be system-generated only
        return False

    def has_delete_permission(self, request, obj=None):
        # Do not allow deletion for audit integrity
        return False

    # ========= DISPLAY HELPERS =========
    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        color_map = {
            "pending": "#ca8a04",     # amber
            "completed": "#16a34a",   # green
            "failed": "#dc2626",      # red
            "expired": "#6b7280",     # gray
        }

        return format_html(
            '<span style="color:{}; font-weight:600;">{}</span>',
            color_map.get(obj.status, "#000"),
            obj.status.upper()
        )

    @admin.display(description="Attempts")
    def attempts_display(self, obj):
        if obj.attempts >= obj.max_attempts:
            return format_html(
                '<span style="color:#dc2626; font-weight:600;">{}/{}</span>',
                obj.attempts,
                obj.max_attempts
            )
        return f"{obj.attempts}/{obj.max_attempts}"

    @admin.display(description="Expires")
    def expires_display(self, obj):
        if obj.expires_at < timezone.now():
            return format_html(
                '<span style="color:#dc2626; font-weight:600;">Expired</span>'
            )
        return obj.expires_at.strftime("%Y-%m-%d %H:%M")

    @admin.display(description="Recovery Summary")
    def status_summary(self, obj):
        return format_html(
            """
            <ul style="margin-left:1em;">
                <li>📩 Method: <b>{}</b></li>
                <li>📞 Contact Used: <b>{}</b></li>
                <li>🧪 Attempts: <b>{}/{}</b></li>
                <li>⏳ Expires: <b>{}</b></li>
                <li>✅ Completed: <b>{}</b></li>
            </ul>
            """,
            obj.method,
            obj.contact_used or "—",
            obj.attempts,
            obj.max_attempts,
            "Expired" if obj.expires_at < timezone.now() else obj.expires_at,
            obj.completed_at or "No",
        )
