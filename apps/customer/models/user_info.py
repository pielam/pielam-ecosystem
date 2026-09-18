# apps/customer/models/user_info.py

"""
UserInfo — single read/aggregation point across the five user-related
tables: User, ProfileInfo, ContactInfo, LocationInfo, SocialInfo.

WHY THIS EXISTS
------------------------------------------------------------------
Each of those five models lives in its own file for good reasons (see
their docstrings — ProfileInfo is core identity, ContactInfo is
reachability, LocationInfo/SocialInfo are sparse/optional and lazily
created). But call sites (views, serializers, dashboards) very often
want "give me everything about this user" in one shot rather than
five separate lookups. This module is that one shot.

It does NOT introduce a new table or model — it's a thin aggregation
layer over the existing five, using select_related where the schema
allows it (ProfileInfo/ContactInfo are OneToOne-on-PK with User, so
they ride along in the same query) and defensive fetches for the two
lazily-created ones (LocationInfo/SocialInfo may simply not exist yet).

USAGE
------------------------------------------------------------------
    from apps.customer.models.user_info import get_user_info

    info = get_user_info(request.user)          # from a User instance
    info = get_user_info(user_id)                # from a pk
    info = get_user_info(user_id, create_missing=True)  # also creates
                                                          # LocationInfo/
                                                          # SocialInfo rows
                                                          # if missing

    info.user           # User instance
    info.profile         # ProfileInfo instance (always exists — signal-created)
    info.contact         # ContactInfo instance or None
    info.location        # LocationInfo instance or None (or created, see above)
    info.social           # SocialInfo instance or None (or created, see above)

    info.display_name()  # convenience passthroughs, see below
    info.export_data()   # merged GDPR export across all five
"""

from dataclasses import dataclass, field
from typing import Optional, Union

from django.core.exceptions import ObjectDoesNotExist

from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo
from apps.customer.models.contact_info import ContactInfo
from apps.customer.models.location_info import LocationInfo
from apps.customer.models.social_info import SocialInfo


@dataclass
class UserInfo:
    """
    Bag of the five related objects for one user, plus a handful of
    convenience methods that just delegate to whichever sub-model
    actually owns that piece of data. Prefer accessing `.profile`,
    `.contact`, etc. directly for anything not covered here — this
    class deliberately doesn't try to re-expose every field on every
    sub-model, just the ones that get asked for across several call
    sites.
    """

    user: User
    profile: Optional[ProfileInfo] = None
    contact: Optional[ContactInfo] = None
    location: Optional[LocationInfo] = None
    social: Optional[SocialInfo] = None

    # ----------------------------------------------------------------
    # Convenience passthroughs
    # ----------------------------------------------------------------

    def display_name(self) -> str:
        """Best available display name (delegates to User.display_name)."""
        return self.user.display_name

    def is_verified(self) -> bool:
        """True if either the account (email/phone) or the profile is verified."""
        profile_verified = bool(self.profile and self.profile.is_profile_verified)
        return self.user.is_verified or profile_verified

    def location_display(self) -> Optional[str]:
        """
        City/State/Country string, honoring ProfileInfo.show_location
        the same way a view rendering this should (see LocationInfo's
        docstring — that toggle was never moved onto LocationInfo
        itself, so it has to be checked here, not on `.location`).
        """
        if not self.location:
            return None
        if self.profile and not self.profile.show_location:
            return None
        return self.location.location_display

    def visible_contact_methods(self, viewer=None) -> dict:
        """Delegates to ContactInfo.get_visible_contact_methods(), or {} if no ContactInfo row exists yet."""
        if not self.contact:
            return {}
        return self.contact.get_visible_contact_methods(viewer=viewer)

    def social_links(self) -> dict:
        """Delegates to SocialInfo.all_links, or {} if no SocialInfo row exists yet."""
        if not self.social:
            return {}
        return self.social.all_links

    # ----------------------------------------------------------------
    # GDPR export
    # ----------------------------------------------------------------

    def export_data(self) -> dict:
        """
        Merged export across every sub-model that exists for this
        user. Each model already knows how to export its own fields
        (see their respective export_data() methods) — this just
        nests them under one dict instead of duplicating field lists
        here, so a change to any one model's export_data() doesn't
        require touching this file too.
        """
        data = {"account": self.user.export_data()}
        if self.profile:
            data["profile"] = self.profile.export_data()
        if self.contact:
            data["contact"] = self.contact.export_data()
        if self.location:
            data["location"] = self.location.export_data()
        if self.social:
            data["social"] = self.social.export_data()
        return data


def get_user_info(
    user_or_id: Union[User, int, str],
    *,
    create_missing: bool = False,
) -> UserInfo:
    """
    Fetch a User together with all four extension rows in as few
    queries as the schema allows.

    Query cost:
      - 1 query for User + ProfileInfo + ContactInfo (select_related,
        since both are OneToOne-on-PK with User).
      - Up to 2 more queries for LocationInfo/SocialInfo, since
        neither is guaranteed to exist (they're lazily created — see
        their own docstrings) and select_related can't safely assume
        the reverse OneToOne row is there.
      - With create_missing=True, those last two become get_or_create
        calls instead of plain fetches (still 1 query each in the
        common case where the row already exists).

    Raises User.DoesNotExist if `user_or_id` is a pk that doesn't
    match any user (soft-deleted users are still matched, deliberately
    — this is a read helper, not an access-control check; callers that
    care about deleted_at/is_active should check `.user` on the
    result).
    """
    if isinstance(user_or_id, User):
        user = user_or_id
        # Re-fetch with select_related so we don't pay for two extra
        # queries below if profile/contact turn out to already be
        # cached on the passed-in instance from a prior select_related
        # — Django's cache makes this a no-op query-wise when they are.
        user = (
            User.objects.select_related("profileinfo", "contactinfo")
            .get(pk=user.pk)
        )
    else:
        user = (
            User.objects.select_related("profileinfo", "contactinfo")
            .get(pk=user_or_id)
        )

    try:
        profile = user.profileinfo
    except ObjectDoesNotExist:
        # Shouldn't normally happen (ProfileInfo is signal-created),
        # but degrade gracefully rather than raising for a caller that
        # just wants a read.
        profile = None

    try:
        contact = user.contactinfo
    except ObjectDoesNotExist:
        contact = None

    if create_missing:
        location, _ = LocationInfo.objects.get_or_create_for_user(user)
        social, _ = SocialInfo.objects.get_or_create_for_user(user)
    else:
        location = LocationInfo.objects.for_user(user)
        social = SocialInfo.objects.for_user(user)

    return UserInfo(
        user=user,
        profile=profile,
        contact=contact,
        location=location,
        social=social,
    )