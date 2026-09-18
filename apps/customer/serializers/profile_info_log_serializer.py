# apps/customer/serializers/profile_info_log_serializer.py

"""
Serializers for ProfileInfoLog (apps/customer/models/profile_info_log.py).

READ-ONLY, BY DESIGN
------------------------------------------------------------------
Same reasoning as account_log_serializer.py: ProfileInfoLog rows are
written exclusively by the pre_save/post_save signal handlers in
profile_info_log.py (plus the occasional direct
ProfileInfoLog.objects.record() call for events outside ProfileInfo.save(),
e.g. a future m2m_changed-based follow/block log). There is no
legitimate client-facing create/edit action here -- every serializer
below is read-only end to end: no create()/update(), every field
listed under read_only_fields.

WHY TWO SERIALIZERS (NESTED vs. ADMIN)
------------------------------------------------------------------
`profile` is omitted from ProfileInfoLogSerializer -- meant to be
nested under a per-profile history endpoint
(`/profiles/<uuid>/logs/` or `/me/profile/logs/`), where repeating the
same profile on every row is redundant. ProfileInfoLogAdminSerializer
is the flatter, standalone shape for a cross-profile admin log view
(e.g. "recent profile moderation actions across the platform"), where
knowing *which* profile each row belongs to is the entire point.

`changes` is passed through as-is (already a plain JSON-safe dict --
see profile_info_log.py's `_diff()`/`_snapshot()`/`_normalize()`)
rather than re-shaped here, so a new tracked field on ProfileInfo is
reflected automatically without touching this file. Note that
profile_photo/profile_cover_photo values inside `changes` are already
storage-path strings, not file objects or URLs -- see
_normalize()'s docstring for why.

WHO SHOULD BE ALLOWED TO SEE THIS
------------------------------------------------------------------
ProfileInfoLog rows can include moderation history (suspension
reasons, verification actions) and, via `changes`, whatever value a
privacy toggle was flipped to/from -- this is account-history data,
not public profile data. Restricting access (the profile owner, or
staff) is a view/permission-class concern and is intentionally NOT
enforced here; these serializers only define *shape*, not *who gets to
request it*.
"""

from rest_framework import serializers

from apps.customer.models.profile_info_log import ProfileInfoLog


# ====================================================================
# NESTED / PER-PROFILE HISTORY VIEW
# ====================================================================

class ProfileInfoLogSerializer(serializers.ModelSerializer):
    """
    One row of a single profile's own change history -- intended to be
    listed under a `/profiles/<uuid>/logs/` or `/me/profile/logs/`
    endpoint where `profile` is already implied by the URL, so it's
    left off here.
    """

    action_display = serializers.CharField(source="get_action_display", read_only=True)
    performed_by_display = serializers.SerializerMethodField()

    class Meta:
        model = ProfileInfoLog
        fields = [
            "id",
            "action",
            "action_display",
            "changes",
            "performed_by_display",
            "ip_address",
            "user_agent",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields

    def get_performed_by_display(self, obj) -> str:
        """
        'Self-service / system' for the common case (performed_by is
        None -- see profile_info_log.py's docstring on what leaving the
        transient _profile_log_actor unset means), otherwise the
        acting user's own display name rather than exposing their raw
        email_or_phone in someone else's history feed.
        """
        if obj.performed_by is None:
            return "Self-service / system"
        return obj.performed_by.display_name


# ====================================================================
# ADMIN / CROSS-PROFILE VIEW
# ====================================================================

class ProfileInfoLogAdminSerializer(serializers.ModelSerializer):
    """
    Standalone, cross-profile shape for an admin-facing log stream --
    includes `profile` (which profile the log entry is about), since
    that's no longer implied by the URL the way it is for
    ProfileInfoLogSerializer.
    """

    action_display = serializers.CharField(source="get_action_display", read_only=True)
    profile_identifier = serializers.CharField(source="profile.user.email_or_phone", read_only=True)
    profile_uuid = serializers.UUIDField(source="profile.uuid", read_only=True)
    performed_by_identifier = serializers.SerializerMethodField()

    class Meta:
        model = ProfileInfoLog
        fields = [
            "id",
            "profile",
            "profile_uuid",
            "profile_identifier",
            "action",
            "action_display",
            "changes",
            "performed_by",
            "performed_by_identifier",
            "ip_address",
            "user_agent",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields

    def get_performed_by_identifier(self, obj):
        if obj.performed_by is None:
            return None
        return obj.performed_by.email_or_phone