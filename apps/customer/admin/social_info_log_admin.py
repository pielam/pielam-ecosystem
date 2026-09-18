# apps/customer/admin/social_info_log_admin.py

"""
Django Admin Configuration for SocialInfoLog
------------------------------------------------
Same read-only audit-trail admin as account_log_admin.py /
contact_info_log_admin.py / profile_info_log_admin.py /
location_info_log_admin.py, adapted for SocialInfoLog
(apps/customer/models/social_info_log.py). See account_log_admin.py's
module docstring for the full reasoning behind the permission model
(no add, no edit, superuser-only delete) and the wiring caveat about
admin autodiscovery not recursing into sibling files -- both apply
identically here and aren't repeated in full below.

REUSE FROM account_log_admin.py
----------------------------------
`ReadOnlyAuditAdminMixin`, `_action_badge_html()`, and `_changes_html()`
are imported rather than redefined -- same reuse pattern the other
three sibling admin files already established.

WHY THIS FILE HAS A CUSTOM LIST FILTER THE OTHERS DON'T
------------------------------------------------------------
social_info_log.py deliberately keeps `action` coarse-grained
(LINK_ADDED / LINK_REMOVED / LINK_CHANGED / MULTIPLE_LINKS_CHANGED)
rather than having one Action value per platform -- see that file's
own docstring for why ("the exact churn problem social_info.py's own
docstring warns about"). That means `list_filter`'s `action` entry
alone can't answer "show me every Instagram link change" the way
ProfileInfoLogAdmin's `action` filter can answer "show me every
suspension." SocialPlatformFilter below fills that gap, using the same
`changes__<field>__isnull=False` query social_info_log.py's own
`SocialInfoLogManager.for_platform()` uses -- this is the admin-UI
surface for that manager method, not a separate mechanism.

FIELD NAME DIFFERENCE FROM THE OTHER *_log MODELS
------------------------------------------------------
SocialInfoLog's FK to the thing it's logging about is named `social`
(pointing at SocialInfo, whose own `user` is a further hop away -- see
social_info.py, where `SocialInfo.user` is the primary key). So
`user_link` below reads `obj.social.user`, and search_fields/
select_related paths go through `social__user__...`, the same shape
as the other three sibling admin files.
"""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.customer.models.social_info_log import SocialInfoLog, TRACKED_FIELDS
from apps.customer.admin.account_log_admin import (
    ReadOnlyAuditAdminMixin,
    _action_badge_html,
    _changes_html,
)


# ====================================================================
# ACTION BADGE COLORS
# ====================================================================
# Same "colored badge per choice value" pattern as the other
# *_log_admin.py modules' _ACTION_COLORS, keyed to SocialInfoLog's own
# (deliberately small/coarse) Action choices.
_ACTION_COLORS = {
    SocialInfoLog.Action.CREATED: '#28a745',                 # Green
    SocialInfoLog.Action.UPDATED: '#6c757d',                 # Gray
    SocialInfoLog.Action.LINK_ADDED: '#28a745',               # Green
    SocialInfoLog.Action.LINK_REMOVED: '#dc3545',             # Red
    SocialInfoLog.Action.LINK_CHANGED: '#17a2b8',             # Cyan
    SocialInfoLog.Action.MULTIPLE_LINKS_CHANGED: '#fd7e14',  # Orange
}
_DEFAULT_ACTION_COLOR = '#6c757d'  # Gray, for any Action value not in the map above


# ====================================================================
# CUSTOM FILTER
# ====================================================================

class SocialPlatformFilter(admin.SimpleListFilter):
    """
    Filter SocialInfoLog rows by which platform field appears in their
    `changes` diff -- the admin-UI counterpart to
    SocialInfoLogManager.for_platform() in social_info_log.py. Exists
    because `action` itself doesn't encode platform (see module
    docstring); this is the only way to answer "show me every LinkedIn
    change" from the list view.

    Lookups are built from TRACKED_FIELDS (imported from
    social_info_log.py, itself derived from
    SocialInfo.PLATFORM_DOMAINS) rather than hand-listed, for the same
    reason social_info_log.py derives TRACKED_FIELDS instead of
    hand-copying the platform list: a new platform field is
    automatically filterable here the moment it's added there, with no
    changes needed in this file.
    """

    title = _('Platform')
    parameter_name = 'platform'

    def lookups(self, request, model_admin):
        return tuple((field, field.replace('_url', '').replace('_', ' ').title()) for field in TRACKED_FIELDS)

    def queryset(self, request, queryset):
        field = self.value()
        if field and field in TRACKED_FIELDS:
            return queryset.filter(**{f"changes__{field}__isnull": False})
        return queryset


# ====================================================================
# INLINE (optional -- see account_log_admin.py's docstring for how
# to attach an inline like this to a ModelAdmin)
# ====================================================================

class SocialInfoLogInline(ReadOnlyAuditAdminMixin, admin.TabularInline):
    """
    Read-only inline showing a SocialInfo row's own change history --
    intended for attaching to a future SocialInfoAdmin (not defined
    here; social_info.py's own admin registration, if any, is out of
    scope for this file), the same way AccountLogInline is meant for
    UserAdmin.
    """

    model = SocialInfoLog
    fk_name = 'social'
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
# SOCIAL INFO LOG ADMIN
# ====================================================================

@admin.register(SocialInfoLog)
class SocialInfoLogAdmin(ReadOnlyAuditAdminMixin, admin.ModelAdmin):
    """Standalone admin for browsing/searching SocialInfoLog across all users."""

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
        SocialPlatformFilter,
        'created_at',
    )

    search_fields = (
        'social__user__email_or_phone',
        'social__user__email',
        'social__user__phone',
        'performed_by__email_or_phone',
        'ip_address',
    )

    date_hierarchy = 'created_at'

    ordering = ('-created_at',)

    # ================================================================
    # DETAIL VIEW
    # ================================================================

    fields = (
        'social',
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

    @admin.display(description='User', ordering='social__user__email_or_phone')
    def user_link(self, obj):
        """
        obj.social is guaranteed non-null (SocialInfoLog.social has no
        null=True), and obj.social.user is that row's primary key, so
        no None-guard is needed here.
        """
        return obj.social.user.email_or_phone

    @admin.display(description='Action')
    def action_badge(self, obj):
        return _action_badge_html(obj.action, obj.get_action_display(), _ACTION_COLORS, _DEFAULT_ACTION_COLOR)

    @admin.display(description='Changed Fields')
    def changed_fields_summary(self, obj):
        """
        Compact one-line summary for the list view (e.g. "github_url,
        linkedin_url") -- the full old/new URLs are one click away on
        the detail page via changes_display. This is also where a
        multi-platform change (MULTIPLE_LINKS_CHANGED) is actually
        distinguishable from a single one, since the badge alone
        doesn't name the platforms.
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
        return qs.select_related('social__user', 'performed_by')