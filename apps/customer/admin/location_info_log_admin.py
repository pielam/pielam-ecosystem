# apps/customer/admin/location_info_log_admin.py

"""
Django Admin Configuration for LocationInfoLog
--------------------------------------------------
Same read-only audit-trail admin as account_log_admin.py /
contact_info_log_admin.py / profile_info_log_admin.py, adapted for
LocationInfoLog (apps/customer/models/location_info_log.py). See
account_log_admin.py's module docstring for the full reasoning behind
the permission model (no add, no edit, superuser-only delete) and the
wiring caveat about admin autodiscovery not recursing into sibling
files -- both apply identically here and aren't repeated in full
below.

REUSE FROM account_log_admin.py
----------------------------------
`ReadOnlyAuditAdminMixin`, `_action_badge_html()`, and `_changes_html()`
are imported rather than redefined -- same reuse pattern
contact_info_log_admin.py / profile_info_log_admin.py already
established. This file's own `_ACTION_COLORS` map is keyed to
LocationInfoLog's Action choices, which are the smallest set of any
*_log model so far (7 values) -- LocationInfo itself has no
verification/suspension/visibility flags the way ContactInfo and
ProfileInfo do, just address data plus an optional coordinate pair.

FIELD NAME DIFFERENCE FROM AccountLog / ContactInfoLog / ProfileInfoLog
----------------------------------------------------------------------------
LocationInfoLog's FK to the thing it's logging about is named
`location` (pointing at LocationInfo, whose own `user` is a further
hop away -- see location_info.py, where `LocationInfo.user` is the
primary key). So `user_link` below reads `obj.location.user`, and
search_fields/select_related paths go through `location__user__...`,
the same shape as the other three sibling admin files.

search_fields ALSO INCLUDES city/country
--------------------------------------------
Same reasoning as profile_info_log_admin.py including profile_name/
profile_name_slug: a common reason to look up a LocationInfoLog row is
"what changed for this city/country," not just "what changed for this
account." location__city and location__country are included alongside
the usual account identifiers for that reason.

WHAT THIS DOESN'T COVER
---------------------------
location_info_log.py's own docstring flags that the broader GDPR gap
in location_info.py (User.soft_delete() not yet knowing about
LocationInfo -- see that file's "NOT YET WIRED" note) is unaffected by
this admin registration. This file only makes existing LocationInfoLog
rows browsable; it doesn't add any new logging or wire up the missing
soft-delete cascade itself.
"""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.customer.models.location_info_log import LocationInfoLog
from apps.customer.admin.account_log_admin import (
    ReadOnlyAuditAdminMixin,
    _action_badge_html,
    _changes_html,
)


# ====================================================================
# ACTION BADGE COLORS
# ====================================================================
# Same "colored badge per choice value" pattern as the other
# *_log_admin.py modules' _ACTION_COLORS, keyed to LocationInfoLog's
# own Action choices.
_ACTION_COLORS = {
    LocationInfoLog.Action.CREATED: '#28a745',               # Green
    LocationInfoLog.Action.UPDATED: '#6c757d',                # Gray
    LocationInfoLog.Action.REGION_CHANGED: '#17a2b8',         # Cyan
    LocationInfoLog.Action.ADDRESS_CHANGED: '#17a2b8',        # Cyan
    LocationInfoLog.Action.COORDINATES_SET: '#28a745',        # Green
    LocationInfoLog.Action.COORDINATES_CHANGED: '#007bff',    # Blue
    LocationInfoLog.Action.COORDINATES_CLEARED: '#fd7e14',    # Orange
}
_DEFAULT_ACTION_COLOR = '#6c757d'  # Gray, for any Action value not in the map above


# ====================================================================
# INLINE (optional -- see account_log_admin.py's docstring for how
# to attach an inline like this to a ModelAdmin)
# ====================================================================

class LocationInfoLogInline(ReadOnlyAuditAdminMixin, admin.TabularInline):
    """
    Read-only inline showing a LocationInfo row's own change history --
    intended for attaching to a future LocationInfoAdmin (not defined
    here; location_info.py's own admin registration, if any, is out of
    scope for this file), the same way AccountLogInline is meant for
    UserAdmin.
    """

    model = LocationInfoLog
    fk_name = 'location'
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
# LOCATION INFO LOG ADMIN
# ====================================================================

@admin.register(LocationInfoLog)
class LocationInfoLogAdmin(ReadOnlyAuditAdminMixin, admin.ModelAdmin):
    """Standalone admin for browsing/searching LocationInfoLog across all users."""

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
        'location__user__email_or_phone',
        'location__user__email',
        'location__user__phone',
        'location__city',
        'location__country',
        'performed_by__email_or_phone',
        'ip_address',
    )

    date_hierarchy = 'created_at'

    ordering = ('-created_at',)

    # ================================================================
    # DETAIL VIEW
    # ================================================================

    fields = (
        'location',
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

    @admin.display(description='User', ordering='location__user__email_or_phone')
    def user_link(self, obj):
        """
        obj.location is guaranteed non-null (LocationInfoLog.location
        has no null=True), and obj.location.user is that row's primary
        key, so no None-guard is needed here.
        """
        return obj.location.user.email_or_phone

    @admin.display(description='Action')
    def action_badge(self, obj):
        return _action_badge_html(obj.action, obj.get_action_display(), _ACTION_COLORS, _DEFAULT_ACTION_COLOR)

    @admin.display(description='Changed Fields')
    def changed_fields_summary(self, obj):
        """
        Compact one-line summary for the list view (e.g. "city,
        country") -- the full old/new values are one click away on the
        detail page via changes_display.
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
        return qs.select_related('location__user', 'performed_by')