# apps/customer/admin/business_info_admin.py

"""
Admin configuration for BusinessInfo.

Mirrors the read-only / soft-delete-aware admin patterns used for
ProfileInfo, LocationInfo, and SocialInfo elsewhere in this app:
- Archived rows are visible (not hidden) but clearly flagged, since
  BusinessInfo.objects default manager doesn't exclude them.
- "Delete" from the admin action list calls soft_delete() rather than
  a real DB delete, via custom actions, since BusinessInfo.delete()
  already routes single-instance deletes through soft_delete() but
  Django's default bulk admin action bypasses instance .delete() and
  calls the queryset's .delete() -- which BusinessInfoQuerySet already
  overrides to archive, so no special-casing is needed there, but we
  add explicit actions for clarity/discoverability in the admin UI.
- Sensitive fields (tax_id, registration_number) are shown but kept
  out of list_display to avoid leaking them in the changelist view.
"""

from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.customer.models.business_info import BusinessInfo


# ====================================================================
# FILTERS
# ====================================================================

class ArchivedFilter(admin.SimpleListFilter):
    """
    Explicit archived/active filter -- the default manager on
    BusinessInfo does not exclude archived rows, so without this the
    changelist would just be a flat, unfiltered dump of everything.
    """

    title = _("Archived status")
    parameter_name = "archived"

    def lookups(self, request, model_admin):
        return (
            ("active", _("Active only")),
            ("archived", _("Archived only")),
        )

    def queryset(self, request, queryset):
        if self.value() == "active":
            return queryset.filter(is_business_archived=False)
        if self.value() == "archived":
            return queryset.filter(is_business_archived=True)
        return queryset


# ====================================================================
# BUSINESS INFO ADMIN
# ====================================================================

@admin.register(BusinessInfo)
class BusinessInfoAdmin(admin.ModelAdmin):

    # ----------------------------------------------------------------
    # LIST VIEW
    # ----------------------------------------------------------------

    list_display = (
        "business_name",
        "user",
        "business_type",
        "industry",
        "verified_badge",
        "public_badge",
        "archived_badge",
        "created_at",
    )

    list_filter = (
        ArchivedFilter,
        "is_business_verified",
        "is_business_public",
        "business_type",
        "currency",
    )

    search_fields = (
        "business_name",
        "legal_name",
        "registration_number",
        "tax_id",
        "business_email",
        "user__username",
        "user__email",
    )

    ordering = ("-created_at",)
    list_per_page = 50
    date_hierarchy = "created_at"

    # ----------------------------------------------------------------
    # DETAIL VIEW
    # ----------------------------------------------------------------

    autocomplete_fields = ("user", "verified_by", "archived_by")

    readonly_fields = (
        "uuid",
        "created_at",
        "updated_at",
        "verified_at",
        "archived_at",
        "completion_percentage",
        "age_in_days_display",
    )

    fieldsets = (
        (_("Owner"), {
            "fields": ("user", "uuid"),
        }),
        (_("Core Details"), {
            "fields": (
                "business_name",
                "legal_name",
                "business_type",
                "industry",
                "description",
                "founded_date",
                "age_in_days_display",
            ),
        }),
        (_("Registration & Tax"), {
            "fields": ("registration_number", "tax_id"),
            "description": _(
                "Sensitive fields -- not shown in the changelist. "
                "No GDPR cascade is wired up yet; see the model docstring."
            ),
        }),
        (_("Business Contact"), {
            "fields": ("business_email", "business_phone", "website"),
        }),
        (_("Scale / Financials"), {
            "fields": ("employee_count", "annual_revenue", "currency"),
        }),
        (_("Verification"), {
            "fields": (
                "is_business_verified",
                "verified_at",
                "verified_by",
            ),
        }),
        (_("Visibility"), {
            "fields": ("is_business_public",),
        }),
        (_("Archive / Soft Delete"), {
            "fields": (
                "is_business_archived",
                "archived_at",
                "archived_by",
            ),
        }),
        (_("Metadata"), {
            "fields": ("metadata", "completion_percentage"),
            "classes": ("collapse",),
        }),
        (_("Timestamps"), {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    # ----------------------------------------------------------------
    # DISPLAY HELPERS
    # ----------------------------------------------------------------

    @admin.display(description=_("Verified"), boolean=False)
    def verified_badge(self, obj):
        if obj.is_business_verified:
            return format_html('<span style="color: #1a7f37;">&#10003; Verified</span>')
        return format_html('<span style="color: #999;">&#8212;</span>')

    @admin.display(description=_("Public"), boolean=False)
    def public_badge(self, obj):
        if obj.is_business_public:
            return format_html('<span style="color: #1a7f37;">Public</span>')
        return format_html('<span style="color: #999;">Private</span>')

    @admin.display(description=_("Archived"), boolean=False)
    def archived_badge(self, obj):
        if obj.is_business_archived:
            return format_html('<span style="color: #cf222e; font-weight: bold;">Archived</span>')
        return format_html('<span style="color: #1a7f37;">Active</span>')

    @admin.display(description=_("Age (days)"))
    def age_in_days_display(self, obj):
        return obj.age_in_days if obj.age_in_days is not None else "-"

    @admin.display(description=_("Completion"))
    def completion_percentage(self, obj):
        return f"{obj.completion_percentage}%"

    # ----------------------------------------------------------------
    # ACTIONS
    # (Explicit soft-delete/restore/verify actions, since the default
    # "Delete selected" admin action calls the queryset's .delete(),
    # which BusinessInfoQuerySet already routes to soft_delete() --
    # these give admins an obvious, named way to do the same via the
    # action dropdown, plus the ability to reverse it.)
    # ----------------------------------------------------------------

    actions = (
        "action_soft_delete",
        "action_restore",
        "action_verify",
        "action_unverify",
    )

    @admin.action(description=_("Archive selected businesses (soft delete)"))
    def action_soft_delete(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.soft_delete(archived_by_user=request.user)
            count += 1
        self.message_user(request, _(f"Archived {count} business record(s)."))

    @admin.action(description=_("Restore selected businesses"))
    def action_restore(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.restore()
            count += 1
        self.message_user(request, _(f"Restored {count} business record(s)."))

    @admin.action(description=_("Verify selected businesses"))
    def action_verify(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.verify(verified_by_user=request.user)
            count += 1
        self.message_user(request, _(f"Verified {count} business record(s)."))

    @admin.action(description=_("Remove verification from selected businesses"))
    def action_unverify(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.unverify()
            count += 1
        self.message_user(request, _(f"Unverified {count} business record(s)."))

    # ----------------------------------------------------------------
    # QUERYSET / PERMISSIONS
    # ----------------------------------------------------------------

    def get_queryset(self, request):
        """
        Admin should see everything, including archived rows -- the
        ArchivedFilter above is how staff narrow that down, rather
        than hiding archived rows unconditionally the way user-facing
        views would (via BusinessInfoQuerySet.active()).
        """
        return super().get_queryset(request).select_related("user", "verified_by", "archived_by")

    def has_delete_permission(self, request, obj=None):
        """
        Disable Django's hard-delete UI entirely. Real removal should
        go through action_soft_delete (or BusinessInfoQuerySet.hard_delete()
        directly in a shell/migration) -- not an admin click that looks
        like a normal delete but silently only archives, or worse,
        actually hard-deletes if this is ever changed upstream.
        """
        return False