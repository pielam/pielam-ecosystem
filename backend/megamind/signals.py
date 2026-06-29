# megamind/signals.py

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from megamind.models.connected_service import ConnectedService
from apps.customer.views.profile import invalidate_engine

@receiver(post_save, sender=ConnectedService)
@receiver(post_delete, sender=ConnectedService)
def bust_engine_cache(sender, instance, **kwargs):
    invalidate_engine(instance.user_id)