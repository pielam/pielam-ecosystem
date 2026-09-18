# apps/customer/serializers/account_serializer.py

"""
Serializers for the User model (apps/customer/models/account.py).

WHY SEVERAL SERIALIZERS INSTEAD OF ONE
------------------------------------------------------------------
User carries security-sensitive fields (password, mfa_secret,
failed_login_attempts, last_login_ip, ...) alongside ordinary profile
preferences (timezone, language, currency). A single ModelSerializer
with a blanket `fields = "__all__"` would either leak security
internals to end users or require constant per-view field
overrides. Instead, each serializer below is scoped to one call site:

    UserRegistrationSerializer   -- POST /register
    UserLoginSerializer          -- POST /login (validation only, not a ModelSerializer)
    UserSelfSerializer           -- GET  /me            (own account, full-ish read)
    UserSelfUpdateSerializer     -- PATCH /me           (own account, narrow write)
    UserPublicSerializer         -- GET  /users/<uuid>/ (anyone else's account, minimal)
    ChangePasswordSerializer     -- POST /me/change-password
    AccountStatusSerializer      -- PATCH /admin/users/<uuid>/status (staff-only)

None of these ever serialize `password`, `mfa_secret`, or other
credential material -- that's enforced by simply never listing those
fields, not by an explicit exclude, so a future field added to User
doesn't accidentally become writable/readable just by not being
excluded.

WRITE PATHS GO THROUGH THE MODEL'S OWN METHODS
------------------------------------------------------------------
Where account.py already exposes a method for a state transition
(accept_terms, give_marketing_consent, change_role, soft_delete, ...),
these serializers call that method rather than setting the field(s)
directly and calling save(). That keeps the *_log.py change-history
signals, the update_fields-scoped saves, and any future logic added
inside those methods as the single source of truth for what a
"password change" or "role change" actually does -- a serializer
should not be a second place that duplicates or drifts from that
behavior.
"""

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.customer.models.account import User


# ====================================================================
# PUBLIC (READ-ONLY, ANYONE-VIEWING-SOMEONE-ELSE)
# ====================================================================

class UserPublicSerializer(serializers.ModelSerializer):
    """
    What one user is allowed to see about another. Deliberately tiny --
    no email/phone, no security fields, no locale/currency preferences.
    Anything else public-facing (bio, follower count, photo) lives on
    ProfileInfo's own serializer and is composed alongside this one at
    the view/aggregation layer (see user_info.py's UserInfo), not
    duplicated here.
    """

    display_name = serializers.CharField(read_only=True)
    is_verified = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = [
            "uuid",
            "display_name",
            "role",
            "is_verified",
            "date_joined",
        ]
        read_only_fields = fields


# ====================================================================
# SELF (READ) -- THE LOGGED-IN USER VIEWING THEIR OWN ACCOUNT
# ====================================================================

class UserSelfSerializer(serializers.ModelSerializer):
    """
    Full-ish read view of a user's own account -- everything they'd
    reasonably need on a settings/account page, still excluding raw
    security internals (mfa_secret, password hash) and admin-only
    audit fields (last_login_ip/user_agent, failed_login_attempts,
    locked_until) that belong on an admin-facing serializer instead,
    not here.
    """

    display_name = serializers.CharField(read_only=True)
    is_verified = serializers.BooleanField(read_only=True)
    is_fully_verified = serializers.BooleanField(read_only=True)
    is_locked = serializers.BooleanField(read_only=True)
    needs_password_rotation = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = [
            "uuid",
            "email_or_phone",
            "email",
            "phone",
            "role",
            "account_status",
            "email_verified",
            "phone_verified",
            "mfa_enabled",
            "mfa_method",
            "timezone",
            "language",
            "country",
            "currency",
            "marketing_consent",
            "terms_accepted",
            "privacy_accepted",
            "require_password_change",
            "date_joined",
            "display_name",
            "is_verified",
            "is_fully_verified",
            "is_locked",
            "needs_password_rotation",
        ]
        read_only_fields = [
            "uuid",
            "email_or_phone",
            "role",
            "account_status",
            "email_verified",
            "phone_verified",
            "mfa_enabled",
            "mfa_method",
            "require_password_change",
            "date_joined",
            "display_name",
            "is_verified",
            "is_fully_verified",
            "is_locked",
            "needs_password_rotation",
        ]


# ====================================================================
# SELF (WRITE) -- NARROW, PREFERENCE-ONLY UPDATE
# ====================================================================

class UserSelfUpdateSerializer(serializers.ModelSerializer):
    """
    What a user can change about their own account through a plain
    PATCH. Intentionally excludes email/phone/email_or_phone (those
    need their own verify-then-swap flow, not a bare field edit),
    role/account_status (admin-only, see AccountStatusSerializer), and
    password (see ChangePasswordSerializer) -- all state transitions
    that need side effects beyond "set the field and save".
    """

    class Meta:
        model = User
        fields = [
            "timezone",
            "language",
            "country",
            "currency",
        ]

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save(update_fields=list(validated_data.keys()))
        return instance


# ====================================================================
# REGISTRATION
# ====================================================================

class UserRegistrationSerializer(serializers.ModelSerializer):
    """
    Account creation. Accepts a password (validated with Django's
    configured password validators, not just a min-length check) and a
    confirmation field that's never persisted. terms_accepted and
    privacy_accepted must be explicitly true -- this mirrors
    account.py's own consent-tracking fields, which only make sense if
    the acceptance actually happened at registration time rather than
    defaulting to True unexamined.
    """

    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
    )
    password_confirm = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
    )

    class Meta:
        model = User
        fields = [
            "email_or_phone",
            "email",
            "password",
            "password_confirm",
            "language",
            "country",
            "currency",
            "marketing_consent",
            "terms_accepted",
            "privacy_accepted",
        ]

    def validate_password(self, value):
        # Runs Django's AUTH_PASSWORD_VALIDATORS (min length, common
        # password check, similarity to user attrs, numeric-only,
        # etc.) rather than reimplementing password rules here.
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def validate(self, attrs):
        if attrs.get("password") != attrs.get("password_confirm"):
            raise serializers.ValidationError(
                {"password_confirm": _("Passwords do not match.")}
            )

        if not attrs.get("terms_accepted"):
            raise serializers.ValidationError(
                {"terms_accepted": _("You must accept the terms and conditions.")}
            )

        if not attrs.get("privacy_accepted"):
            raise serializers.ValidationError(
                {"privacy_accepted": _("You must accept the privacy policy.")}
            )

        return attrs

    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")

        # terms_accepted/privacy_accepted are consumed here via their
        # own methods (which also stamp *_accepted_date) rather than
        # passed straight through to create_user, so the timestamp
        # fields aren't silently left null.
        validated_data.pop("terms_accepted", None)
        validated_data.pop("privacy_accepted", None)
        marketing_consent = validated_data.pop("marketing_consent", False)

        user = User.objects.create_user(password=password, **validated_data)

        user.accept_terms(save=False)
        user.accept_privacy(save=False)
        if marketing_consent:
            user.give_marketing_consent(save=False)
        user.save()

        return user

    def to_representation(self, instance):
        # Return the same shape as UserSelfSerializer after creation,
        # rather than echoing back the write-only password fields.
        return UserSelfSerializer(instance, context=self.context).data


# ====================================================================
# LOGIN
# ====================================================================

class UserLoginSerializer(serializers.Serializer):
    """
    Not a ModelSerializer -- login is a credential check, not a
    create/update of a User row. On success, `validate()` attaches the
    authenticated user as `self.user` for the view to pull
    User.record_successful_login()/record_failed_login() around, since
    this serializer only validates credentials and deliberately
    doesn't decide login-attempt bookkeeping itself.
    """

    email_or_phone = serializers.CharField()
    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
    )

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            username=attrs["email_or_phone"],
            password=attrs["password"],
        )

        if user is None:
            raise serializers.ValidationError(
                _("Unable to log in with the provided credentials."),
                code="authorization",
            )

        if user.is_locked:
            raise serializers.ValidationError(
                _("This account is temporarily locked. Try again later."),
                code="locked",
            )

        if not user.is_active or user.deleted_at is not None:
            raise serializers.ValidationError(
                _("This account is inactive."),
                code="inactive",
            )

        attrs["user"] = user
        return attrs


# ====================================================================
# PASSWORD CHANGE
# ====================================================================

class ChangePasswordSerializer(serializers.Serializer):
    """
    Requires the current password (even for an already-authenticated
    session) before allowing a change -- standard defense against a
    hijacked/left-open session being used to lock the real owner out.
    Delegates the actual field update to User.set_password(), which
    already stamps password_changed_at and clears
    require_password_change on its own.
    """

    old_password = serializers.CharField(write_only=True, style={"input_type": "password"})
    new_password = serializers.CharField(write_only=True, style={"input_type": "password"})
    new_password_confirm = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate_old_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError(_("Current password is incorrect."))
        return value

    def validate_new_password(self, value):
        user = self.context["request"].user
        try:
            validate_password(value, user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def validate(self, attrs):
        if attrs["new_password"] != attrs["new_password_confirm"]:
            raise serializers.ValidationError(
                {"new_password_confirm": _("Passwords do not match.")}
            )
        if attrs["old_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": _("New password must be different from the current password.")}
            )
        return attrs

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password", "password_changed_at", "require_password_change"])
        return user


# ====================================================================
# ADMIN -- ACCOUNT STATUS / ROLE MANAGEMENT
# ====================================================================

class AccountStatusSerializer(serializers.ModelSerializer):
    """
    Staff-only surface for changing role/account_status. Role changes
    are routed through User.change_role() (which also updates
    is_staff/is_superuser to match) rather than writing `role`
    directly, so this serializer can't produce the inconsistent state
    change_role() exists specifically to prevent.
    """

    class Meta:
        model = User
        fields = [
            "uuid",
            "role",
            "account_status",
            "is_active",
        ]
        read_only_fields = ["uuid"]

    def validate_role(self, value):
        if value not in dict(User.Role.choices):
            raise serializers.ValidationError(_("Invalid role."))
        return value

    def update(self, instance, validated_data):
        new_role = validated_data.pop("role", None)
        if new_role and new_role != instance.role:
            instance.change_role(new_role, save=False)

        for field, value in validated_data.items():
            setattr(instance, field, value)

        instance.save()
        return instance