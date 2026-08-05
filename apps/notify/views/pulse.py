# apps/notify/views/pulse.py

"""
Notification Views
-------------------
Views backing apps/notify/urls.py:

    ''                                  -> NotificationView   (full notifications page)
    'unread-count/'                     -> unread_count       (polling API)
    'latest/'                           -> latest             (polling API)
    '<uuid:notification_uuid>/read/'    -> mark_read          (mark single notification read)
    'read-all/'                         -> mark_all_read      (mark all notifications read)

All views require an authenticated user and only ever operate on the
requesting user's own notifications.
"""

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from apps.notify.models.ring_bell import Notification


# ====================================================================
# HELPERS
# ====================================================================

DEFAULT_PAGE_SIZE = 20
LATEST_LIMIT = 10


def _serialize_notification(notification: Notification) -> dict:
    """Small JSON-safe representation of a notification for API responses"""
    return {
        'uuid': str(notification.uuid),
        'type': notification.notification_type,
        'priority': notification.priority,
        'title': notification.title,
        'message': notification.message,
        'action_url': notification.action_url,
        'icon': notification.icon,
        'is_read': notification.is_read,
        'created_at': notification.created_at.isoformat(),
        'read_at': notification.read_at.isoformat() if notification.read_at else None,
    }



from django.shortcuts import redirect

@login_required(login_url='/customer/signin/')
@require_GET
def go(request, slug):
    """Resolve a notification's slug link: mark it read, then redirect
    to its action_url (falls back to the notifications page)."""
    notification = get_object_or_404(
        Notification.objects.for_user(request.user),
        slug=slug,
    )
    notification.mark_read()
    return redirect(notification.action_url or 'notify:notifications')

# ====================================================================
# FULL NOTIFICATIONS PAGE
# ====================================================================

@login_required(login_url='/customer/signin/')
@require_GET
def NotificationView(request):
    """
    Render the full notifications page for the logged-in user.

    Supports optional filtering via query params:
        ?type=<notification_type>
        ?unread=1
    and pagination via ?page=<n>
    """
    queryset = Notification.objects.for_user(request.user)

    notification_type = request.GET.get('type')
    if notification_type:
        queryset = queryset.filter(notification_type=notification_type)

    if request.GET.get('unread') == '1':
        queryset = queryset.filter(is_read=False)

    paginator = Paginator(queryset, DEFAULT_PAGE_SIZE)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'notifications': page_obj.object_list,
        'unread_count': Notification.objects.unread_count(request.user),
        'selected_type': notification_type or '',
        'notification_types': Notification.NotificationType.choices,
    }
    return render(request, 'notify/notifications.html', context)


# ====================================================================
# POLLING API
# ====================================================================

@login_required(login_url='/customer/signin/')
@require_GET
def unread_count(request):
    """Return the current unread notification count for the user"""
    count = Notification.objects.unread_count(request.user)
    return JsonResponse({'unread_count': count})


@login_required(login_url='/customer/signin/')
@require_GET
def latest(request):
    """Return the most recent notifications for the user (for a dropdown/toast poller)"""
    notifications = Notification.objects.for_user(request.user)[:LATEST_LIMIT]
    return JsonResponse({
        'unread_count': Notification.objects.unread_count(request.user),
        'results': [_serialize_notification(n) for n in notifications],
    })


# ====================================================================
# MUTATIONS
# ====================================================================

@login_required(login_url='/customer/signin/')
@require_POST
def mark_read(request, notification_uuid):
    """Mark a single notification (owned by the requesting user) as read"""
    notification = get_object_or_404(
        Notification.objects.for_user(request.user),
        uuid=notification_uuid,
    )
    notification.mark_read()

    return JsonResponse({
        'success': True,
        'uuid': str(notification.uuid),
        'unread_count': Notification.objects.unread_count(request.user),
    })


@login_required(login_url='/customer/signin/')
@require_POST
def mark_all_read(request):
    """Mark all of the requesting user's unread notifications as read"""
    updated = Notification.objects.mark_all_read(request.user)

    return JsonResponse({
        'success': True,
        'marked_read': updated,
        'unread_count': Notification.objects.unread_count(request.user),
    })