# apps/customer/admin/location_info_admin.py
# (LocationInfo Admin)

"""
Django Admin Configuration for LocationInfo Model
---------------------------------------------------
Features:
- Address/location management
- Coordinate display and validation status
- Map link shortcut
- GDPR-sensitive data, so no bulk export action here (see note below)

NOTES
------------------------------------------------------------------
- LocationInfo is NOT auto-created for every user (see the model's
  own docstring -- it's lazy, via get_or_create_for_user()). That
  means, unlike ProfileInfoAdmin, there's no assumption anywhere here
  that a row exists; the changelist will simply be sparser than the
  user table, which is expected.
- There's no `is_profile_archived`-equivalent soft-delete flag on this
  model, so `has_delete_permission` is left at the default (normal
  permission-based delete) rather than the superuser-only hard-delete
  gate ProfileInfoAdmin uses. If a soft-delete/GDPR-scrub story gets
  added to the model later (see its docstring's "NOT YET WIRED" note),
  this admin should be revisited to match.
- No bulk "export data" admin action is provided even though the model
  has export_data() -- address + coordinates is sensitive PII, and an
  admin action makes it one click to dump many users' locations into
  a message/log. If GDPR export needs an admin entry point, prefer a
  per-object action gated more carefully than a queryset action.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.urls import reverse

from apps.customer.models.location_info import LocationInfo


# ====================================================================
# CUSTOM FILTERS
# ====================================================================

class HasCoordinatesFilter(admin.SimpleListFilter):
    """Filter rows by whether a lat/long pair is set"""
    title = _('Coordinates')
    parameter_name = 'has_coordinates'

    def lookups(self, request, model_admin):
        return (
            ('yes', _('Has coordinates')),
            ('no', _('No coordinates')),
        )

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.filter(latitude__isnull=False, longitude__isnull=False)
        elif self.value() == 'no':
            return queryset.filter(latitude__isnull=True, longitude__isnull=True)
        return queryset


class HasAddressFilter(admin.SimpleListFilter):
    """Filter rows by whether any free-text address field is filled in"""
    title = _('Address Filled')
    parameter_name = 'has_address'

    def lookups(self, request, model_admin):
        return (
            ('yes', _('Has address')),
            ('no', _('No address')),
        )

    def queryset(self, request, queryset):
        from django.db.models import Q
        filled = Q(address__isnull=False) & ~Q(address='') | \
                 Q(present_address__isnull=False) & ~Q(present_address='') | \
                 Q(permanent_address__isnull=False) & ~Q(permanent_address='')
        if self.value() == 'yes':
            return queryset.filter(filled)
        elif self.value() == 'no':
            return queryset.exclude(filled)
        return queryset


# ====================================================================
# LOCATION INFO ADMIN
# ====================================================================

@admin.register(LocationInfo)
class LocationInfoAdmin(admin.ModelAdmin):
    """
    Admin interface for LocationInfo model
    """

    # ================================================================
    # LIST DISPLAY
    # ================================================================

    list_display = (
        'user_link',
        'location_display_col',
        'country_code',
        'coordinates_badge',
        'map_link',
        'updated_at',
    )

    list_display_links = ('user_link', 'location_display_col')

    list_filter = (
        HasCoordinatesFilter,
        HasAddressFilter,
        'country',
        'state',
        'created_at',
    )

    search_fields = (
        'user__email',
        'user__email_or_phone',
        'country',
        'country_code',
        'state',
        'city',
        'postal_code',
        'uuid',
    )

    ordering = ('-updated_at',)

    date_hierarchy = 'created_at'

    # ================================================================
    # CUSTOM DISPLAY METHODS
    # ================================================================

    @admin.display(description='User', ordering='user__email_or_phone')
    def user_link(self, obj):
        """Display link to user admin"""
        url = reverse('admin:customer_user_change', args=[obj.user.pk])
        return format_html(
            '<a href="{}">{}</a>',
            url,
            obj.user.email_or_phone
        )

    @admin.display(description='Location')
    def location_display_col(self, obj):
        """Display formatted city/state/country, falling back to a placeholder"""
        display = obj.location_display
        if display:
            return display
        return format_html('<span style="color: #999;">No location set</span>')

    @admin.display(description='Coordinates')
    def coordinates_badge(self, obj):
        """Display whether GPS coordinates are set"""
        if obj.has_coordinates:
            return format_html(
                '<span style="background-color: #28a745; color: white; '
                'padding: 3px 8px; border-radius: 3px; font-size: 10px;">'
                '📍 {}, {}</span>',
                obj.latitude,
                obj.longitude
            )
        return format_html(
            '<span style="background-color: #6c757d; color: white; '
            'padding: 3px 8px; border-radius: 3px; font-size: 10px;">NO COORDS</span>'
        )

    @admin.display(description='Map')
    def map_link(self, obj):
        """
        Link out to a map. Prefers the user-supplied location_url;
        falls back to generating a Google Maps query from lat/long if
        that's set instead. Neither is derived/stored on the model
        itself -- see the model docstring's note on why location_url
        isn't auto-populated from coordinates.
        """
        if obj.location_url:
            return format_html(
                '<a href="{}" target="_blank" rel="noopener">Open link ↗</a>',
                obj.location_url
            )
        if obj.has_coordinates:
            url = f"https://maps.google.com/?q={obj.latitude},{obj.longitude}"
            return format_html(
                '<a href="{}" target="_blank" rel="noopener">View on map ↗</a>',
                url
            )
        return format_html('<span style="color: #999;">—</span>')

    # ================================================================
    # FIELDSETS
    # ================================================================

    fieldsets = (
        (_('User'), {
            'fields': ('user', 'uuid'),
        }),
        (_('Address'), {
            'fields': (
                'country',
                'country_code',
                'state',
                'city',
                'postal_code',
                'address',
                'present_address',
                'permanent_address',
            ),
            'description': _(
                'Note: `address`, `present_address`, and `permanent_address` '
                'are three distinct fields, not alternatives -- see the model '
                'docstring for the overlap this creates.'
            ),
        }),
        (_('Coordinates & Map'), {
            'fields': ('latitude', 'longitude', 'location_url'),
            'description': _(
                'Latitude/longitude must both be set or both left empty. '
                'location_url is independent and not auto-derived from them.'
            ),
        }),
        (_('Timestamps'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    # ================================================================
    # READONLY FIELDS
    # ================================================================

    def get_readonly_fields(self, request, obj=None):
        readonly = ['uuid', 'created_at', 'updated_at']

        # Make user readonly after creation, same convention as
        # ProfileInfoAdmin -- a location row shouldn't be re-parented
        # to a different user after the fact.
        if obj:
            readonly.append('user')

        return readonly

    # ================================================================
    # ADDITIONAL CONFIGURATIONS
    # ================================================================

    def get_queryset(self, request):
        """Optimize queryset with select_related on the user FK"""
        qs = super().get_queryset(request)
        return qs.select_related('user')