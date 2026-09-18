# apps/customer/admin/social_info_admin.py
# (SocialInfo Admin)

"""
Django Admin Configuration for SocialInfo Model
---------------------------------------------------
Features:
- At-a-glance link count / "connected accounts" summary
- Category-grouped fieldsets (mainstream, professional, video/audio,
  writing/community, interests, website) matching the model's own
  section comments
- Domain-mismatch validation errors surface normally through
  full_clean() on save -- no admin-specific validation duplicated here

DESIGN NOTES
------------------------------------------------------------------
- This is a WIDE model by design (30+ URL fields) -- see the model's
  own "ON THE SHEER NUMBER OF FIELDS" note. The list view deliberately
  does NOT try to render a column per platform; that would make the
  changelist unusable. Instead it shows a link count and a short
  preview of which platforms are actually set, with the full set only
  on the change form, grouped into the same categories the model uses.
- No custom "verify each platform" actions, unlike ContactInfoAdmin --
  this model has no per-platform verification flags at all (see the
  model's own note under "ON THE SHEER NUMBER OF FIELDS" about that
  being a possible future refactor, not present now). Don't invent
  verification UI for a field that doesn't exist yet.
- No bulk export action, same reasoning as LocationInfoAdmin/
  ContactInfoAdmin: even though social handles are lower-sensitivity
  than an address or phone number (per the model's own docstring),
  they're still user-identifying PII in aggregate, so a queryset-wide
  dump stays out of the admin action list.
- Like LocationInfo, this model is lazily created (no post_save
  signal), so an empty changelist relative to total users is expected,
  not a bug.
- Like LocationInfo, there's no soft-delete/archive flag on this
  model, so `has_delete_permission` is left at Django's default
  permission-based behavior rather than the superuser-only gate used
  on ProfileInfoAdmin/ContactInfoAdmin.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.urls import reverse

from apps.customer.models.social_info import SocialInfo


# ====================================================================
# CUSTOM FILTERS
# ====================================================================

class HasAnyLinkFilter(admin.SimpleListFilter):
    """Filter rows by whether at least one platform link is set"""
    title = _('Has Any Link')
    parameter_name = 'has_any_link'

    def lookups(self, request, model_admin):
        return (
            ('yes', _('Has at least one link')),
            ('no', _('No links set')),
        )

    def queryset(self, request, queryset):
        from django.db.models import Q
        link_fields = list(SocialInfo.PLATFORM_DOMAINS.keys())
        filled = Q()
        for field_name in link_fields:
            filled |= Q(**{f"{field_name}__isnull": False}) & ~Q(**{field_name: ''})

        if self.value() == 'yes':
            return queryset.filter(filled).distinct()
        elif self.value() == 'no':
            return queryset.exclude(filled)
        return queryset


class HasWebsiteFilter(admin.SimpleListFilter):
    """Filter rows by whether a personal/business website is set"""
    title = _('Website')
    parameter_name = 'has_website'

    def lookups(self, request, model_admin):
        return (
            ('yes', _('Has website')),
            ('no', _('No website')),
        )

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.exclude(website_url__isnull=True).exclude(website_url='')
        elif self.value() == 'no':
            return queryset.filter(website_url__isnull=True) | queryset.filter(website_url='')
        return queryset


# ====================================================================
# SOCIAL INFO ADMIN
# ====================================================================

@admin.register(SocialInfo)
class SocialInfoAdmin(admin.ModelAdmin):
    """
    Admin interface for SocialInfo model
    """

    # ================================================================
    # LIST DISPLAY
    # ================================================================

    list_display = (
        'user_link',
        'link_count_badge',
        'links_preview',
        'website_link',
        'updated_at',
    )

    list_display_links = ('user_link',)

    list_filter = (
        HasAnyLinkFilter,
        HasWebsiteFilter,
        'created_at',
    )

    search_fields = (
        'user__email',
        'user__email_or_phone',
        'uuid',
        'website_url',
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

    @admin.display(description='Links')
    def link_count_badge(self, obj):
        """Display total number of platform links set"""
        count = obj.link_count
        color = '#28a745' if count > 0 else '#6c757d'
        return format_html(
            '<span style="background-color: {}; color: white; '
            'padding: 3px 8px; border-radius: 3px; font-size: 11px; '
            'font-weight: bold;">{}</span>',
            color,
            count
        )

    @admin.display(description='Connected Platforms')
    def links_preview(self, obj):
        """
        Show a short comma-separated preview of which platforms are
        set, not the URLs themselves -- keeps the changelist readable
        for a model with 30+ possible link fields. Full URLs are on
        the change form.
        """
        links = obj.all_links
        if not links:
            return format_html('<span style="color: #999;">None</span>')

        # Human-readable platform names, e.g. "instagram_url" -> "Instagram"
        names = sorted(
            field_name.replace('_url', '').replace('_', ' ').title()
            for field_name in links.keys()
        )

        preview_limit = 5
        shown = names[:preview_limit]
        remainder = len(names) - len(shown)

        text = ', '.join(shown)
        if remainder > 0:
            text += f' (+{remainder} more)'

        return text

    @admin.display(description='Website')
    def website_link(self, obj):
        """Clickable link to the user's personal/business website, if set"""
        if obj.website_url:
            return format_html(
                '<a href="{}" target="_blank" rel="noopener">Visit ↗</a>',
                obj.website_url
            )
        return format_html('<span style="color: #999;">—</span>')

    # ================================================================
    # FIELDSETS
    # ================================================================

    fieldsets = (
        (_('User'), {
            'fields': ('user', 'uuid'),
        }),
        (_('Mainstream Social'), {
            'fields': (
                'facebook_url',
                'instagram_url',
                'twitter_url',
                'threads_url',
                'snapchat_url',
                'pinterest_url',
                'tiktok_url',
                'bluesky_url',
                'mastodon_url',
                'reddit_url',
                'tumblr_url',
                'vk_url',
                'weibo_url',
                'line_url',
                'wechat_url',
            ),
            'classes': ('collapse',),
        }),
        (_('Professional'), {
            'fields': (
                'linkedin_url',
                'github_url',
                'gitlab_url',
                'behance_url',
                'dribbble_url',
            ),
            'classes': ('collapse',),
        }),
        (_('Video / Streaming / Audio'), {
            'fields': (
                'youtube_url',
                'twitch_url',
                'vimeo_url',
                'spotify_url',
                'soundcloud_url',
                'kick_url',
            ),
            'classes': ('collapse',),
        }),
        (_('Writing / Community'), {
            'fields': (
                'medium_url',
                'substack_url',
                'quora_url',
                'discord_url',
                'telegram_url',
                'whatsapp_url',
            ),
            'classes': ('collapse',),
        }),
        (_('Interests / Niche Communities'), {
            'fields': (
                'goodreads_url',
                'letterboxd_url',
                'strava_url',
            ),
            'classes': ('collapse',),
        }),
        (_('Website'), {
            'fields': ('website_url',),
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