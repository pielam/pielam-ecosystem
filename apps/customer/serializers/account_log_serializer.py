# apps/customer/serializers/account_log_serializer.py

"""
Serializers for AccountLog (apps/customer/models/account_log.py).

READ-ONLY, BY DESIGN
------------------------------------------------------------------
AccountLog rows are written exclusively by the pre_save/post_save
signal handlers in account_log.py (plus the occasional direct
AccountLog.objects.record() call for events that don't go through
User.save() at all). There is no legitimate client-facing "create an
AccountLog" or "edit an AccountLog" action -- a log a user could edit
isn't an audit trail. Every serializer below is therefore read-only:
no `create()`/`update()`, and every field is listed under
`read_only_fields` (or is a `read_only=True` SerializerMethodField) so
this can't accidentally become writable just because a future
ModelSerializer field gets added without an explicit override.

WHY TWO SERIALIZERS (LIST vs. DETAIL)
------------------------------------------------------------------
`user` (the account the log is about) is deliberately omitted from
AccountLogSerializer -- these are meant to be nested under a
per-user history endpoint (`/users/<uuid>/logs/`), where repeating the
same user on every row is redundant. AccountLogAdminSerializer is the
flatter, standalone shape for a cross-user admin log view (e.g.
"recent account changes across the whole platform"), where knowing
*which* user each row belongs to is the entire point.

`changes` is passed through as-is (already a plain JSON-safe dict --
see account_log.py's `_diff()`/`_snapshot()`) rather than re-shaped
here, so a new tracked field on User is reflected automatically
without touching this file.

WHO SHOULD BE ALLOWED TO SEE THIS
------------------------------------------------------------------
AccountLog rows can contain IP addresses, user agents, and old/new
values of fields like `email`, `phone`, and `last_login_ip` -- this is
sensitive audit data, not public profile data. Restricting access
(the account owner, or staff) is a view/permission-class concern and
is intentionally NOT enforced here; these serializers only define
*shape*, not *who gets to request it*.
"""

from rest_framework import serializers

from apps.customer.models.account_log import AccountLog


# ====================================================================
# NESTED / PER-USER HISTORY VIEW
# ====================================================================

class AccountLogSerializer(serializers.ModelSerializer):
    """
    One row of a single user's own change history -- intended to be
    listed under a `/users/<uuid>/logs/` or `/me/logs/` endpoint where
    `user` is already implied by the URL, so it's left off here.
    """

    action_display = serializers.CharField(source="get_action_display", read_only=True)
    performed_by_display = serializers.SerializerMethodField()

    class Meta:
        model = AccountLog
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
        None -- see account_log.py's docstring on what leaving the
        transient _account_log_actor unset means), otherwise the
        acting user's own display name rather than exposing their raw
        email_or_phone in someone else's history feed.
        """
        if obj.performed_by is None:
            return "Self-service / system"
        return obj.performed_by.display_name


# ====================================================================
# ADMIN / CROSS-USER VIEW
# ====================================================================

class AccountLogAdminSerializer(serializers.ModelSerializer):
    """
    Standalone, cross-user shape for an admin-facing log stream --
    includes `user` (who the log entry is about), since that's no
    longer implied by the URL the way it is for AccountLogSerializer.
    """

    action_display = serializers.CharField(source="get_action_display", read_only=True)
    user_identifier = serializers.CharField(source="user.email_or_phone", read_only=True)
    performed_by_identifier = serializers.SerializerMethodField()

    class Meta:
        model = AccountLog
        fields = [
            "id",
            "user",
            "user_identifier",
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