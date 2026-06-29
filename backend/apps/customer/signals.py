from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings

from apps.customer.models.profile_info import ProfileInfo   # adjust import if needed


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        # Create ProfileInfo only for new users
        ProfileInfo.objects.get_or_create(user=instance)


# app# apps/customer/signals.py

from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.customer.models.profile_info import ProfileInfo
from megamind.models.connected_service import ConnectedService


SOCIAL_FIELD_MAP = [
    # (profile_field, service_name, service_type)
    ('business_website', 'Business Website',  'business'),
    ('social_facebook',  'Facebook',          'person'),
    ('social_twitter',   'Twitter/X',         'person'),
    ('social_instagram', 'Instagram',         'person'),
    ('social_linkedin',  'LinkedIn',          'person'),
    ('social_youtube',   'YouTube',           'news'),
    ('social_tiktok',    'TikTok',            'news'),
]


@receiver(post_save, sender=ProfileInfo)
def sync_urls_to_connected_services(sender, instance, **kwargs):
    """
    When ProfileInfo is saved, auto-create or update a ConnectedService
    for every social/business URL field that has a value.
    """
    for field_name, service_label, service_type in SOCIAL_FIELD_MAP:
        url = getattr(instance, field_name, None)

        if not url:
            # If URL was cleared, disconnect the service
            ConnectedService.objects.filter(
                user=instance.user,
                profile=instance,
                service_type=service_type,
                service_name=service_label,
            ).update(is_connected=False, status='private')
            continue

        # Determine service name
        if field_name == 'business_website':
            name = instance.business_name or instance.profile_name or service_label
        else:
            name = f"{instance.profile_name or instance.user.email_or_phone} — {service_label}"

        ConnectedService.objects.update_or_create(
            user=instance.user,
            profile=instance,
            service_name=service_label,
            service_type=service_type,
            defaults={
                'service_url':  url,
                'service_name': name,
                'status':       'public',
                'is_connected': True,
            }
        )