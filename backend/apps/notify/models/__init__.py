# apps/notify/models/__init__.py
#
# This file was empty. Django imports ``<app>.models`` at startup, so with no
# imports here the ``Notification`` model was never registered: it did not
# appear in migrations, in the admin, or in ``from apps.notify.models import
# Notification``.

from .ring_bell import Notification, NotificationManager

__all__ = ["Notification", "NotificationManager"]
