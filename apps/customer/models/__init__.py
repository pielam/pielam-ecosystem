# apps/customer/models/__init__.py

from .account import User
from .profile_info import *
from .contact_info import *
from .location_info import *
from .social_info import *

# Change-history models. Signal wiring (pre_save/post_save receivers)
# happens in CustomerConfig.ready() (apps.py), NOT here -- these
# imports exist only to expose each class at the `apps.customer.models`
# package level (e.g. `from apps.customer.models import AccountLog`)
# rather than requiring callers to reach into the submodule directly.
# Imported explicitly (not `import *`) because every one of these
# modules defines its own module-level TRACKED_FIELDS and logger,
# which would otherwise silently overwrite each other's if
# wildcard-imported here.
from .account_log import AccountLog, AccountLogManager
from .contact_info_log import ContactInfoLog, ContactInfoLogManager
from .location_info_log import LocationInfoLog, LocationInfoLogManager
from .profile_info_log import ProfileInfoLog, ProfileInfoLogManager
from .social_info_log import SocialInfoLog, SocialInfoLogManager

from .profile_view_log import ProfileViewLog

# Cross-model read/aggregation helper (see user_info.py) -- not a
# table itself, but the intended entry point for "give me everything
# about this user" call sites.
from .user_info import UserInfo, get_user_info

from .business_info import BusinessInfo, BusinessInfoManager, BusinessInfoQuerySet
from .business_info_log import BusinessInfoLog, BusinessInfoLogManager