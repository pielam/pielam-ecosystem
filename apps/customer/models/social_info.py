# apps/customer/models/social_info.py

"""
SocialInfo -- holds a user's external social media links/handles
across a broad set of platforms (mainstream social, professional,
creative/portfolio, community, streaming, and messaging), plus a
generic personal website.

WHY A SEPARATE MODEL INSTEAD OF PUTTING IT ON ProfileInfo
------------------------------------------------------------------
Same reasoning as LocationInfo (see location_info.py): ProfileInfo is
trimmed to core identity/social-graph data (name, bio, photos,
followers). Social links are optional, sparse -- most users won't fill
in more than a handful of these, if any -- and they change
independently of core identity data, so they get their own table
rather than adding dozens of mostly-null columns to every ProfileInfo
row.

WHY THIS IS NOT AUTO-CREATED LIKE ProfileInfo IS
------------------------------------------------------------------
Same lazy-creation pattern as LocationInfo: there is deliberately no
post_save signal on User creating one of these. Use
SocialInfo.objects.get_or_create_for_user(user) instead, the same way
LocationInfo, cover_photo.py, and profile_photo.py lazily
get_or_create() their respective rows on first use.

ON THE SHEER NUMBER OF FIELDS
------------------------------------------------------------------
This is a wide, flat model by design (mirrors the original ask: as
many platforms as reasonably belong here). If/when per-platform
metadata is needed (verified badge, OAuth-connected vs. manually
pasted, follower count synced from the platform, display order the
user wants links shown in), this is the model to convert to a
ForeignKey + `platform`/`url`/`is_verified`/`sort_order` rows instead
of continuing to add flat columns. That refactor is out of scope here.

NOT YET WIRED: GDPR / soft-delete cascade
------------------------------------------------------------------
Same gap as LocationInfo: User.soft_delete() (account.py) and
signals.py's sync_profile_archive_state() don't currently know about
SocialInfo. Social handles are lower-sensitivity than an address, but
they're still user-identifying PII, so if "delete my account" should
scrub this too, that needs an explicit addition in signals.py (or a
call from soft_delete() itself) -- not included here since this file
is scoped to the model itself.

DELIBERATELY EXCLUDED
------------------------------------------------------------------
Adult/NSFW-oriented platforms (e.g. OnlyFans) and payment/handle
fields (Venmo, CashApp, etc.) are not included here -- different risk
and product category from general social presence links. Add a
separate model for those if the product actually needs them, rather
than folding them into this one.

ON VALIDATION
------------------------------------------------------------------
Each platform field stores a full profile URL (not a bare handle) --
consistent with `location_url` on LocationInfo. clean() checks that
each URL, if present, actually points at its expected domain (e.g.
`instagram_url` must contain "instagram.com") -- a cheap sanity check
against someone pasting the wrong link into the wrong field, not a
guarantee the profile exists or is theirs. A few platforms (X/Twitter,
WhatsApp) accept more than one valid domain -- see PLATFORM_DOMAINS
below. Mastodon is intentionally checked only for the substring
"mastodon" rather than a fixed domain, since it's federated and
self-hosted instances use arbitrary domains.
"""

from typing import Optional
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
import uuid


# ====================================================================
# MANAGER
# ====================================================================

class SocialInfoManager(models.Manager):

    def for_user(self, user):
        """Get the SocialInfo row for a user, or None if none exists yet."""
        return self.filter(user=user).first()

    def get_or_create_for_user(self, user, **defaults):
        """
        Lazily create a SocialInfo for `user` on first use -- the
        intended entry point for views, mirroring
        LocationInfo.objects.get_or_create_for_user() and
        ProfileInfo.objects.get_or_create(user=...).
        """
        obj, created = self.get_or_create(user=user, defaults=defaults)
        return obj, created

    def with_any_link(self):
        """Rows that have at least one social link set -- useful for 'connected accounts' style features."""
        link_fields = [f.name for f in SocialInfo._meta.get_fields() if isinstance(f, models.URLField)]
        q = models.Q()
        for field_name in link_fields:
            q |= models.Q(**{f"{field_name}__isnull": False})
        return self.filter(q)


# ====================================================================
# MODEL
# ====================================================================

class SocialInfo(models.Model):
    """
    A single set of social media links for a user. One-to-one with
    User for now (mirrors LocationInfo's and ProfileInfo's shape) --
    see the "ON THE SHEER NUMBER OF FIELDS" note above for when to
    convert this to a per-platform row model instead.
    """

    # (platform field name -> required domain substring, or tuple of
    # acceptable domain substrings) -- used by clean() below to
    # sanity-check each URL against its expected platform.
    PLATFORM_DOMAINS = {
        # -- Mainstream social --
        "facebook_url": "facebook.com",
        "instagram_url": "instagram.com",
        "twitter_url": ("twitter.com", "x.com"),
        "threads_url": "threads.net",
        "snapchat_url": "snapchat.com",
        "pinterest_url": "pinterest.com",
        "tiktok_url": "tiktok.com",
        "bluesky_url": "bsky.app",
        "mastodon_url": "mastodon",  # self-hosted instances vary widely
        "reddit_url": "reddit.com",
        "tumblr_url": "tumblr.com",
        "vk_url": "vk.com",
        "weibo_url": "weibo.com",
        "line_url": "line.me",
        "wechat_url": "wechat.com",

        # -- Professional --
        "linkedin_url": "linkedin.com",
        "github_url": "github.com",
        "gitlab_url": "gitlab.com",
        "behance_url": "behance.net",
        "dribbble_url": "dribbble.com",

        # -- Video / streaming / audio --
        "youtube_url": "youtube.com",
        "twitch_url": "twitch.tv",
        "vimeo_url": "vimeo.com",
        "spotify_url": "spotify.com",
        "soundcloud_url": "soundcloud.com",
        "kick_url": "kick.com",

        # -- Writing / community --
        "medium_url": "medium.com",
        "substack_url": "substack.com",
        "quora_url": "quora.com",
        "discord_url": "discord.com",
        "telegram_url": "t.me",
        "whatsapp_url": ("wa.me", "whatsapp.com"),

        # -- Interests / niche communities --
        "goodreads_url": "goodreads.com",
        "letterboxd_url": "letterboxd.com",
        "strava_url": "strava.com",
    }

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="social_info",
        help_text=_("User these social links belong to"),
    )

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier for external API references"),
    )

    # ---------------------------------------------------------------
    # Mainstream social
    # ---------------------------------------------------------------

    facebook_url = models.URLField(_("Facebook URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Facebook profile or page"))
    instagram_url = models.URLField(_("Instagram URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Instagram profile"))
    twitter_url = models.URLField(_("X / Twitter URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to X (Twitter) profile"))
    threads_url = models.URLField(_("Threads URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Threads profile"))
    snapchat_url = models.URLField(_("Snapchat URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Snapchat profile"))
    pinterest_url = models.URLField(_("Pinterest URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Pinterest profile"))
    tiktok_url = models.URLField(_("TikTok URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to TikTok profile"))
    bluesky_url = models.URLField(_("Bluesky URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Bluesky profile"))
    mastodon_url = models.URLField(_("Mastodon URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Mastodon profile (any instance)"))
    reddit_url = models.URLField(_("Reddit URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Reddit profile"))
    tumblr_url = models.URLField(_("Tumblr URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Tumblr blog"))
    vk_url = models.URLField(_("VK URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to VK profile"))
    weibo_url = models.URLField(_("Weibo URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Weibo profile"))
    line_url = models.URLField(_("LINE URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to LINE profile"))
    wechat_url = models.URLField(_("WeChat URL"), max_length=500, blank=True, null=True, help_text=_("Full URL or QR link to WeChat profile"))

    # ---------------------------------------------------------------
    # Professional
    # ---------------------------------------------------------------

    linkedin_url = models.URLField(_("LinkedIn URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to LinkedIn profile"))
    github_url = models.URLField(_("GitHub URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to GitHub profile"))
    gitlab_url = models.URLField(_("GitLab URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to GitLab profile"))
    behance_url = models.URLField(_("Behance URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Behance portfolio"))
    dribbble_url = models.URLField(_("Dribbble URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Dribbble portfolio"))

    # ---------------------------------------------------------------
    # Video / streaming / audio
    # ---------------------------------------------------------------

    youtube_url = models.URLField(_("YouTube URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to YouTube channel"))
    twitch_url = models.URLField(_("Twitch URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Twitch channel"))
    vimeo_url = models.URLField(_("Vimeo URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Vimeo profile"))
    spotify_url = models.URLField(_("Spotify URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Spotify artist or profile page"))
    soundcloud_url = models.URLField(_("SoundCloud URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to SoundCloud profile"))
    kick_url = models.URLField(_("Kick URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Kick channel"))

    # ---------------------------------------------------------------
    # Writing / community
    # ---------------------------------------------------------------

    medium_url = models.URLField(_("Medium URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Medium profile"))
    substack_url = models.URLField(_("Substack URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Substack newsletter"))
    quora_url = models.URLField(_("Quora URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Quora profile"))
    discord_url = models.URLField(_("Discord URL"), max_length=500, blank=True, null=True, help_text=_("Invite or profile link for Discord"))
    telegram_url = models.URLField(_("Telegram URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Telegram profile or channel"))
    whatsapp_url = models.URLField(_("WhatsApp URL"), max_length=500, blank=True, null=True, help_text=_("wa.me link or WhatsApp business profile"))

    # ---------------------------------------------------------------
    # Interests / niche communities
    # ---------------------------------------------------------------

    goodreads_url = models.URLField(_("Goodreads URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Goodreads profile"))
    letterboxd_url = models.URLField(_("Letterboxd URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Letterboxd profile"))
    strava_url = models.URLField(_("Strava URL"), max_length=500, blank=True, null=True, help_text=_("Full URL to Strava profile"))

    # Deliberately not domain-checked in clean() like the platform
    # fields above -- a personal website is, by definition, not tied
    # to a single expected domain.
    website_url = models.URLField(
        _("Website URL"),
        max_length=500,
        blank=True,
        null=True,
        help_text=_("Personal or business website"),
    )

    created_at = models.DateTimeField(_("Created At"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated At"), auto_now=True)

    objects = SocialInfoManager()

    class Meta:
        verbose_name = _("Social Info")
        verbose_name_plural = _("Social Infos")
        db_table = "social_info"
        indexes = [
            models.Index(fields=["uuid"]),
        ]

    def __str__(self) -> str:
        return f"Social links for {self.user}"

    def __repr__(self) -> str:
        return f"<SocialInfo: {self.user} ({self.link_count} link(s) set)>"

    # ================================================================
    # PROPERTIES
    # ================================================================

    @property
    def all_links(self) -> dict:
        """{platform_name: url} for every platform that has a URL set, skipping empties."""
        fields = list(self.PLATFORM_DOMAINS.keys()) + ["website_url"]
        return {
            name: getattr(self, name)
            for name in fields
            if getattr(self, name)
        }

    @property
    def link_count(self) -> int:
        return len(self.all_links)

    @property
    def has_any_link(self) -> bool:
        return self.link_count > 0

    # ================================================================
    # DATA EXPORT (GDPR)
    # ================================================================

    def export_data(self) -> dict:
        """Export social link data for GDPR compliance -- same shape/spirit as User.export_data(), ProfileInfo.export_data(), and LocationInfo.export_data()."""
        data = {"uuid": str(self.uuid)}
        for field_name in list(self.PLATFORM_DOMAINS.keys()) + ["website_url"]:
            data[field_name] = getattr(self, field_name)
        data["created_at"] = self.created_at.isoformat()
        data["updated_at"] = self.updated_at.isoformat()
        return data

    # ================================================================
    # VALIDATION
    # ================================================================

    def clean(self) -> None:
        super().clean()

        errors = {}

        for field_name, expected_domains in self.PLATFORM_DOMAINS.items():
            url = getattr(self, field_name)
            if not url:
                continue

            if isinstance(expected_domains, str):
                expected_domains = (expected_domains,)

            hostname = (urlparse(url).hostname or "").lower()
            if not any(domain in hostname for domain in expected_domains):
                field_label = self._meta.get_field(field_name).verbose_name
                errors[field_name] = _(
                    "%(field)s must be a link to %(domains)s"
                ) % {
                    "field": field_label,
                    "domains": " or ".join(expected_domains),
                }

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)