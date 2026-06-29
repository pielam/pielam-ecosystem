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