# apps/customer/admin/user_info_admin.py

"""
Django Admin Configuration for the UserInfo aggregation layer
----------------------------------------------------------------
IMPORTANT: apps/customer/models/user_info.py does NOT define a Django
model. `UserInfo` is a plain @dataclass wrapping User + four related
rows (ProfileInfo/ContactInfo/LocationInfo/SocialInfo), and
`get_user_info()` is a function, not a manager method -- there's no
table to register with admin.site.register() directly.

To still give this aggregation a browsable admin surface without
duplicating User's own admin (apps/customer/admin/account_admin.py
already owns that), this registers a *proxy model* of User:
`UserInfoProxy`. A proxy model adds no new table/migration -- it's the
same `customer_user` DB table, just a second admin.site registration
under a different name/label, purely so "aggregated view across all
five tables" can live as its own menu entry instead of being bolted
onto UserAdmin.

DESIGN NOTES
------------------------------------------------------------------
- Entirely read-only. This is a read/reporting surface over data that
  already has its own dedicated, writable admins (UserAdmin,
  ProfileInfoAdmin, ContactInfoAdmin, LocationInfoAdmin,
  SocialInfoAdmin) -- editing here would just be a confusing second
  path to change the same rows. has_add/change/delete_permission all
  return False.
- list_display intentionally does NOT call get_user_info() per row.
  That helper does up to 2 extra queries per call (LocationInfo/
  SocialInfo aren't guaranteed OneToOne-select_related-able -- see its
  own docstring), so calling it once per row in a changelist would be
  an N+1 query multiplier on top of the list's own pagination query.
  Instead, get_queryset() select_related()s the two OneToOne rows
  (profileinfo/contactinfo) for list display, and list columns touch
  location/social only via cheap hasattr-guarded lookups.
- The detail (change) view is the one place get_user_info() is called
  -- once, for a single object, which is exactly the cost profile it
  was designed for. That's where the full aggregated read (including
  the merged GDPR export) is actually rendered.
- No search/filter duplicate everything UserAdmin already exposes;
  this mirrors UserAdmin's identity-facing filters/search only, since
  drilling into role/security/MFA specifics belongs on UserAdmin
  itself, not this aggregation view.
"""

import json

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.db.models import Q

from apps.customer.models.account import User
from apps.customer.models.user_info import get_user_info


# ====================================================================
# PROXY MODEL
# ====================================================================

class UserInfoProxy(User):
    """
    Proxy model of User purely so this aggregated view can have its
    own entry in the admin index, separate from UserAdmin. Same table,
    same fields, same manager -- no migration needed beyond the
    initial proxy registration Django requires (makemigrations will
    generate a no-op CreateModel-with-proxy=True entry).
    """

    class Meta:
        proxy = True
        verbose_name = _("User Info (Aggregated)")
        verbose_name_plural = _("User Info (Aggregated)")


# ====================================================================
# CUSTOM FILTERS (mirrors the identity-facing subset of UserAdmin's)
# ====================================================================

class HasContactInfoFilter(admin.SimpleListFilter):
    """Filter by whether a ContactInfo row exists at all (it's optional/lazy)."""
    title = _('Has Contact Info')
    parameter_name = 'has_contact'

    def lookups(self, request, model_admin):
        return (
            ('yes', _('Yes')),
            ('no', _('No')),
        )

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.filter(contactinfo__isnull=False)
        elif self.value() == 'no':
            return queryset.filter(contactinfo__isnull=True)
        return queryset


class VerifiedAnywhereFilter(admin.SimpleListFilter):
    """
    Filter by UserInfo.is_verified()'s definition: verified account
    (email/phone) OR verified profile -- broader than either flag
    alone, matching what the aggregation layer actually reports.
    """
    title = _('Verified (account or profile)')
    parameter_name = 'verified_anywhere'

    def lookups(self, request, model_admin):
        return (
            ('yes', _('Verified')),
            ('no', _('Not Verified')),
        )

    def queryset(self, request, queryset):
        condition = (
            Q(email_verified=True)
            | Q(phone_verified=True)
            | Q(profileinfo__is_profile_verified=True)
        )
        if self.value() == 'yes':
            return queryset.filter(condition).distinct()
        elif self.value() == 'no':
            return queryset.exclude(condition).distinct()
        return queryset


# ====================================================================
# USER INFO (AGGREGATED) ADMIN
# ====================================================================

@admin.register(UserInfoProxy)
class UserInfoAdmin(admin.ModelAdmin):
    """
    Read-only aggregated view across User + ProfileInfo + ContactInfo +
    LocationInfo + SocialInfo, mirroring what get_user_info() assembles
    at the application layer.
    """

    # ================================================================
    # LIST DISPLAY
    # ================================================================

    list_display = (
        'email_or_phone',
        'display_name_col',
        'verified_anywhere_badge',
        'has_contact_badge',
        'has_location_badge',
        'has_social_badge',
        'date_joined',
    )

    list_display_links = ('email_or_phone', 'display_name_col')

    list_filter = (
        HasContactInfoFilter,
        VerifiedAnywhereFilter,
        'is_active',
        'account_status',
        'date_joined',
    )

    search_fields = (
        'email_or_phone',
        'email',
        'phone',
        'uuid',
    )

    ordering = ('-date_joined',)

    # ================================================================
    # LIST COLUMNS -- cheap, no get_user_info() call per row
    # ================================================================

    @admin.display(description='Display Name')
    def display_name_col(self, obj):
        return obj.display_name

    @admin.display(description='Verified', boolean=True)
    def verified_anywhere_badge(self, obj):
        profile = getattr(obj, 'profileinfo', None)
        profile_verified = bool(profile and profile.is_profile_verified)
        return obj.is_verified or profile_verified

    @admin.display(description='Contact Info', boolean=True)
    def has_contact_badge(self, obj):
        return hasattr(obj, 'contactinfo')

    @admin.display(description='Location Info', boolean=True)
    def has_location_badge(self, obj):
        return hasattr(obj, 'locationinfo')

    @admin.display(description='Social Info', boolean=True)
    def has_social_badge(self, obj):
        return hasattr(obj, 'socialinfo')

    # ================================================================
    # DETAIL VIEW -- the one place the full aggregation runs
    # ================================================================

    fieldsets = (
        (_('Identity'), {
            'fields': ('email_or_phone_display', 'display_name_display', 'verified_display'),
        }),
        (_('Location'), {
            'fields': ('location_display_field',),
        }),
        (_('Visible Contact Methods'), {
            'fields': ('contact_methods_display',),
        }),
        (_('Social Links'), {
            'fields': ('social_links_display',),
        }),
        (_('Merged GDPR Export'), {
            'fields': ('export_data_display',),
            'description': _(
                'Exactly what UserInfo.export_data() would return for this '
                'user -- nested per sub-model, omitting any that don\'t exist.'
            ),
        }),
    )

    readonly_fields = (
        'email_or_phone_display',
        'display_name_display',
        'verified_display',
        'location_display_field',
        'contact_methods_display',
        'social_links_display',
        'export_data_display',
    )

    def _get_info(self, obj):
        """
        Build (and cache on the request-scoped obj) the UserInfo
        aggregation for this one object -- called from several
        readonly_fields methods for the same obj, so cache it on the
        instance to avoid redoing the LocationInfo/SocialInfo lookups
        for every field on the same page render.
        """
        if not hasattr(obj, '_cached_user_info'):
            obj._cached_user_info = get_user_info(obj)
        return obj._cached_user_info

    @admin.display(description='Email or Phone')
    def email_or_phone_display(self, obj):
        return obj.email_or_phone

    @admin.display(description='Display Name')
    def display_name_display(self, obj):
        return self._get_info(obj).display_name()

    @admin.display(description='Verified')
    def verified_display(self, obj):
        return '✓ Verified' if self._get_info(obj).is_verified() else '✗ Not Verified'

    @admin.display(description='Location')
    def location_display_field(self, obj):
        return self._get_info(obj).location_display() or '—'

    @admin.display(description='Contact Methods')
    def contact_methods_display(self, obj):
        methods = self._get_info(obj).visible_contact_methods(viewer=obj)
        if not methods:
            return '—'
        return format_html(
            '<pre style="margin:0;">{}</pre>',
            json.dumps(methods, indent=2, default=str)
        )

    @admin.display(description='Social Links')
    def social_links_display(self, obj):
        links = self._get_info(obj).social_links()
        if not links:
            return '—'
        return format_html(
            '<pre style="margin:0;">{}</pre>',
            json.dumps(links, indent=2, default=str)
        )

    @admin.display(description='Export Data')
    def export_data_display(self, obj):
        data = self._get_info(obj).export_data()
        return format_html(
            '<pre style="margin:0; max-height: 500px; overflow:auto;">{}</pre>',
            json.dumps(data, indent=2, default=str)
        )

    # ================================================================
    # READ-ONLY ENFORCEMENT
    # ================================================================

    def get_queryset(self, request):
        """
        select_related the two OneToOne rows for cheap list-column
        access; LocationInfo/SocialInfo stay as hasattr-guarded lookups
        (see module docstring -- they aren't select_related-safe since
        they may not exist).
        """
        return super().get_queryset(request).select_related('profileinfo', 'contactinfo')

    def has_add_permission(self, request):
        """This is a read-only aggregation; create users via UserAdmin."""
        return False

    def has_change_permission(self, request, obj=None):
        """
        Django still requires "change" permission to view the detail
        page for a ModelAdmin with no separate view-only mode pre-4.1
        patterns; every field on this admin is readonly regardless, so
        granting this doesn't allow any actual mutation.
        """
        return request.user.is_staff

    def has_delete_permission(self, request, obj=None):
        """Delete users via UserAdmin (which routes through soft_delete())."""
        return False