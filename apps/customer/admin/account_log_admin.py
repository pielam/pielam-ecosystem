# apps/customer/admin/account_log_admin.py

"""
Django Admin Configuration for AccountLog
-------------------------------------------
AccountLog (apps/customer/models/account_log.py) is an append-only
audit trail written entirely by signals -- nothing in the app is meant
to create, edit, or normally delete rows here by hand. This admin
registration reflects that:

- No "Add" button (has_add_permission returns False): rows only ever
  come from the pre_save/post_save signals on User, never from an
  admin form. A hand-added row wouldn't correspond to any real change
  and would just be misleading.
- Nothing is editable (has_change_permission returns False, and every
  field is also listed in readonly_fields as a belt-and-suspenders
  measure in case change permission is ever loosened by mistake).
  Editing history after the fact defeats the point of having it.
- Deletion is restricted to superusers (mirrors UserAdmin's own
  has_delete_permission in admin.py) -- for things like enforcing a
  data-retention window, not for routine cleanup.

WHY THIS IS A SEPARATE FILE FROM admin.py
------------------------------------------
Keeps UserAdmin's already-long file from growing further, and keeps
each *_log.py model's admin paired with a matching *_log_admin.py --
the same one-model-per-file convention already used for the model
layer (account_log.py, contact_info_log.py, etc.).

WIRING THIS UP
---------------
Django's admin autodiscovery only imports each app's top-level
`admin.py` (or `admin/__init__.py` for an admin package) automatically
-- it does NOT recurse into arbitrary sibling files the way
`apps.py`'s `ready()` does for signal modules. So this file needs an
explicit import somewhere autodiscovery-visible. Two ways to do that,
pick whichever matches how the rest of the app is laid out:

  1. If apps/customer/admin.py is a single file (as shown in the
     admin.py reviewed earlier in this conversation), add this import
     near the top of that file:

         import apps.customer.admin.account_log_admin  # noqa: F401

     (Note: that only works if apps/customer/admin.py is converted
     into an admin/ *package* first -- see option 2 below, which is
     simpler if admin.py is currently a plain module.)

  2. Convert apps/customer/admin.py into a package:
     apps/customer/admin/__init__.py (move the existing admin.py
     content here) + apps/customer/admin/account_log_admin.py (this
     file) + `from . import account_log_admin` in __init__.py.

Whichever route is used, the registration below only takes effect once
this module is actually imported during Django's admin autodiscovery.

OPTIONAL: EMBEDDING AS AN INLINE ON UserAdmin
------------------------------------------------
AccountLogInline (below) is defined but NOT attached to UserAdmin here
-- doing that would mean editing admin.py directly, which is out of
scope for this file. To show a user's recent account history on their
own detail page, add to UserAdmin in admin.py:

    from apps.customer.admin.account_log_admin import AccountLogInline

    class UserAdmin(BaseUserAdmin):
        inlines = [AccountLogInline]
        ...
"""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.customer.models.account_log import AccountLog


# ====================================================================
# ACTION BADGE COLORS
# ====================================================================
# Same "colored badge per choice value" pattern as UserAdmin's
# role_badge / account_status_badge in admin.py, so AccountLog entries
# read consistently with the rest of the customer app's admin.
_ACTION_COLORS = {
    AccountLog.Action.CREATED: '#28a745',           # Green
    AccountLog.Action.UPDATED: '#6c757d',            # Gray
    AccountLog.Action.DELETED: '#dc3545',            # Red
    AccountLog.Action.RESTORED: '#28a745',           # Green
    AccountLog.Action.ROLE_CHANGED: '#17a2b8',       # Cyan
    AccountLog.Action.LOGIN_SUCCESS: '#28a745',      # Green
    AccountLog.Action.LOGIN_FAILED: '#dc3545',       # Red
    AccountLog.Action.LOCKED: '#fd7e14',             # Orange
    AccountLog.Action.UNLOCKED: '#28a745',           # Green
    AccountLog.Action.PASSWORD_CHANGED: '#007bff',   # Blue
    AccountLog.Action.MFA_ENABLED: '#28a745',        # Green
    AccountLog.Action.MFA_DISABLED: '#6c757d',       # Gray
    AccountLog.Action.EMAIL_VERIFIED: '#28a745',     # Green
    AccountLog.Action.PHONE_VERIFIED: '#28a745',     # Green
}
_DEFAULT_ACTION_COLOR = '#6c757d'  # Gray, for any Action value not in the map above


def _action_badge_html(action: str, display_label: str, color_map: dict = None, default_color: str = None) -> str:
    """
    Shared badge renderer -- used by both AccountLogAdmin's list
    column and AccountLogInline's, so the two views of the same data
    look identical. Also imported directly by the sibling
    *_log_admin.py modules (contact_info_log_admin.py, etc.), each of
    which has its own Action choices and therefore its own color map --
    color_map/default_color default to this module's own
    _ACTION_COLORS/_DEFAULT_ACTION_COLOR when omitted (the common case
    for calls within this file), but a caller working with a different
    Action enum must pass its own.
    """
    if color_map is None:
        color_map = _ACTION_COLORS
    if default_color is None:
        default_color = _DEFAULT_ACTION_COLOR
    color = color_map.get(action, default_color)
    return format_html(
        '<span style="background-color: {}; color: white; '
        'padding: 3px 10px; border-radius: 3px; font-size: 11px; '
        'font-weight: bold;">{}</span>',
        color,
        display_label.upper(),
    )


def _changes_html(changes: dict) -> str:
    """
    Render the {field: {"old": ..., "new": ...}} diff as a compact
    HTML table instead of a raw JSON blob -- the same information
    admin.py's Django admin would show for a JSONField by default, but
    scannable at a glance rather than requiring the reader to parse
    JSON syntax themselves.
    """
    if not changes:
        return "-"

    rows = []
    for field, values in changes.items():
        old = values.get("old")
        new = values.get("new")
        rows.append(
            format_html(
                '<tr>'
                '<td style="padding: 2px 8px; font-weight: bold; white-space: nowrap;">{}</td>'
                '<td style="padding: 2px 8px; color: #dc3545;">{}</td>'
                '<td style="padding: 2px 8px;">→</td>'
                '<td style="padding: 2px 8px; color: #28a745;">{}</td>'
                '</tr>',
                field,
                old if old is not None else '—',
                new if new is not None else '—',
            )
        )

    return format_html(
        '<table style="border-collapse: collapse;">{}</table>',
        format_html(''.join(rows)),
    )


# ====================================================================
# READONLY MIXIN
# ====================================================================

class ReadOnlyAuditAdminMixin:
    """
    Shared "this is an append-only audit trail" permission set, so
    AccountLogAdmin (standalone) and any future *_log_admin.py module
    for the sibling logs (ContactInfoLog, LocationInfoLog,
    ProfileInfoLog, SocialInfoLog) don't each have to redeclare the
    same three permission methods.
    """

    def has_add_permission(self, request):
        # Rows only ever come from account.py's signals -- an
        # admin-created row wouldn't correspond to a real event.
        return False

    def has_change_permission(self, request, obj=None):
        # History that can be edited after the fact isn't history.
        return False

    def has_delete_permission(self, request, obj=None):
        # Mirrors UserAdmin.has_delete_permission in admin.py --
        # deletion is for enforcing retention policy, not routine use.
        return request.user.is_superuser


# ====================================================================
# INLINE (optional -- see module docstring for how to attach it)
# ====================================================================

class AccountLogInline(ReadOnlyAuditAdminMixin, admin.TabularInline):
    """
    Read-only inline showing a user's own AccountLog history on their
    UserAdmin detail page. Not attached to UserAdmin by this file --
    see the module docstring's "OPTIONAL: EMBEDDING AS AN INLINE" note.
    """

    model = AccountLog
    fk_name = 'user'
    extra = 0
    can_delete = False
    ordering = ('-created_at',)

    # Cap how many rows Django even fetches for the inline formset --
    # a long-lived account could otherwise have thousands of
    # AccountLog rows, and this is a detail-page widget, not a report.
    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('performed_by')
        return qs.order_by('-created_at')[:25]

    fields = ('action_badge_display', 'changes_display', 'performed_by', 'ip_address', 'created_at')
    readonly_fields = fields

    @admin.display(description='Action')
    def action_badge_display(self, obj):
        return _action_badge_html(obj.action, obj.get_action_display())

    @admin.display(description='Changes')
    def changes_display(self, obj):
        return _changes_html(obj.changes)


# ====================================================================
# ACCOUNT LOG ADMIN
# ====================================================================

@admin.register(AccountLog)
class AccountLogAdmin(ReadOnlyAuditAdminMixin, admin.ModelAdmin):
    """Standalone admin for browsing/searching AccountLog across all users."""

    # ================================================================
    # LIST DISPLAY
    # ================================================================

    list_display = (
        'created_at',
        'user_link',
        'action_badge',
        'changed_fields_summary',
        'performed_by',
        'ip_address',
    )

    list_display_links = ('created_at', 'user_link')

    list_filter = (
        'action',
        'created_at',
    )

    search_fields = (
        'user__email_or_phone',
        'user__email',
        'user__phone',
        'performed_by__email_or_phone',
        'ip_address',
    )

    date_hierarchy = 'created_at'

    ordering = ('-created_at',)

    # ================================================================
    # DETAIL VIEW
    # ================================================================

    fields = (
        'user',
        'action_badge',
        'changes_display',
        'performed_by',
        'ip_address',
        'user_agent',
        'metadata',
        'created_at',
    )

    readonly_fields = fields  # every field -- see has_change_permission above

    # ================================================================
    # CUSTOM DISPLAY METHODS
    # ================================================================

    @admin.display(description='User', ordering='user__email_or_phone')
    def user_link(self, obj):
        """
        obj.user is guaranteed non-null (AccountLog.user has no
        null=True and User rows are never hard-deleted in normal
        operation -- see account.py's soft_delete()/delete() override),
        so no None-guard is needed the way admin.py's fieldsets code
        guards against fields that might not exist.
        """
        return obj.user.email_or_phone

    @admin.display(description='Action')
    def action_badge(self, obj):
        return _action_badge_html(obj.action, obj.get_action_display())

    @admin.display(description='Changed Fields')
    def changed_fields_summary(self, obj):
        """
        Compact one-line summary for the list view (e.g. "role,
        account_status") -- the full old/new values are one click away
        on the detail page via changes_display, so the list view stays
        scannable instead of trying to cram a diff table into every row.
        """
        if not obj.changes:
            return "-"
        return ", ".join(sorted(obj.changes.keys()))

    @admin.display(description='Changes')
    def changes_display(self, obj):
        return _changes_html(obj.changes)

    # ================================================================
    # QUERYSET OPTIMIZATION
    # ================================================================

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('user', 'performed_by')