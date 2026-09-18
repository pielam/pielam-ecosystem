# apps/customer/admin/contact_info_admin.py

"""
Admin registration for ContactInfo and ContactVerification.

Every field on both models is editable from the admin, grouped into
fieldsets that mirror the field groupings already used in
contact_info.py (email / phone / messaging apps / preferences /
archive / metadata) so the admin form reads the same way the model
source does.

Two fields are intentionally NOT admin-editable, because Django itself
won't allow it -- not a choice made here:
  - `uuid`            (editable=False on the field)
  - `contact_creation_time` / `contact_updated_time` (auto_now_add /
    auto_now imply editable=False)
These are shown read-only instead of being silently dropped, so an
admin can still see them.

`user` (the OneToOne primary key) is editable when *creating* a new
ContactInfo row, but locked read-only once the row exists -- changing
the PK of an existing OneToOne-on-PK row would either break the
relation or collide with another user's row, so that's blocked at the
admin layer rather than left as a footgun.

Soft delete: ContactInfo.delete() (single object) and
ContactInfoQuerySet.delete() (bulk, which the admin's default "delete
selected" action uses) are already overridden on the model to archive
instead of hard-deleting -- see contact_info.py. So the built-in admin
delete action already does the safe thing; `hard_delete_selected` is
added here as the explicit escape hatch matching
`ContactInfoQuerySet.hard_delete()`.

CHANNEL VALIDATION -- ContactVerification.channel must match one of
ContactInfo.CHANNEL_FIELD_NAMES (enforced by ContactVerification.
clean(), which runs on every save() since that model's save() now
calls full_clean()). Both the inline and the standalone
ContactVerification admin use ChannelChoiceForm so that constraint is
a dropdown in the UI, not just a save-time error.

PHONE-BASED CHANNEL CHECKBOXES -- WhatsApp, Signal, Viber, and imo are
the four messaging channels (besides phone_number itself, which
already has a real is_phone_verified field) that use a phone number as
their identifier. ContactInfoAdminForm adds one virtual checkbox per
channel, backed by ContactVerification via
ContactInfo.verify_channel()/unverify_channel(), so an admin can
verify/unverify these without leaving the ContactInfo change form or
touching the ContactVerification inline. The remaining 11 (non-phone)
channels are shown read-only via `verification_summary`.

VERIFYING FROM ContactVerificationAdmin -- `mark_verified` routes
through ContactInfo.verify_channel() rather than setting
status/verified_value by hand, so the same "snapshot the current field
value, reject if the field is empty" logic applies whether a channel
is verified from code, from the ContactInfo checkboxes, or from this
bulk action.
"""

from django import forms
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.customer.models.contact_info import ContactInfo, ContactVerification


# ====================================================================
# SHARED FORM — restricts `channel` to ContactInfo's registered
# messaging-channel field names, used by both the inline and the
# standalone ContactVerification admin so the constraint only has to
# be defined once.
# ====================================================================

class ChannelChoiceForm(forms.ModelForm):
    channel = forms.ChoiceField(
        choices=[(name, name) for name in sorted(ContactInfo.CHANNEL_FIELD_NAMES)],
        help_text=_(
            "Must match a ContactInfo messaging-channel field name "
            "(e.g. 'whatsapp_number', not 'whatsapp')."
        ),
    )

    class Meta:
        model = ContactVerification
        fields = "__all__"


# ====================================================================
# CONTACT VERIFICATION — inline (quick view/edit from the ContactInfo
# page) and a standalone admin (full-field editing, incl. otp_code_hash
# for support/debugging of a stuck verification).
# ====================================================================

class StaleFilter(admin.SimpleListFilter):
    """
    Rows where status=VERIFIED but the live ContactInfo field no
    longer matches verified_value. is_stale() isn't a DB-level
    property (it reads a live attribute off the related ContactInfo),
    so this filter evaluates it in Python over the VERIFIED subset
    rather than expressing it as a queryset filter.
    """

    title = _("Staleness")
    parameter_name = "stale"

    def lookups(self, request, model_admin):
        return (
            ("stale", _("Stale (verified but value changed)")),
            ("fresh", _("Fresh / not applicable")),
        )

    def queryset(self, request, queryset):
        if self.value() not in ("stale", "fresh"):
            return queryset

        verified = queryset.filter(
            status=ContactVerification.Status.VERIFIED
        ).select_related("contact")
        stale_ids = [v.pk for v in verified if v.is_stale()]

        if self.value() == "stale":
            return queryset.filter(pk__in=stale_ids)
        # "fresh" = everything not in the stale set, including
        # non-VERIFIED rows (staleness only applies to VERIFIED ones).
        return queryset.exclude(pk__in=stale_ids)


class ContactVerificationInline(admin.TabularInline):
    model = ContactVerification
    form = ChannelChoiceForm
    extra = 0
    fields = (
        "channel", "status", "method",
        "verified_value", "verified_at", "stale_indicator",
        "external_id", "attempts",
    )
    readonly_fields = ("stale_indicator",)
    show_change_link = True

    @admin.display(description=_("Stale"), boolean=True)
    def stale_indicator(self, obj):
        # obj is None for the empty "add another" extra row.
        return obj.is_stale() if obj and obj.pk else False


@admin.register(ContactVerification)
class ContactVerificationAdmin(admin.ModelAdmin):
    form = ChannelChoiceForm

    list_display = (
        "id", "contact", "channel", "status", "method",
        "verified_value", "verified_at", "stale_badge", "attempts", "updated_at",
    )
    list_filter = ("status", "method", "channel", StaleFilter)
    search_fields = (
        "contact__user__email", "contact__user__phone",
        "channel", "verified_value", "external_id",
    )
    autocomplete_fields = ("contact",)
    readonly_fields = ("created_at", "updated_at", "stale_badge")

    fieldsets = (
        (_("Verification"), {
            "fields": ("contact", "channel", "status", "method"),
        }),
        (_("Verified Value"), {
            "fields": ("verified_value", "verified_at", "stale_badge", "external_id"),
        }),
        (_("OTP State"), {
            "fields": ("otp_code_hash", "otp_expires_at", "attempts"),
            "description": _(
                "otp_code_hash is a hash, not a plaintext code -- editing "
                "it here is for support/debug purposes only (e.g. forcing "
                "a stuck OTP flow to reset)."
            ),
        }),
        (_("Metadata"), {
            "fields": ("created_at", "updated_at"),
        }),
    )

    @admin.display(description=_("Stale"), boolean=True)
    def stale_badge(self, obj):
        """
        True when status=VERIFIED but the live ContactInfo field no
        longer matches verified_value -- exactly the rows that
        ContactInfo.get_visible_contact_methods() will silently treat
        as unverified on the frontend. Surfacing this here means staff
        can spot it directly instead of only hearing about it via a
        "why does my verified badge say unverified" support ticket.
        """
        return obj.is_stale()

    actions = ["mark_verified", "mark_unverified", "reset_otp_state"]

    @admin.action(description=_("Mark selected as Verified (manual review)"))
    def mark_verified(self, request, queryset):
        """
        Routes through ContactInfo.verify_channel() instead of setting
        status/verified_value directly, so approving from here goes
        through the exact same "snapshot the current field value,
        refuse if it's empty" path as verifying from code or from the
        ContactInfo phone-channel checkboxes.
        """
        verified_count = 0
        skipped_count = 0
        for verification in queryset.select_related("contact"):
            try:
                verification.contact.verify_channel(
                    verification.channel,
                    method=ContactVerification.Method.MANUAL,
                )
                verified_count += 1
            except ValueError:
                # Channel currently has no value on ContactInfo --
                # nothing to verify against.
                skipped_count += 1

        message = f"{verified_count} verification(s) marked as verified."
        if skipped_count:
            message += f" Skipped {skipped_count} with no current value on ContactInfo."
        self.message_user(request, message, messages.SUCCESS if not skipped_count else messages.WARNING)

    @admin.action(description=_("Mark selected as Unverified"))
    def mark_unverified(self, request, queryset):
        updated = queryset.update(status=ContactVerification.Status.UNVERIFIED, verified_at=None)
        self.message_user(request, f"{updated} verification(s) marked as unverified.", messages.SUCCESS)

    @admin.action(description=_("Reset OTP state (clear code/expiry/attempts)"))
    def reset_otp_state(self, request, queryset):
        updated = queryset.update(otp_code_hash="", otp_expires_at=None, attempts=0)
        self.message_user(request, f"OTP state cleared for {updated} verification(s).", messages.SUCCESS)


# ====================================================================
# CONTACT INFO
# ====================================================================

PHONE_BASED_CHANNELS = ("whatsapp", "signal", "viber", "imo")


class ContactInfoAdminForm(forms.ModelForm):
    """
    Adds one checkbox per phone-based messaging channel (WhatsApp,
    Signal, Viber, imo). phone_number itself already has a real
    is_phone_verified model field/checkbox, so it isn't duplicated
    here.

    These four checkboxes are NOT model fields -- they're virtual,
    backed by ContactVerification rows. save() reads the checked state
    and calls ContactInfo.verify_channel()/unverify_channel()
    accordingly, rather than writing anything directly, so this goes
    through the exact same "snapshot current value, refuse if empty"
    logic as verifying from code or from the ContactVerification admin
    action.
    """

    verify_whatsapp = forms.BooleanField(
        label=_("WhatsApp verified"), required=False,
        help_text=_("Check to verify, uncheck to unverify. No effect if the number field is empty."),
    )
    verify_signal = forms.BooleanField(
        label=_("Signal verified"), required=False,
        help_text=_("Check to verify, uncheck to unverify. No effect if the number field is empty."),
    )
    verify_viber = forms.BooleanField(
        label=_("Viber verified"), required=False,
        help_text=_("Check to verify, uncheck to unverify. No effect if the number field is empty."),
    )
    verify_imo = forms.BooleanField(
        label=_("imo verified"), required=False,
        help_text=_("Check to verify, uncheck to unverify. No effect if the number field is empty."),
    )

    class Meta:
        model = ContactInfo
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance")
        if instance is not None and instance.pk:
            for key in PHONE_BASED_CHANNELS:
                self.fields[f"verify_{key}"].initial = (
                    instance.get_verification_status(key) == "verified"
                )

    def save(self, commit=True):
        instance = super().save(commit=commit)
        # Apply checkbox changes after the base save, so any field
        # value the admin edited in the same submit (e.g. a new
        # whatsapp_number) is already persisted before we snapshot it
        # into verified_value.
        for key in PHONE_BASED_CHANNELS:
            checked = self.cleaned_data.get(f"verify_{key}", False)
            currently_verified = instance.get_verification_status(key) == "verified"
            if checked and not currently_verified:
                try:
                    instance.verify_channel(key, method=ContactVerification.Method.MANUAL)
                except ValueError:
                    # Field is empty -- nothing to verify. Silently a
                    # no-op rather than blocking the whole form save;
                    # the checkbox will just show unchecked again on
                    # reload since get_verification_status() will say
                    # 'unverified'.
                    pass
            elif not checked and currently_verified:
                instance.unverify_channel(key)
        return instance


@admin.register(ContactInfo)
class ContactInfoAdmin(admin.ModelAdmin):
    form = ContactInfoAdminForm
    inlines = [ContactVerificationInline]

    list_display = (
        "user",
        "phone_number", "is_phone_verified", "show_phone",
        "alt_email", "is_alt_email_verified", "show_email",
        "preferred_contact_method",
        "is_contact_public", "allow_contact_requests", "is_contact_archived",
        "contact_updated_time",
    )
    list_filter = (
        "is_contact_public",
        "is_contact_archived",
        "allow_contact_requests",
        "is_phone_verified",
        "is_alt_email_verified",
        "preferred_contact_method",
    )
    search_fields = (
        "user__email", "user__phone",
        "uuid",
        "phone_number", "alt_email",
        "whatsapp_number", "telegram_username", "signal_number",
        "discord_username", "instagram_username", "twitter_username",
        "linkedin_url", "slack_workspace_handle",
    )
    autocomplete_fields = ("user",)
    date_hierarchy = "contact_creation_time"
    save_on_top = True

    fieldsets = (
        (_("Account"), {
            "fields": ("user", "uuid"),
        }),
        (_("Email"), {
            "fields": (
                "alt_email", "is_alt_email_verified", "alt_email_verified_at",
                "show_email",
            ),
        }),
        (_("Phone-Based Channels"), {
            "description": _(
                "These 5 channels use a phone number. phone_number has its "
                "own verified flag; the other 4 are verified here via "
                "checkbox (backed by ContactVerification)."
            ),
            "fields": (
                "phone_number", "is_phone_verified", "phone_verified_at", "show_phone",
                ("whatsapp_number", "show_whatsapp", "verify_whatsapp"),
                ("signal_number", "show_signal", "verify_signal"),
                ("viber_number", "show_viber", "verify_viber"),
                ("imo_number", "show_imo", "verify_imo"),
            ),
        }),
        (_("Other Messaging Apps"), {
            "fields": (
                "verification_summary",
                ("telegram_username", "show_telegram"),
                ("wechat_id", "show_wechat"),
                ("line_id", "show_line"),
                ("skype_id", "show_skype"),
                ("discord_username", "show_discord"),
                ("facebook_messenger_username", "show_facebook_messenger"),
                ("instagram_username", "show_instagram"),
                ("snapchat_username", "show_snapchat"),
                ("twitter_username", "show_twitter"),
                ("linkedin_url", "show_linkedin"),
                ("slack_workspace_handle", "show_slack"),
            ),
        }),
        (_("Contact Preferences"), {
            "fields": (
                "preferred_contact_method",
                "allow_contact_requests",
                "is_contact_public",
            ),
        }),
        (_("Archive / Soft Delete"), {
            "fields": ("is_contact_archived", "archived_at"),
        }),
        (_("Metadata"), {
            "fields": ("contact_creation_time", "contact_updated_time"),
            "classes": ("collapse",),
        }),
    )

    @admin.display(description=_("Verification status (non-phone channels)"))
    def verification_summary(self, obj):
        """
        One-glance verified/stale/unverified state for every messaging
        channel (excluding the 4 phone-based ones, which have their
        own checkboxes above) that actually has a value, using
        ContactInfo.get_verification_status() -- so this always agrees
        with what get_visible_contact_methods() would show a viewer,
        without needing to open the ContactVerification inline rows
        one by one. Channels with no value set are omitted rather than
        listed as "unverified" clutter.
        """
        if obj is None or obj.pk is None:
            return _("Save first to see verification status.")

        rows = []
        for key, field_name in ContactInfo.MESSAGING_CHANNELS:
            if key in PHONE_BASED_CHANNELS:
                continue  # already has its own checkbox in Phone-Based Channels
            if not getattr(obj, field_name):
                continue
            status = obj.get_verification_status(key)
            color = {"verified": "green", "stale": "#b8860b", "unverified": "#888"}[status]
            rows.append(
                f'<span style="color:{color};font-weight:600;">{key}: {status}</span>'
            )

        if not rows:
            return _("No other messaging channels set.")
        return format_html("<br>".join(rows))

    def get_readonly_fields(self, request, obj=None):
        """
        `uuid` and the auto_now/auto_now_add timestamps are always
        read-only -- Django enforces this at the field level
        (editable=False), listing them here just makes them visible
        in the form instead of silently vanishing.

        `verification_summary` is a computed display, never a real
        field, so it's always read-only regardless of obj.

        `user` is only locked once the row already exists: it's the
        OneToOne primary key, so repointing it on an existing row
        would either orphan the old user's contact info or collide
        with another user's row. On the *add* form (obj is None) it
        stays editable so a new ContactInfo can actually be created.
        """
        readonly = ["uuid", "contact_creation_time", "contact_updated_time", "verification_summary"]
        if obj is not None:
            readonly.append("user")
        return readonly

    # ----------------------------------------------------------------
    # Actions
    # ----------------------------------------------------------------

    actions = [
        "verify_phone_action", "unverify_phone_action",
        "verify_email_action", "unverify_email_action",
        "archive_selected", "restore_selected",
        "make_public", "make_private",
        "hard_delete_selected",
    ]

    @admin.action(description=_("Verify phone number"))
    def verify_phone_action(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.verify_phone()
            count += 1
        self.message_user(request, f"Phone verified for {count} record(s).", messages.SUCCESS)

    @admin.action(description=_("Unverify phone number"))
    def unverify_phone_action(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.unverify_phone()
            count += 1
        self.message_user(request, f"Phone unverified for {count} record(s).", messages.SUCCESS)

    @admin.action(description=_("Verify alternate email"))
    def verify_email_action(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.verify_alt_email()
            count += 1
        self.message_user(request, f"Alternate email verified for {count} record(s).", messages.SUCCESS)

    @admin.action(description=_("Unverify alternate email"))
    def unverify_email_action(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.unverify_alt_email()
            count += 1
        self.message_user(request, f"Alternate email unverified for {count} record(s).", messages.SUCCESS)

    @admin.action(description=_("Archive selected (soft delete)"))
    def archive_selected(self, request, queryset):
        # Route through the queryset's own overridden .delete() so this
        # stays identical to what ContactInfoQuerySet.delete() already
        # does elsewhere in the app, rather than re-implementing it here.
        count, _detail = queryset.delete()
        self.message_user(request, f"Archived {count} record(s).", messages.SUCCESS)

    @admin.action(description=_("Restore archived selected"))
    def restore_selected(self, request, queryset):
        count = 0
        for obj in queryset:
            obj.restore()
            count += 1
        self.message_user(request, f"Restored {count} record(s).", messages.SUCCESS)

    @admin.action(description=_("Make contact info public"))
    def make_public(self, request, queryset):
        updated = queryset.update(is_contact_public=True)
        self.message_user(request, f"{updated} record(s) set to public.", messages.SUCCESS)

    @admin.action(description=_("Make contact info private"))
    def make_private(self, request, queryset):
        updated = queryset.update(is_contact_public=False)
        self.message_user(request, f"{updated} record(s) set to private.", messages.SUCCESS)

    @admin.action(description=_("Hard delete selected (irreversible, bypasses soft delete)"))
    def hard_delete_selected(self, request, queryset):
        """
        Explicit escape hatch matching ContactInfoQuerySet.hard_delete().
        The default admin "Delete selected" action already soft-deletes
        via the model's overridden .delete()/.delete() queryset method,
        so this is only for when an admin genuinely needs the row gone.
        """
        count = queryset.count()
        queryset.hard_delete()
        self.message_user(
            request,
            f"Permanently deleted {count} record(s). This cannot be undone.",
            messages.WARNING,
        )