# apps/customer/serializers/profile_info_serializer.py

"""
Serializers for ProfileInfo (apps/customer/models/profile_info.py).

SCOPED SERIALIZERS, SAME PATTERN AS account_serializer.py
------------------------------------------------------------------
    ProfilePublicSerializer     -- GET  /profiles/<slug_or_uuid>/   (anyone viewing this profile)
    ProfileSelfSerializer       -- GET  /me/profile/                (own profile, full read)
    ProfileSelfUpdateSerializer -- PATCH /me/profile/                (own profile, narrow write)
    ProfileImageUpdateSerializer-- PATCH /me/profile/photo/          (own profile, photo-only)
    ProfilePrivacyUpdateSerializer -- PATCH /me/profile/privacy/     (own profile, show_*/allow_* toggles)
    ProfileNotificationUpdateSerializer -- PATCH /me/profile/notifications/
    ProfileAdminSerializer      -- PATCH /admin/profiles/<uuid>/     (staff-only: verify/suspend/feature)
    FollowActionSerializer      -- POST  /profiles/<uuid>/follow/    (input-only, no model fields)

WHY THIS SPLIT MATTERS HERE ESPECIALLY
------------------------------------------------------------------
ProfileInfo mixes public-facing content (bio, photo), privacy-sensitive
toggles (show_email, show_phone), moderation state (is_profile_suspended,
suspension_reason), and internal analytics (profile_views) on one model.
A single serializer would either leak suspension reasons to the public
profile page or require per-view field overrides scattered everywhere.
Each serializer below is scoped to exactly one call site instead.

STALE FIELDS FROM THE MODEL'S OWN CHANGELOG
------------------------------------------------------------------
profile_info.py's changelog documents several fields that were removed
(profile_type, verification_level, profile_tagline, profile_phone, all
location/business/social blocks) but are still read/written by other,
untouched files (dashboard.py, profile.py, profile_edit.py,
profile_managers.py, public_profile.py). None of those removed fields
appear anywhere below -- this file only ever serializes what actually
exists on the current model.

WRITE PATHS GO THROUGH THE MODEL'S OWN METHODS
------------------------------------------------------------------
Same rule as account_serializer.py: verify(), suspend(), block_user(),
follow_profile(), etc. already exist on ProfileInfo and already handle
their side effects (timestamps, M2M mirroring, notifications) correctly
-- see profile_info.py's block_user() changelog entry #1 for exactly
the kind of bug that happens when a serializer's update() bypasses a
model method and sets fields directly instead.
"""

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.customer.models.profile_info import ProfileInfo


# ====================================================================
# PUBLIC (READ-ONLY, ANYONE VIEWING THIS PROFILE)
# ====================================================================

class ProfilePublicSerializer(serializers.ModelSerializer):
    """
    What one user is allowed to see about someone else's profile.

    Privacy toggles are respected here rather than left to the frontend:
    show_email/show_phone (email/phone actually live on User, not here,
    so those are composed at the view/aggregation layer -- see
    account_serializer.py's UserPublicSerializer note on the same
    split), show_dob/show_age, and show_followers/show_following each
    gate their corresponding field to None when the profile owner has
    turned that toggle off, so a client can't get the real value just
    by knowing the field name.

    show_location is intentionally NOT wired to anything -- the model's
    own changelog (#8) flags it as dead weight now that every
    location field was removed. Left out entirely here rather than
    gating a field that no longer exists.
    """

    display_name = serializers.CharField(source="profile_name", read_only=True)
    photo_url = serializers.CharField(source="get_profile_photo_url", read_only=True)
    cover_photo_url = serializers.CharField(source="get_profile_cover_photo_url", read_only=True)
    age = serializers.SerializerMethodField()
    dob = serializers.SerializerMethodField()
    follower_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    profile_url = serializers.CharField(read_only=True)
    is_following = serializers.SerializerMethodField()

    class Meta:
        model = ProfileInfo
        fields = [
            "uuid",
            "display_name",
            "profile_name_slug",
            "profile_bio",
            "photo_url",
            "cover_photo_url",
            "profile_gender",
            "dob",
            "age",
            "is_profile_verified",
            "is_profile_featured",
            "follower_count",
            "following_count",
            "profile_url",
            "is_following",
        ]
        read_only_fields = fields

    def get_dob(self, obj):
        if obj.show_dob:
            return obj.profile_dob
        return None

    def get_age(self, obj):
        if obj.show_age:
            return obj.age
        return None

    def get_follower_count(self, obj):
        if obj.show_followers:
            return obj.follower_count
        return None

    def get_following_count(self, obj):
        if obj.show_following:
            return obj.following_count
        return None

    def get_is_following(self, obj) -> bool:
        """
        Whether the *requesting* user follows this profile. Needs
        `request` in context (view is responsible for passing it) --
        defaults to False for an anonymous/missing request rather than
        raising, since this is a nice-to-have, not a hard dependency.
        """
        request = self.context.get("request")
        if not request or not request.user or not request.user.is_authenticated:
            return False
        return obj.is_followed_by(request.user)


# ====================================================================
# SELF (READ) -- THE LOGGED-IN USER VIEWING THEIR OWN PROFILE
# ====================================================================

class ProfileSelfSerializer(serializers.ModelSerializer):
    """
    Full read view of a user's own profile -- unlike ProfilePublicSerializer,
    no toggle-gating (a user can always see their own data), and includes
    moderation state (is_profile_suspended, suspension_reason) and analytics
    (profile_views) that would never be shown to anyone else.
    """

    photo_url = serializers.CharField(source="get_profile_photo_url", read_only=True)
    cover_photo_url = serializers.CharField(source="get_profile_cover_photo_url", read_only=True)
    age = serializers.IntegerField(read_only=True)
    follower_count = serializers.IntegerField(read_only=True)
    following_count = serializers.IntegerField(read_only=True)
    is_complete = serializers.BooleanField(read_only=True)
    completion_percentage = serializers.IntegerField(read_only=True)
    profile_url = serializers.CharField(read_only=True)

    class Meta:
        model = ProfileInfo
        fields = [
            "uuid",
            "profile_name",
            "profile_name_slug",
            "profile_bio",
            "photo_url",
            "cover_photo_url",
            "profile_gender",
            "profile_dob",
            "profile_language",
            "age",
            "is_profile_verified",
            "is_profile_public",
            "is_profile_featured",
            "is_profile_suspended",
            "suspension_reason",
            "show_email",
            "show_phone",
            "show_dob",
            "show_age",
            "show_followers",
            "show_following",
            "allow_messages",
            "allow_follow",
            "notify_on_follow",
            "notify_on_message",
            "email_notifications",
            "sms_notifications",
            "follower_count",
            "following_count",
            "profile_views",
            "is_complete",
            "completion_percentage",
            "profile_url",
            "profile_creation_time",
        ]
        read_only_fields = [
            "uuid",
            "profile_name_slug",
            "is_profile_verified",
            "is_profile_featured",
            "is_profile_suspended",
            "suspension_reason",
            "age",
            "follower_count",
            "following_count",
            "profile_views",
            "is_complete",
            "completion_percentage",
            "profile_url",
            "profile_creation_time",
        ]


# ====================================================================
# SELF (WRITE) -- BASIC PROFILE FIELDS
# ====================================================================

class ProfileSelfUpdateSerializer(serializers.ModelSerializer):
    """
    What a user can change about their own basic profile info through a
    plain PATCH. Slug regeneration happens through generate_slug() (called
    from save() whenever profile_name changes and no slug exists yet) --
    this serializer never sets profile_name_slug directly.

    Excludes verification/suspension/featured flags (admin-only, see
    ProfileAdminSerializer) and privacy/notification toggles (their own
    narrower serializers below) so a single "edit profile" form can't
    accidentally also flip a moderation or privacy setting.
    """

    class Meta:
        model = ProfileInfo
        fields = [
            "profile_name",
            "profile_bio",
            "profile_gender",
            "profile_dob",
            "profile_language",
        ]

    def validate_profile_dob(self, value):
        # Mirrors clean()'s two distinct error messages (future date vs.
        # under-13) rather than deferring to full_clean() at save time,
        # so the API returns a field-specific error instead of a generic
        # non_field ValidationError.
        from datetime import date
        if value and value > date.today():
            raise serializers.ValidationError(_("Date of birth cannot be in the future."))
        if value:
            today = date.today()
            age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
            if age < 13:
                raise serializers.ValidationError(_("You must be at least 13 years old to create a profile."))
        return value

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        # Not update_fields-scoped: profile_name changing means save()
        # needs to run generate_slug(), and save() here always does a
        # full save (unlike account.py's User.save(), ProfileInfo's
        # save() doesn't special-case update_fields).
        instance.save()
        return instance


# ====================================================================
# SELF (WRITE) -- PHOTO / COVER PHOTO ONLY
# ====================================================================

class ProfileImageUpdateSerializer(serializers.ModelSerializer):
    """
    Separate from ProfileSelfUpdateSerializer since image uploads
    typically hit their own endpoint/multipart request rather than
    riding along with a JSON PATCH of text fields.
    """

    class Meta:
        model = ProfileInfo
        fields = ["profile_photo", "profile_cover_photo"]

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save(update_fields=list(validated_data.keys()))
        return instance


# ====================================================================
# SELF (WRITE) -- PRIVACY TOGGLES
# ====================================================================

class ProfilePrivacyUpdateSerializer(serializers.ModelSerializer):
    """
    show_location deliberately omitted -- see profile_info.py changelog
    #8, it's a no-op now that every location field is gone, and exposing
    a toggle that does nothing would be misleading on a settings page.
    """

    class Meta:
        model = ProfileInfo
        fields = [
            "is_profile_public",
            "show_email",
            "show_phone",
            "show_dob",
            "show_age",
            "show_followers",
            "show_following",
            "allow_messages",
            "allow_follow",
        ]

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save(update_fields=list(validated_data.keys()))
        return instance


# ====================================================================
# ADMIN -- VERIFY / FEATURE / SUSPEND / ARCHIVE
# ====================================================================

class ProfileAdminSerializer(serializers.ModelSerializer):
    """
    Staff-only surface for moderation actions. Each boolean flag is
    routed through its corresponding model method (verify/unverify,
    suspend/unsuspend, feature/unfeature) rather than set directly, so
    the side effects those methods own -- verified_at/verified_by,
    suspended_at/suspension_reason, etc. -- stay correct and consistent
    with AccountStatusSerializer's same reasoning for User.change_role().

    `verified_by`/`archived_by` are populated from the requesting admin
    (context["request"].user), matching account_log.py's convention of
    an explicit actor rather than trusting a client-supplied user id.
    """

    class Meta:
        model = ProfileInfo
        fields = [
            "uuid",
            "is_profile_verified",
            "is_profile_featured",
            "is_profile_suspended",
            "suspension_reason",
            "is_profile_archived",
        ]
        read_only_fields = ["uuid"]

    def update(self, instance, validated_data):
        admin_user = self.context["request"].user

        new_verified = validated_data.pop("is_profile_verified", None)
        if new_verified is not None and new_verified != instance.is_profile_verified:
            instance.verify(verified_by_user=admin_user, save=False) if new_verified else instance.unverify(save=False)

        new_featured = validated_data.pop("is_profile_featured", None)
        if new_featured is not None and new_featured != instance.is_profile_featured:
            instance.feature(save=False) if new_featured else instance.unfeature(save=False)

        new_suspended = validated_data.pop("is_profile_suspended", None)
        suspension_reason = validated_data.pop("suspension_reason", None)
        if new_suspended is not None and new_suspended != instance.is_profile_suspended:
            if new_suspended:
                instance.suspend(reason=suspension_reason, save=False)
            else:
                instance.unsuspend(save=False)

        new_archived = validated_data.pop("is_profile_archived", None)
        if new_archived is not None and new_archived != instance.is_profile_archived:
            if new_archived:
                instance.soft_delete(archived_by_user=admin_user, save=False)
            else:
                instance.restore(save=False)

        instance.save()
        return instance


# ====================================================================
# FOLLOW / UNFOLLOW / BLOCK -- INPUT-ONLY, NO MODEL FIELDS
# ====================================================================

class FollowActionSerializer(serializers.Serializer):
    """
    Not a ModelSerializer -- follow/unfollow/block are graph mutations
    on TWO ProfileInfo rows (see block_user()'s changelog entry #1 on
    why both sides matter), not a field edit on one. This serializer
    only validates the target; the view is expected to call
    follow_profile()/unfollow_profile()/block_user()/unblock_user() on
    context["request"].user.profileinfo with the resolved target.
    """

    target_uuid = serializers.UUIDField()

    def validate_target_uuid(self, value):
        try:
            target = ProfileInfo.objects.select_related("user").get(uuid=value)
        except ProfileInfo.DoesNotExist:
            raise serializers.ValidationError(_("Profile not found."))

        request = self.context.get("request")
        if request and target.user == request.user:
            raise serializers.ValidationError(_("You can't perform this action on your own profile."))

        self._target_profile = target
        return value

    @property
    def target_profile(self) -> ProfileInfo:
        return self._target_profile


# ====================================================================
# SELF (WRITE) -- NOTIFICATION PREFERENCES
# ====================================================================

class ProfileNotificationUpdateSerializer(serializers.ModelSerializer):
    """
    notify_on_comment/notify_on_mention are NOT here -- profile_info.py
    changelog #9 removed both fields from the model entirely. If a
    frontend settings form still posts those keys, DRF will simply
    ignore the unknown keys (they're not in `fields`), which is the
    correct behavior now, unlike profile_managers.py's setattr() loop
    which the changelog flags as silently accepting-and-discarding them.
    """

    class Meta:
        model = ProfileInfo
        fields = [
            "notify_on_follow",
            "notify_on_message",
            "email_notifications",
            "sms_notifications",
        ]

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save(update_fields=list(validated_data.keys()))
        return instance