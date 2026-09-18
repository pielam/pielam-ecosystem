from __future__ import annotations

import datetime

from django.contrib import admin
from django.utils import timezone as dj_timezone
from django.utils.html import format_html

from megamind.models.visit_log import DiscoveryVisitLog

__all__ = ['DiscoveryVisitLogAdmin']


# Badge colors per DeviceType choice. Anything not listed falls back to
# the 'unknown' gray. Kept here (not in the model) since this is purely
# a presentation concern for this admin.
_DEVICE_TYPE_COLORS = {
    DiscoveryVisitLog.DeviceType.DESKTOP: '#2563eb',  # blue
    DiscoveryVisitLog.DeviceType.MOBILE: '#16a34a',   # green
    DiscoveryVisitLog.DeviceType.TABLET: '#9333ea',   # purple
    DiscoveryVisitLog.DeviceType.BOT: '#dc2626',      # red
    DiscoveryVisitLog.DeviceType.UNKNOWN: '#6b7280',  # gray
}


def _country_flag_emoji(iso_code: str) -> str:
    """
    Convert an ISO 3166-1 alpha-2 code (e.g. 'BD') into its flag emoji
    by mapping each letter to a Unicode regional indicator symbol.
    Returns '' for anything that isn't exactly 2 letters (empty/unset
    country values are common right now since GeoIP capture isn't
    wired up yet).
    """
    code = (iso_code or '').strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ''
    return ''.join(chr(0x1F1E6 + ord(ch) - ord('A')) for ch in code)


@admin.register(DiscoveryVisitLog)
class DiscoveryVisitLogAdmin(admin.ModelAdmin):
    """
    Read-only admin for DiscoveryVisitLog.

    The model itself enforces immutability at the ORM level (save()/delete()
    raise on existing rows), but that only blocks form-based edits, not
    someone opening the change form and clicking Save on unchanged data,
    or a stray admin action. This ModelAdmin additionally disables
    add/change/delete permissions entirely so the admin is strictly a
    read-only inspection tool for this audit trail, matching the model's
    "append-only" contract end to end.

    Bulk purging (e.g. by retention_expires_at) should still go through a
    dedicated management command / scheduled task using queryset.delete(),
    not the admin.

    Also renders a visitor-analytics summary (today / this week / this
    month / this year) above the change list — see changelist_view()
    and _get_visitor_analytics() below, backed by
    templates/admin/megamind/discoveryvisitlog/change_list.html, which
    carries its own inline <style> block (no separate static CSS file).
    """

    # Styling lives inline in change_list.html (a <style> block) rather
    # than a separate static CSS file, so there's no Media class here
    # and nothing to collectstatic.
    change_list_template = 'admin/megamind/discoveryvisitlog/change_list.html'

    # ── List view ────────────────────────────────────────────────
    list_display = (
        'visited_at',
        'user_display',
        'is_authenticated',
        'device_type_badge',
        'country_display',
        'ip_address',
        'is_likely_bot',
        'is_proxy_or_vpn',
        'utm_source',
        'search_query',
        'product',
        'results_count',
    )
    list_display_links = ('visited_at', 'user_display')
    list_filter = (
        'is_authenticated',
        'device_type',
        'connection_type',
        'country',
        'is_likely_bot',
        'is_proxy_or_vpn',
        'is_tor_exit_node',
        'is_datacenter_ip',
        'is_first_visit',
        'is_returning_visitor',
        'data_classification',
        'gdpr_applicable',
        'ccpa_applicable',
        'environment',
        ('visited_at', admin.DateFieldListFilter),
    )
    search_fields = (
        'id',
        'user__username',
        'user__email',
        'ip_address',
        'session_key',
        'device_fingerprint',
        'request_id',
        'trace_id',
        'correlation_id',
        'referrer_domain',
        'search_query',
        'utm_campaign',
        'ja3_fingerprint',
        'product__product_name',
    )
    date_hierarchy = 'visited_at'
    ordering = ('-visited_at',)
    list_per_page = 50
    list_select_related = ('user', 'product')
    show_full_result_count = False  # this table can get huge; skip the COUNT(*) on every page load

    # ── Detail view ──────────────────────────────────────────────
    fieldsets = (
        ('Identity / correlation', {
            'fields': ('id', 'request_id', 'trace_id', 'correlation_id'),
        }),
        ('Who', {
            'fields': (
                'user', 'role_at_visit', 'is_authenticated', 'account_tier_at_visit',
                'session_key', 'device_fingerprint', 'is_first_visit',
                'is_returning_visitor', 'visit_sequence_number',
            ),
        }),
        ('Network / geo', {
            'fields': (
                'ip_address', 'country', 'region', 'city', 'postal_code',
                'latitude', 'longitude', 'isp', 'asn',
                'is_proxy_or_vpn', 'is_tor_exit_node', 'is_datacenter_ip',
                'connection_type',
            ),
        }),
        ('Client / device', {
            'fields': (
                'user_agent', 'device_type', 'browser_name', 'browser_version',
                'os_name', 'os_version', 'accept_language', 'timezone',
                'screen_width', 'screen_height', 'viewport_width', 'viewport_height',
                'pixel_ratio', 'color_depth',
            ),
        }),
        ('Security / trust signals', {
            'fields': (
                'tls_version', 'ja3_fingerprint', 'bot_score', 'is_likely_bot',
                'waf_action', 'threat_score',
            ),
        }),
        ('Acquisition / attribution', {
            'fields': (
                'referrer', 'referrer_domain', 'landing_page_path',
                'utm_source', 'utm_medium', 'utm_campaign', 'utm_term',
                'utm_content', 'click_id',
            ),
        }),
        ('Experimentation / product context', {
            'fields': (
                'ab_test_variant', 'feature_flags', 'client_app_version', 'environment',
            ),
        }),
        ('What they were looking at', {
            'fields': (
                'product', 'query_string', 'search_query', 'filter_slug', 'sort_by',
                'page_number', 'results_count',
            ),
        }),
        ('Performance', {
            'fields': ('server_response_time_ms',),
        }),
        ('Consent / data governance', {
            'fields': (
                'consent_given', 'consent_categories', 'gdpr_applicable',
                'ccpa_applicable', 'data_classification', 'retention_expires_at',
            ),
        }),
        ('Timestamp', {
            'fields': ('visited_at',),
        }),
    )

    # All fields on the model are editable=False already, but that only
    # hides them from *auto-generated* ModelForms — since this ModelAdmin
    # defines explicit fieldsets, we mirror that here too so the change
    # view renders every field as plain text rather than erroring out
    # trying to build widgets for non-editable fields.
    readonly_fields = [f.name for f in DiscoveryVisitLog._meta.fields]

    @admin.display(description='User', ordering='user')
    def user_display(self, obj: DiscoveryVisitLog) -> str:
        if obj.user_id:
            return str(obj.user)
        return f'anonymous ({obj.ip_address or "unknown ip"})'

    @admin.display(description='Device', ordering='device_type')
    def device_type_badge(self, obj: DiscoveryVisitLog) -> str:
        color = _DEVICE_TYPE_COLORS.get(obj.device_type, _DEVICE_TYPE_COLORS[DiscoveryVisitLog.DeviceType.UNKNOWN])
        return format_html(
            '<span class="visit-log-badge" style="background:{}">{}</span>',
            color, obj.get_device_type_display(),
        )

    @admin.display(description='Country', ordering='country')
    def country_display(self, obj: DiscoveryVisitLog) -> str:
        if not obj.country:
            return format_html('<span class="visit-log-muted">—</span>')
        flag = _country_flag_emoji(obj.country)
        return format_html('{} {}', flag, obj.country)

    # ── Lock the admin down to read-only ────────────────────────
    #
    # These four are independent Django admin permission hooks, not a
    # cascade — has_view_permission does NOT automatically follow from
    # has_change_permission being True. In fact the opposite trap is
    # what to watch for: ModelAdmin.has_view_permission() falls back to
    # has_change_permission() when it isn't overridden, so a naive
    # "has_change_permission = False, don't bother with view" setup can
    # leave staff unable to even open a row to look at it (unless they
    # separately hold the 'view' model permission). Defining
    # has_view_permission explicitly avoids depending on that fallback.
    def has_add_permission(self, request) -> bool:
        return False

    def has_view_permission(self, request, obj=None) -> bool:
        # Any staff user with access to this admin section can look at
        # rows. Tighten this to a permission/role check if visibility
        # into raw IP/device/geo data should be restricted further.
        return request.user.is_active and request.user.is_staff

    def has_change_permission(self, request, obj=None) -> bool:
        # Rows can be opened (has_view_permission above) but never
        # saved — this is what actually makes the change form render
        # read-only instead of just erroring out on submit.
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_selected_permission(self, request, obj=None) -> bool:  # admin action guard
        return False

    def get_actions(self, request):
        # Strip bulk actions (including the built-in "delete selected")
        # entirely — there is nothing this admin should be able to do
        # to rows besides look at them.
        return {}

    # ── Visitor analytics ────────────────────────────────────────
    #
    # "Visitor" = distinct (user_id, ip_address) pair within the period.
    # Logged-in users are counted once per account regardless of which
    # IP they visit from; anonymous visitors are counted once per IP.
    # This is a heuristic, not a verified identity (see the model's
    # DATA SOURCE TRUST docstring) — two people behind the same NAT'd
    # IP will undercount, and one person switching networks will
    # overcount. Good enough for a dashboard glance, not for billing.
    #
    # Periods are calendar-based (this week/month/year *so far*), using
    # the current timezone (settings.TIME_ZONE), not rolling windows.
    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['visitor_analytics'] = self._get_visitor_analytics()
        return super().changelist_view(request, extra_context=extra_context)

    def _get_visitor_analytics(self) -> list[dict]:
        now = dj_timezone.localtime(dj_timezone.now())
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_week = start_of_day - datetime.timedelta(days=start_of_day.weekday())  # Monday
        start_of_month = start_of_day.replace(day=1)
        start_of_year = start_of_day.replace(month=1, day=1)

        periods = (
            ('Today', start_of_day),
            ('This week', start_of_week),
            ('This month', start_of_month),
            ('This year', start_of_year),
        )

        stats = []
        for label, since in periods:
            qs = DiscoveryVisitLog.objects.filter(visited_at__gte=since)
            stats.append({
                'label': label,
                'unique_visitors': qs.values('user_id', 'ip_address').distinct().count(),
                'total_hits': qs.count(),
            })
        return stats