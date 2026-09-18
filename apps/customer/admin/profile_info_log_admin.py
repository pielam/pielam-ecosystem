# apps/customer/admin/profile_info_log_admin.py

"""
Django Admin Configuration for ProfileInfoLog
------------------------------------------------
Same read-only audit-trail admin as account_log_admin.py /
contact_info_log_admin.py, adapted for ProfileInfoLog
(apps/customer/models/profile_info_log.py). See account_log_admin.py's
module docstring for the full reasoning behind the permission model
(no add, no edit, superuser-only delete) and the wiring caveat about
admin autodiscovery not recursing into sibling files -- both apply
identically here and aren't repeated in full below.

REUSE FROM account_log_admin.py
----------------------------------
`ReadOnlyAuditAdminMixin`, `_action_badge_html()`, and `_changes_html()`
are imported rather than redefined -- same reuse pattern
contact_info_log_admin.py already established. This file's own
`_ACTION_COLORS` map is keyed to ProfileInfoLog's Action choices,
which are the largest of any *_log model so far (17 values, vs.
AccountLog's 14 and ContactInfoLog's 14) since ProfileInfo itself has
the most independently-toggleable status flags of the models logged in
this app.

FIELD NAME DIFFERENCE FROM AccountLog / ContactInfoLog
----------------------------------------------------------
ProfileInfoLog's FK to the thing it's logging about is named
`profile` (pointing at ProfileInfo, whose own `user` is a further hop
away -- see profile_info.py, where `ProfileInfo.user` is the primary
key). So `user_link` below reads `obj.profile.user`, and
search_fields/select_related paths go through `profile__user__...`,
the same shape as ContactInfoLogAdmin's `contact__user__...`.

WHAT THIS DOESN'T COVER
---------------------------
profile_info_log.py's own docstring already flags that follower/
following/blocked_users changes aren't tracked by ProfileInfoLog at
all (they're M2M fields, a different signal mechanism entirely -- see
that file for the full reasoning). This admin file inherits that gap
as-is: nothing here surfaces follow/block activity, only whatever
ProfileInfoLog.changes actually contains.
"""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.customer.models.profile_info_log import ProfileInfoLog
from apps.customer.admin.account_log_admin import (
    ReadOnlyAuditAdminMixin,
    _action_badge_html,
    _changes_html,
)


# ====================================================================
# ACTION BADGE COLORS
# ====================================================================
# Same "colored badge per choice value" pattern as AccountLogAdmin's/
# ContactInfoLogAdmin's _ACTION_COLORS, keyed to ProfileInfoLog's own
# Action choices.
_ACTION_COLORS = {
    ProfileInfoLog.Action.CREATED: '#28a745',                         # Green
    ProfileInfoLog.Action.UPDATED: '#6c757d',                         # Gray
    ProfileInfoLog.Action.ARCHIVED: '#dc3545',                        # Red
    ProfileInfoLog.Action.RESTORED: '#28a745',                        # Green
    ProfileInfoLog.Action.SUSPENDED: '#dc3545',                       # Red
    ProfileInfoLog.Action.UNSUSPENDED: '#28a745',                     # Green
    ProfileInfoLog.Action.VERIFIED: '#28a745',                        # Green
    ProfileInfoLog.Action.UNVERIFIED: '#6c757d',                      # Gray
    ProfileInfoLog.Action.MADE_PUBLIC: '#007bff',                     # Blue
    ProfileInfoLog.Action.MADE_PRIVATE: '#fd7e14',                    # Orange
    ProfileInfoLog.Action.FEATURED: '#ffc107',                        # Yellow
    ProfileInfoLog.Action.UNFEATURED: '#6c757d',                      # Gray
    ProfileInfoLog.Action.PHOTO_CHANGED: '#17a2b8',                   # Cyan
    ProfileInfoLog.Action.IDENTITY_CHANGED: '#17a2b8',                # Cyan
    ProfileInfoLog.Action.BIO_CHANGED: '#17a2b8',                     # Cyan
    ProfileInfoLog.Action.PRIVACY_SETTINGS_CHANGED: '#fd7e14',        # Orange
    ProfileInfoLog.Action.NOTIFICATION_SETTINGS_CHANGED: '#6c757d',   # Gray
}
_DEFAULT_ACTION_COLOR = '#6c757d'  # Gray, for any Action value not in the map above


# ====================================================================
# INLINE (optional -- see account_log_admin.py's docstring for how
# to attach an inline like this to a ModelAdmin)
# ====================================================================

class ProfileInfoLogInline(ReadOnlyAuditAdminMixin, admin.TabularInline):
    """
    Read-only inline showing a ProfileInfo row's own change history --
    intended for attaching to a future ProfileInfoAdmin (not defined
    here; profile_info.py's own admin registration, if any, is out of
    scope for this file), the same way AccountLogInline is meant for
    UserAdmin.
    """

    model = ProfileInfoLog
    fk_name = 'profile'
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
# PROFILE INFO LOG ADMIN
# ====================================================================

@admin.register(ProfileInfoLog)
class ProfileInfoLogAdmin(ReadOnlyAuditAdminMixin, admin.ModelAdmin):
    """Standalone admin for browsing/searching ProfileInfoLog across all users."""

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
        'profile__user__email_or_phone',
        'profile__user__email',
        'profile__user__phone',
        'profile__profile_name',
        'profile__profile_name_slug',
        'performed_by__email_or_phone',
        'ip_address',
    )

    date_hierarchy = 'created_at'

    ordering = ('-created_at',)

    # ================================================================
    # DETAIL VIEW
    # ================================================================

    fields = (
        'profile',
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

    @admin.display(description='User', ordering='profile__user__email_or_phone')
    def user_link(self, obj):
        """
        obj.profile is guaranteed non-null (ProfileInfoLog.profile has
        no null=True), and obj.profile.user is that row's primary key,
        so no None-guard is needed here.
        """
        return obj.profile.user.email_or_phone

    @admin.display(description='Action')
    def action_badge(self, obj):
        return _action_badge_html(obj.action, obj.get_action_display(), _ACTION_COLORS, _DEFAULT_ACTION_COLOR)

    @admin.display(description='Changed Fields')
    def changed_fields_summary(self, obj):
        """
        Compact one-line summary for the list view (e.g. "profile_bio,
        show_location") -- the full old/new values are one click away
        on the detail page via changes_display.
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
        return qs.select_related('profile__user', 'performed_by')