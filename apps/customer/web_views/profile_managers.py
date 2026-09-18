"""
Standard Profile & Account Settings View
-----------------------------------------
(See original docstring for the full field-migration changelog — unchanged
below. This revision only adds AJAX support for the single-field "edit in a
popup" pattern used by the new profile_managers.html.)

AJAX SUPPORT (this pass)
-------------------------
The template now edits most text/select/date/file fields one at a time in a
modal, saved via fetch() with header `X-Requested-With: XMLHttpRequest`.
Detection: `_is_ajax(request)`.

For an AJAX POST, every branch below now returns JsonResponse instead of
redirecting:
    {"success": true,  "message": "...", "display_value": "..."}
    {"success": false, "message": "..."}

`display_value` is what the modal will show back in the row next to the
Edit button (already formatted the way the template would render it — e.g.
a choice field's human label, a date formatted as "Jan 2, 2000", etc.),
computed by `_display_value_for()`.

Non-AJAX POSTs (toggle forms, password change, MFA, danger zone) are
completely unchanged: same messages.success()/messages.error() + redirect
flow as before.

VERIFICATION UPGRADE (this pass)
---------------------------------
profile_managers.html renders a "Verify" button for every channel in
CHANNEL_VERIFICATION_METHODS -- including alt_email and phone_number, and
including the OAuth-backed channels (twitter_username, linkedin_url,
instagram_username, discord_username, facebook_messenger_username,
snapchat_username, wechat_id). All of these buttons go through the same
AJAX `start_verification` action; the template has no separate "connect"
link for OAuth channels. So:

  * alt_email / phone_number are now real OTP channels (email / SMS).
  * The OAuth branch of `start_verification` now actually kicks off the
    OAuth handshake (via `_build_oauth_authorize_url`) and returns an
    "open_link" step, the same shape already used for BOT channels,
    instead of telling the user to use a "connect button" that doesn't
    exist in this template.
  * Any failure to hand a code/link off to its transport now raises
    `VerificationDeliveryError` and is caught before anything is
    persisted, so a mis/un-configured provider produces a clean error
    message instead of a 500.

VERIFICATION FIX (this pass) -- alt_email / phone_number don't use
ContactVerification
---------------------------------------------------------------------
ContactInfo verifies alt_email/phone_number with its own
is_alt_email_verified/is_phone_verified + *_verified_at pair directly on
the model; every *other* channel is verified through the separate
ContactVerification model instead (see both models' docstrings).
ContactVerification.clean() enforces this: it rejects any `channel` that
isn't one of ContactInfo's 15 registered messaging-channel field names,
which does NOT include "alt_email" or "phone_number". The previous
revision of this view didn't know that and tried to create a
ContactVerification row for alt_email/phone_number like any other
channel -- that raised a ValidationError on every attempt, so verifying
either of those two fields was silently broken (surfaced to the user as
a generic "channel is not one of ContactInfo's messaging channel field
names" error).

Fixed by giving alt_email/phone_number their own code path
(DIRECT_VERIFIED_CHANNELS, `_start_direct_channel_verification` /
`_confirm_direct_channel_verification`) that never touches
ContactVerification:
  * The OTP itself (hash, expiry, attempts, the value it was sent to) is
    kept in the session rather than a DB row, since ContactInfo has no
    generic "pending code" field for these two channels the way
    ContactVerification does for the other 15.
  * On success it calls `contact_info.verify_alt_email()` /
    `contact_info.verify_phone()` directly -- the same methods
    ContactInfo already exposes for this -- rather than writing to a
    ContactVerification row that can't legally exist for these channels.

This also fixes signal_number's PHONE_MATCH auto-verify, which used to
check for a (structurally impossible) VERIFIED ContactVerification row
with channel="phone_number". It now checks
`contact_info.is_phone_verified` directly, which is the model's actual
source of truth for phone verification.

Because ContactInfo.save() only auto-invalidates the 15
ContactVerification-backed messaging channels when their value changes
(`_invalidate_changed_channel_verifications`), editing alt_email/
phone_number no longer auto-clears their verified flag the way the other
channels do. `_invalidate_stale_direct_verifications` below does that by
hand from the `update_contact` action, mirroring what the model does for
the other 15 fields.
"""

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import date as date_filter, truncatewords

from apps.customer.models.business_info import BusinessInfo
from apps.customer.models.contact_info import ContactInfo, ContactVerification
from apps.customer.models.location_info import LocationInfo
from apps.customer.models.profile_info import ProfileInfo
from apps.customer.models.profile_view_log import ProfileViewLog
from apps.customer.models.social_info import SocialInfo
from apps.customer.models.user_info import get_user_info

from apps.ponno.models.brand import Brand
from apps.ponno.models.category import Category
from apps.ponno.models.product import Product

User = get_user_model()


import requests
from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from urllib.parse import urlencode
# ----------------------------------------------------------------------
# Field whitelists — only these are ever mass-assigned from POST data.
# Keeping this explicit avoids accidentally exposing fields like
# `is_profile_verified`, `metadata`, or FK fields to user-controlled input.
# ----------------------------------------------------------------------

PROFILE_TEXT_FIELDS = [
    "profile_name", "profile_bio", "profile_gender", "profile_dob",
    "profile_language",
]

LOCATION_FIELDS = [
    "country", "country_code", "state", "city",
    "address", "present_address", "permanent_address", "postal_code",
    "latitude", "longitude",
]

from apps.customer.models.contact_info import ContactInfo, ContactVerification

CHANNEL_VERIFICATION_METHODS = {
    "alt_email": ContactVerification.Method.OTP,
    "phone_number": ContactVerification.Method.OTP,
    "whatsapp_number": ContactVerification.Method.OTP,
    "viber_number": ContactVerification.Method.OTP,
    "telegram_username": ContactVerification.Method.BOT,
    "line_id": ContactVerification.Method.BOT,
    "twitter_username": ContactVerification.Method.OAUTH,
    "linkedin_url": ContactVerification.Method.OAUTH,
    "instagram_username": ContactVerification.Method.OAUTH,
    "discord_username": ContactVerification.Method.OAUTH,
    "facebook_messenger_username": ContactVerification.Method.OAUTH,
    "snapchat_username": ContactVerification.Method.OAUTH,
    "wechat_id": ContactVerification.Method.OAUTH,
    "signal_number": ContactVerification.Method.PHONE_MATCH,
    "skype_id": ContactVerification.Method.MANUAL,
    "imo_number": ContactVerification.Method.MANUAL,
    "slack_workspace_handle": ContactVerification.Method.MANUAL,
}

# alt_email / phone_number are verified directly on ContactInfo
# (is_alt_email_verified / is_phone_verified), never via a
# ContactVerification row -- ContactVerification.clean() rejects those
# two channel names outright. Everything else in
# CHANNEL_VERIFICATION_METHODS is one of ContactInfo.CHANNEL_FIELD_NAMES
# and goes through ContactVerification as before.
DIRECT_VERIFIED_CHANNELS = frozenset({"alt_email", "phone_number"})

OTP_CODE_LENGTH = 6
OTP_EXPIRY_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

import hashlib
import secrets
from datetime import timedelta

from django.utils import timezone


def _generate_otp_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(OTP_CODE_LENGTH))


def _hash_code(code: str) -> str:
    # Codes are short-lived and single-use, so a fast hash is fine here --
    # this isn't password storage, it's a 10-minute OTP.
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class VerificationDeliveryError(Exception):
    """
    Raised whenever a verification code, bot prompt, or OAuth link could
    not be handed off to its transport -- unsupported channel, missing
    provider config, or a provider-side error. Callers must catch this
    and surface `str(exc)` to the user instead of letting it propagate;
    it is always written to be a safe, user-facing message.
    """


def _send_otp_code(channel: str, target_value: str, code: str) -> None:
    """
    Delivers an OTP code over the channel's real transport. Never raises
    a raw provider/library exception -- always VerificationDeliveryError.
    """
    if channel == "alt_email":
        try:
            send_mail(
                subject="Your PIELAM verification code",
                message=(
                    f"Your verification code is {code}. "
                    f"It expires in {OTP_EXPIRY_MINUTES} minutes."
                ),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[target_value],
                fail_silently=False,
            )
        except Exception as exc:
            raise VerificationDeliveryError(
                "Couldn't send the verification email. Please try again shortly."
            ) from exc

    elif channel == "whatsapp_number":
        try:
            response = requests.post(
                f"https://graph.facebook.com/v19.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages",
                headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": target_value,
                    "type": "template",
                    "template": {
                        "name": "verification_code",  # must match an approved template
                        "language": {"code": "en_US"},
                        "components": [{
                            "type": "body",
                            "parameters": [{"type": "text", "text": code}],
                        }],
                    },
                },
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise VerificationDeliveryError(
                "Couldn't send the WhatsApp code. Please try again shortly."
            ) from exc

    else:
        # phone_number, viber_number: no SMS/Viber Business gateway is wired
        # up yet. Wire the real provider (Twilio, Vonage, Viber Business,
        # etc.) into a branch above -- this intentionally fails clean rather
        # than raising an unhandled NotImplementedError into the request.
        raise VerificationDeliveryError(
            f"Verification via {channel.replace('_', ' ')} isn't available yet. "
            "Please try another channel."
        )


def _send_bot_verification_prompt(channel: str, contact_info, token: str) -> str:
    """
    Returns a deep-link/URL the user should open to complete a
    bot-based verification (Telegram, LINE). The bot's webhook, on
    receiving the /start=<token> payload, is responsible for calling
    back into confirm_verification-equivalent logic server-to-server
    (this can't go through the browser POST flow the same way OTP
    confirmation does).

    Raises VerificationDeliveryError if the corresponding bot isn't
    configured yet, rather than letting an unhandled NotImplementedError
    reach the user.
    """
    if channel == "telegram_username":
        bot_username = getattr(settings, "TELEGRAM_BOT_USERNAME", None)
        if not bot_username:
            raise VerificationDeliveryError("Telegram verification isn't configured yet.")
        return f"https://t.me/{bot_username}?start={token}"

    if channel == "line_id":
        # TODO: LINE's Add Friend + postback flow needs a configured LINE
        # Official Account and a webhook endpoint; wire it up here.
        raise VerificationDeliveryError("LINE verification isn't configured yet.")

    raise VerificationDeliveryError(
        f"Bot verification isn't configured for {channel.replace('_', ' ')}."
    )


# Per-provider OAuth app config. Populate the referenced settings once each
# app is registered with its provider; channels without a client id configured
# fail cleanly via VerificationDeliveryError rather than silently misbehaving.
OAUTH_PROVIDER_CONFIG = {
    "twitter_username": {
        "authorize_url": "https://twitter.com/i/oauth2/authorize",
        "client_id_setting": "TWITTER_OAUTH_CLIENT_ID",
        "scope": "users.read",
    },
    "linkedin_url": {
        "authorize_url": "https://www.linkedin.com/oauth/v2/authorization",
        "client_id_setting": "LINKEDIN_OAUTH_CLIENT_ID",
        "scope": "r_liteprofile",
    },
    "instagram_username": {
        "authorize_url": "https://api.instagram.com/oauth/authorize",
        "client_id_setting": "INSTAGRAM_OAUTH_CLIENT_ID",
        "scope": "user_profile",
    },
    "discord_username": {
        "authorize_url": "https://discord.com/api/oauth2/authorize",
        "client_id_setting": "DISCORD_OAUTH_CLIENT_ID",
        "scope": "identify",
    },
    "facebook_messenger_username": {
        "authorize_url": "https://www.facebook.com/v19.0/dialog/oauth",
        "client_id_setting": "FACEBOOK_OAUTH_CLIENT_ID",
        "scope": "public_profile",
    },
    "snapchat_username": {
        "authorize_url": "https://accounts.snapchat.com/accounts/oauth2/auth",
        "client_id_setting": "SNAPCHAT_OAUTH_CLIENT_ID",
        "scope": "user.display_name",
    },
    "wechat_id": {
        "authorize_url": "https://open.weixin.qq.com/connect/qrconnect",
        "client_id_setting": "WECHAT_OAUTH_CLIENT_ID",
        "scope": "snsapi_login",
    },
}


def _build_oauth_authorize_url(request, channel: str, user, token: str) -> str:
    """
    Builds the provider "authorize" URL for an OAuth-backed contact
    channel. `token` binds this specific verification attempt; it's
    folded into a signed `state` value together with the user id and
    channel so `contact_oauth_callback` can confirm the callback belongs
    to this user, this channel, and hasn't expired or been reused.

    Raises VerificationDeliveryError if the channel has no OAuth config
    or no client id configured yet.
    """
    config = OAUTH_PROVIDER_CONFIG.get(channel)
    if config is None:
        raise VerificationDeliveryError(
            f"OAuth verification isn't configured for {channel.replace('_', ' ')}."
        )

    client_id = getattr(settings, config["client_id_setting"], None)
    if not client_id:
        raise VerificationDeliveryError(
            f"{channel.replace('_', ' ').title()} verification isn't configured yet."
        )

    state = signing.dumps({"user_id": user.pk, "channel": channel, "token": token})
    redirect_uri = request.build_absolute_uri(f"/user/contact/oauth/{channel}/callback/")

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "scope": config.get("scope", ""),
    }
    return f"{config['authorize_url']}?{urlencode(params)}"


CONTACT_FIELDS = [
    "alt_email", "phone_number",
    "whatsapp_number", "telegram_username", "signal_number",
    "viber_number", "wechat_id", "line_id", "imo_number", "skype_id",
    "discord_username", "facebook_messenger_username",
    "instagram_username", "snapchat_username", "twitter_username",
    "linkedin_url", "slack_workspace_handle",
    "preferred_contact_method",
]

CONTACT_PRIVACY_BOOL_FIELDS = [
    "show_email", "show_phone", "show_whatsapp", "show_telegram",
    "show_signal", "show_viber", "show_wechat", "show_line", "show_imo",
    "show_skype", "show_discord", "show_facebook_messenger",
    "show_instagram", "show_snapchat", "show_twitter", "show_linkedin",
    "show_slack", "allow_contact_requests", "is_contact_public",
]

BUSINESS_FIELDS = [
    "business_name", "legal_name", "business_type", "industry",
    "description", "founded_date", "registration_number", "tax_id",
    "business_email", "business_phone", "website",
    "employee_count", "annual_revenue", "currency",
]

SOCIAL_FIELDS = list(SocialInfo.PLATFORM_DOMAINS.keys()) + ["website_url"]

PRIVACY_BOOL_FIELDS = [
    "is_profile_public", "show_email", "show_phone", "show_dob",
    "show_age", "show_location", "show_followers", "show_following",
    "allow_messages", "allow_follow",
]

NOTIFICATION_BOOL_FIELDS = [
    "notify_on_follow", "notify_on_message",
    "email_notifications", "sms_notifications",
]

ACCOUNT_FIELDS = ["language", "country", "currency"]

# Fields whose stored value is a "choice" code and should be rendered back
# to the modal/row using the model's get_FOO_display() rather than the raw
# stored value (mirrors what the template does elsewhere with |get_FOO_display).
CHOICE_DISPLAY_FIELDS = {
    "profile_gender": "get_profile_gender_display",
    "preferred_contact_method": "get_preferred_contact_method_display",
    "business_type": "get_business_type_display",
}

# Fields that are dates and should be formatted the same way the template
# formats them (M j, Y) rather than shown as a raw ISO string.
DATE_FIELDS = {"profile_dob", "founded_date"}

# Fields that get truncated for display in the row (long text fields).
TRUNCATE_FIELDS = {"profile_bio": 24, "description": 24}


def _update_text_fields(instance, post_data, field_names):
    """Assign only the whitelisted fields that were actually submitted."""
    for field in field_names:
        if field in post_data:
            value = post_data.get(field, "").strip()
            setattr(instance, field, value or None)


def _invalidate_stale_contact_verifications(contact_info, submitted_fields):
    """
    Editing a field that's already been verified silently invalidates
    that verification -- otherwise a user could verify a number once
    and then swap in a different value while the UI still shows a
    green checkmark for it.

    Only checks channels that were actually part of this POST (not
    every VERIFIED row for this contact), so a routine save() doesn't
    pay for an unconditional extra query when nothing verification-
    relevant even changed.

    This only ever queries ContactVerification, which can only hold
    rows for ContactInfo.CHANNEL_FIELD_NAMES (the 15 messaging
    channels) -- alt_email/phone_number are filtered out here and
    handled separately by `_invalidate_stale_direct_verifications`,
    since a ContactVerification row for either of those two is
    rejected by ContactVerification.clean() and can never exist.

    Called AFTER _update_text_fields() has already applied the new
    values to `contact_info` in memory, so getattr() below reads the
    submitted value, not the old one.
    """
    messaging_fields = [f for f in submitted_fields if f in ContactInfo.CHANNEL_FIELD_NAMES]
    if not messaging_fields:
        return

    stale_qs = ContactVerification.objects.filter(
        contact=contact_info,
        channel__in=messaging_fields,
        status=ContactVerification.Status.VERIFIED,
    )

    for verification in stale_qs:
        current_value = getattr(contact_info, verification.channel, None) or ""
        if current_value != verification.verified_value:
            verification.status = ContactVerification.Status.UNVERIFIED
            verification.verified_at = None
            verification.save(update_fields=["status", "verified_at", "updated_at"])


def _invalidate_stale_direct_verifications(contact_info, old_values):
    """
    Counterpart to `_invalidate_stale_contact_verifications` for
    alt_email/phone_number -- these two are verified directly on
    ContactInfo (is_alt_email_verified / is_phone_verified) rather than
    through ContactVerification, so ContactInfo.save()'s own
    `_invalidate_changed_channel_verifications` (which only walks
    MESSAGING_CHANNELS) never touches them. Without this, editing an
    already-verified alt_email/phone_number would leave the verified
    flag on even though the value no longer matches what was verified.

    `old_values` is a {field_name: old_value} snapshot taken by the
    caller *before* `_update_text_fields()` overwrote the instance, for
    just the direct-verified fields that were actually submitted.
    """
    if "alt_email" in old_values:
        if contact_info.is_alt_email_verified and old_values["alt_email"] != contact_info.alt_email:
            contact_info.unverify_alt_email(save=False)

    if "phone_number" in old_values:
        if contact_info.is_phone_verified and old_values["phone_number"] != contact_info.phone_number:
            contact_info.unverify_phone(save=False)


def _pending_verification_session_key(channel: str) -> str:
    return f"pending_contact_verification:{channel}"


def _start_direct_channel_verification(request, channel: str, target_value: str) -> None:
    """
    Start-of-OTP-flow for alt_email/phone_number. These verify directly
    on ContactInfo (is_alt_email_verified/is_phone_verified) rather than
    via a ContactVerification row -- ContactVerification.clean() rejects
    channel="alt_email"/"phone_number" outright, so there's no DB row to
    hang pending-code state (hash/expiry/attempts) off of the way the
    other 15 channels do. That state is kept in the session instead,
    scoped to this channel and cleared on success, expiry, or lockout.

    Raises VerificationDeliveryError (propagated from _send_otp_code) if
    the code couldn't be sent -- caller is responsible for not writing
    anything to the session in that case.
    """
    code = _generate_otp_code()
    _send_otp_code(channel, target_value, code)
    request.session[_pending_verification_session_key(channel)] = {
        "code_hash": _hash_code(code),
        "target_value": target_value,
        "expires_at": (timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat(),
        "attempts": 0,
    }


def _confirm_direct_channel_verification(request, contact_info, channel: str, submitted_code: str):
    """
    Confirm side of `_start_direct_channel_verification`. Returns
    (ok: bool, message: str). On success, marks the channel verified via
    ContactInfo.verify_alt_email()/verify_phone() -- the model's own
    methods for this -- rather than writing a ContactVerification row.
    """
    key = _pending_verification_session_key(channel)
    pending = request.session.get(key)
    if not pending:
        return False, "No pending verification found for this channel."

    expires_at = timezone.datetime.fromisoformat(pending["expires_at"])
    if timezone.now() > expires_at:
        del request.session[key]
        return False, "This code has expired. Request a new one."

    if pending["attempts"] >= OTP_MAX_ATTEMPTS:
        del request.session[key]
        return False, "Too many incorrect attempts. Request a new code."

    if _hash_code(submitted_code) != pending["code_hash"]:
        pending["attempts"] += 1
        request.session[key] = pending
        return False, "Incorrect code. Please try again."

    current_value = (getattr(contact_info, channel, "") or "").strip()
    if current_value != pending["target_value"]:
        del request.session[key]
        return False, "This field changed since the code was sent. Request a new one."

    if channel == "alt_email":
        contact_info.verify_alt_email()
    else:
        contact_info.verify_phone()

    del request.session[key]
    return True, "Verified."


def _update_boolean_fields(instance, post_data, field_names):
    """Checkbox semantics: a field's absence in POST means False/unchecked."""
    for field in field_names:
        setattr(instance, field, field in post_data)


def _error_message(exc: ValidationError) -> str:
    if hasattr(exc, "message_dict"):
        return " ".join(f"{k}: {', '.join(v)}" for k, v in exc.message_dict.items())
    return "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


def _is_ajax(request) -> bool:
    """Field-edit modal marks its fetch() calls with this header."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _display_value_for(instance, field_name):
    """
    Render a single field's current value the way the template would show
    it in a field-view row, so the modal can update the row in place
    without a full page reload.
    """
    if instance is None:
        return None

    if field_name in CHOICE_DISPLAY_FIELDS:
        getter = getattr(instance, CHOICE_DISPLAY_FIELDS[field_name], None)
        value = getter() if callable(getter) else None
        return value or None

    value = getattr(instance, field_name, None)
    if value in (None, ""):
        return None

    if field_name in DATE_FIELDS:
        return date_filter(value, "M j, Y")

    if field_name in TRUNCATE_FIELDS:
        return truncatewords(str(value), TRUNCATE_FIELDS[field_name])

    return str(value)


def _json_ok(message, instance=None, field_name=None):
    return JsonResponse({
        "success": True,
        "message": message,
        "display_value": _display_value_for(instance, field_name) if field_name else None,
    })


def _json_error(message, status=400):
    return JsonResponse({"success": False, "message": message}, status=status)


@login_required(login_url="/customer/signin/")
def ProfileManagerView(request):
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)
    ajax = _is_ajax(request)

    # ------------------------------------------------------------------
    # POST — one of several settings sections being saved
    # ------------------------------------------------------------------
    if request.method == "POST":
        action = request.POST.get("action")

        try:
            with transaction.atomic():

                if action == "update_profile":
                    _update_text_fields(profile_info, request.POST, PROFILE_TEXT_FIELDS)
                    if request.FILES.get("profile_photo"):
                        profile_info.profile_photo = request.FILES["profile_photo"]
                    if request.FILES.get("profile_cover_photo"):
                        profile_info.profile_cover_photo = request.FILES["profile_cover_photo"]
                    if profile_info.profile_name:
                        profile_info.generate_slug()
                    profile_info.full_clean()
                    profile_info.save()
                    if ajax:
                        field_name = next((f for f in PROFILE_TEXT_FIELDS if f in request.POST), None)
                        return _json_ok("Profile updated successfully.", profile_info, field_name)
                    messages.success(request, "Profile updated successfully.")

                elif action == "update_location":
                    location_info, _ = LocationInfo.objects.get_or_create_for_user(user)
                    _update_text_fields(location_info, request.POST, LOCATION_FIELDS)
                    location_info.save()
                    if ajax:
                        field_name = next((f for f in LOCATION_FIELDS if f in request.POST), None)
                        return _json_ok("Location updated.", location_info, field_name)
                    messages.success(request, "Location updated.")

                elif action == "update_contact":
                    contact_info, _ = ContactInfo.objects.get_or_create(user=user)
                    submitted_fields = [f for f in CONTACT_FIELDS if f in request.POST]

                    # Snapshot alt_email/phone_number BEFORE overwriting,
                    # so _invalidate_stale_direct_verifications can tell
                    # whether they actually changed.
                    old_direct_values = {
                        f: getattr(contact_info, f, None)
                        for f in DIRECT_VERIFIED_CHANNELS
                        if f in submitted_fields
                    }

                    _update_text_fields(contact_info, request.POST, CONTACT_FIELDS)
                    _invalidate_stale_contact_verifications(contact_info, submitted_fields)
                    _invalidate_stale_direct_verifications(contact_info, old_direct_values)
                    contact_info.save()
                    if ajax:
                        field_name = submitted_fields[0] if submitted_fields else None
                        return _json_ok("Contact information updated.", contact_info, field_name)
                    messages.success(request, "Contact information updated.")

                elif action == "update_contact_privacy":
                    contact_info, _ = ContactInfo.objects.get_or_create(user=user)
                    _update_boolean_fields(contact_info, request.POST, CONTACT_PRIVACY_BOOL_FIELDS)
                    contact_info.save()
                    if ajax:
                        return _json_ok("Contact visibility updated.")
                    messages.success(request, "Contact visibility updated.")

                elif action == "start_verification":
                    channel = request.POST.get("channel", "")
                    method = CHANNEL_VERIFICATION_METHODS.get(channel)

                    if method is None:
                        if ajax:
                            return _json_error("This channel doesn't support verification.")
                        messages.error(request, "This channel doesn't support verification.")
                    else:
                        contact_info, _ = ContactInfo.objects.get_or_create(user=user)
                        target_value = (getattr(contact_info, channel, "") or "").strip()

                        if not target_value:
                            error_msg = "Set a value for this field before verifying it."
                            if ajax:
                                return _json_error(error_msg)
                            messages.error(request, error_msg)

                        elif channel in DIRECT_VERIFIED_CHANNELS:
                            # alt_email / phone_number: session-backed OTP,
                            # no ContactVerification row (see module docstring).
                            try:
                                _start_direct_channel_verification(request, channel, target_value)
                            except VerificationDeliveryError as exc:
                                if ajax:
                                    return _json_error(str(exc))
                                messages.error(request, str(exc))
                            else:
                                msg = f"A verification code was sent to your {channel.replace('_', ' ')}."
                                if ajax:
                                    return JsonResponse({
                                        "success": True,
                                        "message": msg,
                                        "next_step": "enter_code",
                                    })
                                messages.success(request, msg)

                        else:
                            verification, _ = ContactVerification.objects.get_or_create(
                                contact=contact_info, channel=channel,
                                defaults={"method": method},
                            )
                            verification.method = method

                            if method == ContactVerification.Method.OTP:
                                code = _generate_otp_code()
                                try:
                                    _send_otp_code(channel, target_value, code)
                                except VerificationDeliveryError as exc:
                                    if ajax:
                                        return _json_error(str(exc))
                                    messages.error(request, str(exc))
                                else:
                                    verification.otp_code_hash = _hash_code(code)
                                    verification.otp_expires_at = timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
                                    verification.attempts = 0
                                    verification.status = ContactVerification.Status.PENDING
                                    verification.save()
                                    if ajax:
                                        return JsonResponse({
                                            "success": True,
                                            "message": f"A verification code was sent to your {channel.replace('_', ' ')}.",
                                            "next_step": "enter_code",
                                        })
                                    messages.success(request, "Verification code sent.")

                            elif method == ContactVerification.Method.BOT:
                                token = secrets.token_urlsafe(24)
                                try:
                                    deep_link = _send_bot_verification_prompt(channel, contact_info, token)
                                except VerificationDeliveryError as exc:
                                    if ajax:
                                        return _json_error(str(exc))
                                    messages.error(request, str(exc))
                                else:
                                    verification.otp_code_hash = _hash_code(token)  # reused as a generic pending-token slot
                                    verification.otp_expires_at = timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
                                    verification.status = ContactVerification.Status.PENDING
                                    verification.save()
                                    if ajax:
                                        return JsonResponse({
                                            "success": True,
                                            "message": "Open the link to finish verifying.",
                                            "next_step": "open_link",
                                            "verification_url": deep_link,
                                        })
                                    messages.success(request, "Follow the link to finish verifying.")

                            elif method == ContactVerification.Method.PHONE_MATCH:
                                # signal_number auto-verifies if it matches
                                # this user's already-verified phone_number.
                                # Phone verification lives directly on
                                # ContactInfo (is_phone_verified) -- there is
                                # no ContactVerification row for
                                # channel="phone_number" to query, since that
                                # channel name is rejected by
                                # ContactVerification.clean().
                                phone_matches = target_value == (contact_info.phone_number or "")
                                if contact_info.is_phone_verified and phone_matches:
                                    verification.status = ContactVerification.Status.VERIFIED
                                    verification.verified_value = target_value
                                    verification.verified_at = timezone.now()
                                    verification.save()
                                    msg = "Verified automatically -- matches your verified phone number."
                                    if ajax:
                                        return _json_ok(msg)
                                    messages.success(request, msg)
                                else:
                                    verification.status = ContactVerification.Status.FAILED
                                    verification.save()
                                    msg = "This number doesn't match a verified phone on your account."
                                    if ajax:
                                        return _json_error(msg)
                                    messages.error(request, msg)

                            elif method == ContactVerification.Method.MANUAL:
                                verification.status = ContactVerification.Status.PENDING
                                verification.save()
                                msg = "Submitted for manual review. This can take 1-2 business days."
                                if ajax:
                                    return _json_ok(msg)
                                messages.success(request, msg)

                            elif method == ContactVerification.Method.OAUTH:
                                token = secrets.token_urlsafe(24)
                                try:
                                    authorize_url = _build_oauth_authorize_url(request, channel, user, token)
                                except VerificationDeliveryError as exc:
                                    if ajax:
                                        return _json_error(str(exc))
                                    messages.error(request, str(exc))
                                else:
                                    verification.otp_code_hash = _hash_code(token)  # reused as the state-binding token slot
                                    verification.otp_expires_at = timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
                                    verification.status = ContactVerification.Status.PENDING
                                    verification.save()
                                    msg = "Open the link to connect and verify this account."
                                    if ajax:
                                        return JsonResponse({
                                            "success": True,
                                            "message": msg,
                                            "next_step": "open_link",
                                            "verification_url": authorize_url,
                                        })
                                    messages.success(request, msg)

                elif action == "confirm_verification":
                    channel = request.POST.get("channel", "")
                    submitted_code = request.POST.get("code", "").strip()

                    if channel in DIRECT_VERIFIED_CHANNELS:
                        contact_info, _ = ContactInfo.objects.get_or_create(user=user)
                        ok, msg = _confirm_direct_channel_verification(request, contact_info, channel, submitted_code)
                        if ok:
                            if ajax:
                                return _json_ok(msg)
                            messages.success(request, msg)
                        else:
                            if ajax:
                                return _json_error(msg)
                            messages.error(request, msg)

                    else:
                        try:
                            verification = ContactVerification.objects.get(
                                contact__user=user, channel=channel,
                                status=ContactVerification.Status.PENDING,
                            )
                        except ContactVerification.DoesNotExist:
                            msg = "No pending verification found for this channel."
                            if ajax:
                                return _json_error(msg)
                            messages.error(request, msg)
                        else:
                            if verification.otp_expires_at and timezone.now() > verification.otp_expires_at:
                                verification.status = ContactVerification.Status.FAILED
                                verification.save(update_fields=["status", "updated_at"])
                                msg = "This code has expired. Request a new one."
                                if ajax:
                                    return _json_error(msg)
                                messages.error(request, msg)
                            elif verification.attempts >= OTP_MAX_ATTEMPTS:
                                verification.status = ContactVerification.Status.FAILED
                                verification.save(update_fields=["status", "updated_at"])
                                msg = "Too many incorrect attempts. Request a new code."
                                if ajax:
                                    return _json_error(msg)
                                messages.error(request, msg)
                            elif _hash_code(submitted_code) != verification.otp_code_hash:
                                verification.attempts += 1
                                verification.save(update_fields=["attempts", "updated_at"])
                                msg = "Incorrect code. Please try again."
                                if ajax:
                                    return _json_error(msg)
                                messages.error(request, msg)
                            else:
                                contact_info = verification.contact
                                verification.status = ContactVerification.Status.VERIFIED
                                verification.verified_value = getattr(contact_info, channel, "") or ""
                                verification.verified_at = timezone.now()
                                verification.otp_code_hash = ""
                                verification.otp_expires_at = None
                                verification.save()
                                msg = "Verified."
                                if ajax:
                                    return _json_ok(msg)
                                messages.success(request, msg)

                elif action == "update_business":
                    if not user.is_dealer:
                        if ajax:
                            return _json_error("Only dealer accounts can edit business info.", status=403)
                        messages.error(request, "Only dealer accounts can edit business info.")
                    else:
                        submitted_name = request.POST.get("business_name", "").strip()
                        existing = BusinessInfo.objects.for_user(user)
                        name_for_create = submitted_name or (existing.business_name if existing else "")
                        if not name_for_create:
                            if ajax:
                                return _json_error("Business name is required.")
                            messages.error(request, "Business name is required.")
                        else:
                            business_info, _ = BusinessInfo.objects.get_or_create_for_user(
                                user, business_name=name_for_create,
                            )
                            _update_text_fields(business_info, request.POST, BUSINESS_FIELDS)
                            if not business_info.currency:
                                business_info.currency = "USD"
                            business_info.save()
                            if ajax:
                                field_name = next((f for f in BUSINESS_FIELDS if f in request.POST), None)
                                return _json_ok("Business information updated.", business_info, field_name)
                            messages.success(request, "Business information updated.")

                elif action == "update_social":
                    social_info, _ = SocialInfo.objects.get_or_create_for_user(user)
                    _update_text_fields(social_info, request.POST, SOCIAL_FIELDS)
                    social_info.save()
                    if ajax:
                        field_name = next((f for f in SOCIAL_FIELDS if f in request.POST), None)
                        return _json_ok("Social links updated.", social_info, field_name)
                    messages.success(request, "Social links updated.")

                elif action == "update_privacy":
                    _update_boolean_fields(profile_info, request.POST, PRIVACY_BOOL_FIELDS)
                    profile_info.save()
                    if ajax:
                        return _json_ok("Privacy settings updated.")
                    messages.success(request, "Privacy settings updated.")

                elif action == "update_notifications":
                    _update_boolean_fields(profile_info, request.POST, NOTIFICATION_BOOL_FIELDS)
                    profile_info.save()
                    if ajax:
                        return _json_ok("Notification preferences updated.")
                    messages.success(request, "Notification preferences updated.")

                elif action == "update_account":
                    for field in ACCOUNT_FIELDS:
                        if field in request.POST:
                            setattr(user, field, request.POST.get(field))
                    user.full_clean()
                    user.save()
                    if ajax:
                        field_name = next((f for f in ACCOUNT_FIELDS if f in request.POST), None)
                        return _json_ok("Account settings updated.", user, field_name)
                    messages.success(request, "Account settings updated.")

                elif action == "update_marketing_consent":
                    if "marketing_consent" in request.POST:
                        user.give_marketing_consent()
                    else:
                        user.revoke_marketing_consent()
                    if ajax:
                        return _json_ok("Marketing preferences updated.")
                    messages.success(request, "Marketing preferences updated.")

                elif action == "change_password":
                    current_password = request.POST.get("current_password", "")
                    new_password = request.POST.get("new_password", "")
                    confirm_password = request.POST.get("confirm_password", "")

                    if not user.check_password(current_password):
                        if ajax:
                            return _json_error("Current password is incorrect.")
                        messages.error(request, "Current password is incorrect.")
                    elif len(new_password) < 8:
                        if ajax:
                            return _json_error("New password must be at least 8 characters.")
                        messages.error(request, "New password must be at least 8 characters.")
                    elif new_password != confirm_password:
                        if ajax:
                            return _json_error("New passwords do not match.")
                        messages.error(request, "New passwords do not match.")
                    else:
                        user.set_password(new_password)
                        user.save()
                        update_session_auth_hash(request, user)  # keep the user logged in
                        if ajax:
                            return _json_ok("Password changed successfully.")
                        messages.success(request, "Password changed successfully.")

                elif action == "enable_mfa":
                    method = request.POST.get("mfa_method", "totp")
                    user.enable_mfa(method=method)
                    if ajax:
                        return _json_ok("Two-factor authentication enabled.")
                    messages.success(request, "Two-factor authentication enabled.")

                elif action == "disable_mfa":
                    user.disable_mfa()
                    if ajax:
                        return _json_ok("Two-factor authentication disabled.")
                    messages.success(request, "Two-factor authentication disabled.")

                elif action == "deactivate_account":
                    user.deactivate_account()
                    if ajax:
                        return _json_ok("Your account has been deactivated.")
                    messages.success(request, "Your account has been deactivated.")
                    return redirect("/customer/signin/")

                elif action == "delete_account":
                    confirm = request.POST.get("confirm_delete", "")
                    if confirm == user.email_or_phone:
                        user.soft_delete()
                        if ajax:
                            return _json_ok("Your account has been deleted.")
                        messages.success(request, "Your account has been deleted.")
                        return redirect("/customer/signin/")
                    if ajax:
                        return _json_error("Confirmation text did not match. Account not deleted.")
                    messages.error(request, "Confirmation text did not match. Account not deleted.")

                else:
                    if ajax:
                        return _json_error("Unknown settings action.")
                    messages.error(request, "Unknown settings action.")

        except ValidationError as exc:
            error_text = _error_message(exc)
            if ajax:
                return _json_error(error_text)
            messages.error(request, error_text)

        if ajax:
            # Any branch above that didn't already return (e.g. an
            # early "else" without an explicit return) falls through here.
            return _json_error("Nothing was saved.")
        return redirect(request.path)

    # ------------------------------------------------------------------
    # GET — render the settings page (unchanged)
    # ------------------------------------------------------------------
    info = get_user_info(user, create_missing=True)
    contact_info = info.contact

    # ContactVerification only ever holds rows for the 15 messaging
    # channels (ContactInfo.CHANNEL_FIELD_NAMES) -- alt_email/phone_number
    # are verified directly on ContactInfo instead, so their status is
    # added here from is_alt_email_verified/is_phone_verified rather than
    # from a (nonexistent) ContactVerification row.
    if contact_info:
        contact_verifications = {
            v.channel: v.status
            for v in ContactVerification.objects.filter(contact=contact_info)
        }
        contact_verifications["alt_email"] = (
            ContactVerification.Status.VERIFIED if contact_info.is_alt_email_verified
            else ContactVerification.Status.UNVERIFIED
        )
        contact_verifications["phone_number"] = (
            ContactVerification.Status.VERIFIED if contact_info.is_phone_verified
            else ContactVerification.Status.UNVERIFIED
        )
    else:
        contact_verifications = {}

    location_info = info.location
    social_info = info.social
    business_info = BusinessInfo.objects.for_user(user)

    followings_count = profile_info.following.count()
    followers_count = profile_info.followers.count()

    if user.is_dealer:
        products_listed_count = Product.objects.filter(dealer=user).count()
        active_products_count = Product.objects.filter(dealer=user, is_active=True).count()
    else:
        products_listed_count = active_products_count = 0

    brands_count = Brand.objects.count()
    categories_count = Category.objects.count()
    brands = Brand.objects.all()
    categories = Category.objects.all()

    recent_viewers = (
        ProfileViewLog.objects
        .filter(profile_user=user, viewer__isnull=False)
        .exclude(viewer=user)
        .select_related("viewer")
        .order_by("-viewed_at")[:20]
    )

    context = {
        "user": user,
        "profile_info": profile_info,
        "contact_info": contact_info,
        "contact_verifications": contact_verifications,
        "location_info": location_info,
        "social_info": social_info,
        "business_info": business_info,
        "followings_count": followings_count,
        "followers_count": followers_count,
        "products_listed_count": products_listed_count,
        "active_products_count": active_products_count,
        "brands_count": brands_count,
        "categories_count": categories_count,
        "brands": brands,
        "categories": categories,
        "recent_viewers": recent_viewers,
        "profile_view_count": profile_info.profile_views,
        "is_dealer": user.is_dealer,
        "is_fully_verified": user.is_fully_verified,
        "needs_password_rotation": user.needs_password_rotation,
        "is_locked": user.is_locked,
        "completion_percentage": profile_info.completion_percentage,
        "business_completion_percentage": (
            business_info.completion_percentage if business_info else 0
        ),
        "social_link_count": social_info.link_count if social_info else 0,
    }
    return render(request, "customer/profile_managers.html", context)



@login_required(login_url="/customer/signin/")
def contact_oauth_start(request, channel):
    """
    Direct-link entry point into the OAuth handshake for a channel.

    Not currently linked from profile_managers.html (the template's
    "Verify" button drives OAuth channels through `start_verification`
    above via AJAX instead), but kept as a plain-redirect alternative for
    any other entry point (e.g. an email reminder link) that wants a
    normal browser navigation rather than a fetch() call.
    """
    if CHANNEL_VERIFICATION_METHODS.get(channel) != ContactVerification.Method.OAUTH:
        return redirect("/user/profile_manager/#contact")

    contact_info, _ = ContactInfo.objects.get_or_create(user=request.user)
    target_value = (getattr(contact_info, channel, "") or "").strip()
    if not target_value:
        messages.error(request, "Set a value for this field before verifying it.")
        return redirect("/user/profile_manager/#contact")

    token = secrets.token_urlsafe(24)
    try:
        authorize_url = _build_oauth_authorize_url(request, channel, request.user, token)
    except VerificationDeliveryError as exc:
        messages.error(request, str(exc))
        return redirect("/user/profile_manager/#contact")

    verification, _ = ContactVerification.objects.get_or_create(
        contact=contact_info, channel=channel,
        defaults={"method": ContactVerification.Method.OAUTH},
    )
    verification.method = ContactVerification.Method.OAUTH
    verification.otp_code_hash = _hash_code(token)
    verification.otp_expires_at = timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)
    verification.status = ContactVerification.Status.PENDING
    verification.save()

    return redirect(authorize_url)


@login_required(login_url="/customer/signin/")
def contact_oauth_callback(request, channel):
    """
    Provider redirects back here with `state` (our signed payload) and
    `code` (the provider's authorization code).

    This validates the state signature/expiry/user/channel match and that
    it corresponds to a still-pending verification for this channel
    before doing anything else, so a forged or replayed callback can't
    verify a channel it doesn't belong to.

    The actual code -> token -> provider-profile exchange is provider-
    specific (and needs each provider's client secret configured), so
    it's left as a TODO -- everything around it is real and safe to run
    as-is even before that piece lands.
    """
    error = request.GET.get("error")
    if error:
        messages.error(request, "The connection was cancelled or failed.")
        return redirect("/user/profile_manager/#contact")

    state = request.GET.get("state", "")
    code = request.GET.get("code", "")

    try:
        payload = signing.loads(state, max_age=OTP_EXPIRY_MINUTES * 60)
    except signing.BadSignature:
        messages.error(request, "This verification link is invalid or has expired.")
        return redirect("/user/profile_manager/#contact")

    if payload.get("user_id") != request.user.pk or payload.get("channel") != channel:
        messages.error(request, "This verification link doesn't match your account.")
        return redirect("/user/profile_manager/#contact")

    try:
        verification = ContactVerification.objects.get(
            contact__user=request.user, channel=channel,
            status=ContactVerification.Status.PENDING,
        )
    except ContactVerification.DoesNotExist:
        messages.error(request, "No pending verification found for this channel.")
        return redirect("/user/profile_manager/#contact")

    if _hash_code(payload.get("token", "")) != verification.otp_code_hash:
        messages.error(request, "This verification link has already been used.")
        return redirect("/user/profile_manager/#contact")

    if not code:
        messages.error(request, "The provider didn't return an authorization code.")
        return redirect("/user/profile_manager/#contact")

    # TODO: exchange `code` for an access token with the provider (using
    # OAUTH_PROVIDER_CONFIG[channel] plus a client-secret setting), fetch
    # the provider's profile, and set `provider_identifier` to the
    # returned handle/ID. Recommend django-allauth or
    # social-auth-app-django rather than hand-rolling the exchange for
    # each of the 7 OAuth channels.
    provider_identifier = None  # placeholder until the exchange above is implemented

    contact_info = verification.contact
    current_value = getattr(contact_info, channel, "") or ""

    if provider_identifier and provider_identifier.lstrip("@").lower() == current_value.lstrip("@").lower():
        verification.status = ContactVerification.Status.VERIFIED
        verification.verified_value = current_value
        verification.verified_at = timezone.now()
        verification.otp_code_hash = ""
        verification.otp_expires_at = None
        verification.save()
        messages.success(request, "Verified.")
    else:
        verification.status = ContactVerification.Status.FAILED
        verification.save(update_fields=["status", "updated_at"])
        messages.error(request, "We couldn't confirm this account. Please try again.")

    return redirect("/user/profile_manager/#contact")