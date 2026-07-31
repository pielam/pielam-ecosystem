"""
Admin package initialization
File: admin/__init__.py
"""

# Import all admin configurations
# This makes the admin classes available when importing from the admin package
__all__ = ['UserAdmin']

from .keys_admin import *
from .recovery_admin import *
from .profile_info_admin import *
from .connected_service_admin import *


# megamind/admin/__init__.py
#
# Django's admin.autodiscover() only imports each app's top-level
# `admin` module/package — it does NOT recurse into submodules on its
# own. Since `admin.py` is now a package (megamind/admin/), each
# submodule that calls @admin.register(...) must be imported here so
# those registrations actually run at startup. Forgetting this is the
# most common cause of "my model doesn't show up in the admin" after
# a refactor like this one.

from .visit_log_admin import DiscoveryVisitLogAdmin  # noqa: F401

__all__ = ['DiscoveryVisitLogAdmin']