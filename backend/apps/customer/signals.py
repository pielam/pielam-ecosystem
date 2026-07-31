# apps/customer/signals.py

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings

from apps.customer.models.profile_info import ProfileInfo


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_profile(sender, instance, created, **kwargs):
    """Create a ProfileInfo row automatically when a new User is created."""
    if created:
        ProfileInfo.objects.get_or_create(user=instance)


# NOTE:
# The previous `sync_urls_to_connected_services` receiver (post_save on
# ProfileInfo, auto-creating/updating ConnectedService rows for social/
# business URL fields) has been intentionally removed. It fired on every
# ProfileInfo.save() — including saves from increment_view_count(),
# verify(), suspend(), make_public(), feature(), etc. — and its
# update_or_create() lookup mixed a stable key (service_label) with a
# mutated value (personalized display name) in the same field, which
# produced duplicate ConnectedService rows on nearly every save and
# silently failed to disconnect cleared URLs.
#
# If ConnectedService rows need to be created/updated from ProfileInfo
# social fields going forward, do it explicitly (e.g. in a form/serializer
# save step, or an explicit service method the caller invokes), not as an
# implicit signal side effect of every ProfileInfo.save().