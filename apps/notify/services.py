# apps/notify/services.py

from apps.notify.models.ring_bell import Notification

# apps/notify/services.py
def notice(
    recipient,
    notification_type,
    title,
    *,
    actor=None,
    message="",
    action_url="",
    image="",
    priority=Notification.Priority.NORMAL,
    send_email=False,
    data=None,
):
    if actor and actor.pk == recipient.pk:
        return None

    return Notification.objects.create(
        recipient=recipient,
        actor=actor,
        notification_type=notification_type,
        title=title,
        message=message,
        action_url=action_url,
        image=image,
        priority=priority,
        send_email=send_email,
        data=data or {},
    )