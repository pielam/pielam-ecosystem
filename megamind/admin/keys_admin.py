from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from megamind.models.keys import UserKey

@admin.register(UserKey)
class UserKeyAdmin(admin.ModelAdmin):
    """
    Secure admin interface for user cryptographic keys.
    """

    # ========= LIST VIEW =========
    list_display = (
        "user",
        "symmetric_key_status",
        "e2e_key_status",
        "jwt_key_status",
        "created_at",
        "updated_at",
    )

    list_select_related = ("user",)
    search_fields = ("user__email_or_phone", "user__user_uuid")
    ordering = ("-created_at",)

    # ========= READ-ONLY SECURITY =========
    readonly_fields = (
        "created_at",
        "updated_at",
        "key_status_summary",
    )

    fieldsets = (
        (_("User"), {
            "fields": ("user",),
        }),
        (_("Key Status (Read-Only)"), {
            "fields": ("key_status_summary",),
        }),
        (_("Symmetric Encryption"), {
            "fields": ("encryption_key",),
            "classes": ("collapse",),
        }),
        (_("E2E Asymmetric Keys"), {
            "fields": ("public_key", "private_key_encrypted"),
            "classes": ("collapse",),
        }),
        (_("JWT Signing Keys"), {
            "fields": ("jwt_public_key", "jwt_private_key_encrypted"),
            "classes": ("collapse",),
        }),
        (_("Timestamps"), {
            "fields": ("created_at", "updated_at"),
        }),
    )

    # ========= PERMISSIONS =========
    def has_add_permission(self, request):
        # Keys should be created programmatically only
        return False

    def has_delete_permission(self, request, obj=None):
        # Prevent accidental key deletion
        return False

    # ========= STATUS BADGES =========
    @admin.display(description="Symmetric Key", ordering="encryption_key")
    def symmetric_key_status(self, obj):
        return self._badge(obj.has_symmetric_key)

    @admin.display(description="E2E Keys")
    def e2e_key_status(self, obj):
        return self._badge(obj.has_e2e_keys)

    @admin.display(description="JWT Keys")
    def jwt_key_status(self, obj):
        return self._badge(obj.has_jwt_keys)

    def _badge(self, enabled: bool):
        """
        Render green/red status badge safely.
        """
        color = "#16a34a" if enabled else "#dc2626"
        label = "✔ Present" if enabled else "✘ Missing"

        return format_html(
            '<span style="color:{}; font-weight:600;">{}</span>',
            color,
            label
        )

    # ========= SUMMARY =========
    @admin.display(description="Key Summary")
    def key_status_summary(self, obj):
        return format_html(
            """
            <ul style="margin-left: 1em;">
                <li>🔐 Symmetric Key: <b>{}</b></li>
                <li>🔑 E2E Keys: <b>{}</b></li>
                <li>🪪 JWT Keys: <b>{}</b></li>
            </ul>
            """,
            "Yes" if obj.has_symmetric_key else "No",
            "Yes" if obj.has_e2e_keys else "No",
            "Yes" if obj.has_jwt_keys else "No",
        )
