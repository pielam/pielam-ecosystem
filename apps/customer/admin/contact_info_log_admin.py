# apps/customer/admin/contact_info_log_admin.py

"""
Django Admin Configuration for ContactInfoLog
------------------------------------------------
Same read-only audit-trail admin as account_log_admin.py, adapted for
ContactInfoLog (apps/customer/models/contact_info_log.py). See that
file's module docstring for the full reasoning behind the permission
model (no add, no edit, superuser-only delete) and the wiring caveat
about admin autodiscovery not recursing into sibling files -- both
apply identically here and aren't repeated in full below.

REUSE FROM account_log_admin.py
----------------------------------
`ReadOnlyAuditAdminMixin` and `_changes_html()` are imported from
account_log_admin.py rather than redefined here -- both are already
fully generic (the mixin's three permission methods don't reference
AccountLog at all, and _changes_html() just renders whatever
{field: {"old": ..., "new": ...}} dict it's given). `_action_badge_html()`
is also reused as the *renderer*, but each *_log_admin.py module still
defines its own `_ACTION_COLORS` map, since the Action choices differ
per model (ContactInfoLog has no ROLE_CHANGED or LOGIN_FAILED, for
instance, but does have MADE_PUBLIC/MADE_PRIVATE, which AccountLog has
no equivalent of).

FIELD NAME DIFFERENCE FROM AccountLog
----------------------------------------
AccountLog's FK to the thing it's logging about is named `user`
(pointing straight at User). ContactInfoLog's is named `contact`
(pointing at ContactInfo, whose own `user` is a further hop away --
see contact_info.py, where `ContactInfo.user` is the primary key).
So `user_link` below reads `obj.contact.user`, and search_fields/
select_related paths go one level deeper (`contact__user__...`)
than AccountLogAdmin's did.
"""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.customer.models.contact_info_log import ContactInfoLog
from apps.customer.admin.account_log_admin import (
    ReadOnlyAuditAdminMixin,
    _action_badge_html,
    _changes_html,
)


# ====================================================================
# ACTION BADGE COLORS
# ====================================================================
# Same "colored badge per choice value" pattern as AccountLogAdmin's
# _ACTION_COLORS, keyed to ContactInfoLog's own Action choices.
_ACTION_COLORS = {
    ContactInfoLog.Action.CREATED: '#28a745',                    # Green
    ContactInfoLog.Action.UPDATED: '#6c757d',                    # Gray
    ContactInfoLog.Action.ARCHIVED: '#dc3545',                   # Red
    ContactInfoLog.Action.RESTORED: '#28a745',                   # Green
    ContactInfoLog.Action.PHONE_VERIFIED: '#28a745',             # Green
    ContactInfoLog.Action.PHONE_UNVERIFIED: '#6c757d',           # Gray
    ContactInfoLog.Action.EMAIL_VERIFIED: '#28a745',             # Green
    ContactInfoLog.Action.EMAIL_UNVERIFIED: '#6c757d',           # Gray
    ContactInfoLog.Action.PREFERRED_METHOD_CHANGED: '#17a2b8',   # Cyan
    ContactInfoLog.Action.CONTACT_REQUESTS_ENABLED: '#28a745',   # Green
    ContactInfoLog.Action.CONTACT_REQUESTS_DISABLED: '#6c757d',  # Gray
    ContactInfoLog.Action.MADE_PUBLIC: '#007bff',                # Blue
    ContactInfoLog.Action.MADE_PRIVATE: '#fd7e14',               # Orange
    ContactInfoLog.Action.VISIBILITY_CHANGED: '#17a2b8',         # Cyan
}
_DEFAULT_ACTION_COLOR = '#6c757d'  # Gray, for any Action value not in the map above


# ====================================================================
# INLINE (optional -- see account_log_admin.py's docstring for how
# to attach an inline like this to a ModelAdmin)
# ====================================================================

class ContactInfoLogInline(ReadOnlyAuditAdminMixin, admin.TabularInline):
    """
    Read-only inline showing a ContactInfo row's own change history --
    intended for attaching to a future ContactInfoAdmin (not defined
    here; contact_info.py's own admin registration, if any, is out of
    scope for this file), the same way AccountLogInline is meant for
    UserAdmin.
    """

    model = ContactInfoLog
    fk_name = 'contact'
    extra = 0
    can_delete = False
    ordering = ('-created_at',)

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('performed_by')
        return qs.order_by('-created_at')[:25]

    fields = ('action_badge_display', 'changes_display', 'performed_by', 'ip_address', 'created_at')
    readonly_fields = fields

    @admin.display(description='Action')
    def action_badge_display(self, obj):
        return _action_badge_html(obj.action, obj.get_action_display(), _ACTION_COLORS, _DEFAULT_ACTION_COLOR)

    @admin.display(description='Changes')
    def changes_display(self, obj):
        return _changes_html(obj.changes)


# ====================================================================
# CONTACT INFO LOG ADMIN
# ====================================================================

@admin.register(ContactInfoLog)
class ContactInfoLogAdmin(ReadOnlyAuditAdminMixin, admin.ModelAdmin):
    """Standalone admin for browsing/searching ContactInfoLog across all users."""

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
        'contact__user__email_or_phone',
        'contact__user__email',
        'contact__user__phone',
        'performed_by__email_or_phone',
        'ip_address',
    )

    date_hierarchy = 'created_at'

    ordering = ('-created_at',)

    # ================================================================
    # DETAIL VIEW
    # ================================================================

    fields = (
        'contact',
        'action_badge',
        'changes_display',
        'performed_by',
        'ip_address',
        'user_agent',
        'metadata',
        'created_at',
    )

    readonly_fields = fields  # every field -- see ReadOnlyAuditAdminMixin

    # ================================================================
    # CUSTOM DISPLAY METHODS
    # ================================================================

    @admin.display(description='User', ordering='contact__user__email_or_phone')
    def user_link(self, obj):
        """
        obj.contact is guaranteed non-null (ContactInfoLog.contact has
        no null=True), and obj.contact.user is that row's primary key,
        so no None-guard is needed here.
        """
        return obj.contact.user.email_or_phone

    @admin.display(description='Action')
    def action_badge(self, obj):
        return _action_badge_html(obj.action, obj.get_action_display(), _ACTION_COLORS, _DEFAULT_ACTION_COLOR)

    @admin.display(description='Changed Fields')
    def changed_fields_summary(self, obj):
        """
        Compact one-line summary for the list view (e.g. "alt_email,
        show_email") -- the full old/new values are one click away on
        the detail page via changes_display.
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
        return qs.select_related('contact__user', 'performed_by')