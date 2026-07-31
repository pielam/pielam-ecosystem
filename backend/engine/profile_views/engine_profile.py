# engine/profile_views/engine_profile.py

# Standard Library
import json
import logging
import threading

# Django
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

# Django REST Framework
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

# Local Apps
from apps.customer.models.profile_info import ProfileInfo
from megamind.models.connected_service import ConnectedService
from megamind.services.scraper import scrape_url
from megamind.utils.service_fetcher import fetch_service_data
from megamind.utils.feed_cache import refresh_feed_cache
from megamind.utils.media_info import normalize_images, normalize_links
from megamind.utils.video_info import get_video_info_cached

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Serializers
# ═══════════════════════════════════════════════════════════════════════════

class ConnectedServiceSerializer(serializers.ModelSerializer):
    """Read serializer — all fields including scraped data."""

    class Meta:
        model = ConnectedService
        fields = [
            "id", "service_name", "service_url", "service_type",
            "status", "is_connected",
            "og_title", "og_description", "og_thumbnail", "og_site_name", "og_type",
            "extracted_images", "extracted_videos", "extracted_links", "extracted_text",
            "fetch_status", "fetch_error", "last_fetch_time",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "og_title", "og_description", "og_thumbnail", "og_site_name", "og_type",
            "extracted_images", "extracted_videos", "extracted_links", "extracted_text",
            "fetch_status", "fetch_error", "last_fetch_time", "last_fetched_data",
            "created_at", "updated_at",
        ]


class ConnectedServiceWriteSerializer(serializers.ModelSerializer):
    """Write serializer — only user-editable fields."""

    class Meta:
        model = ConnectedService
        fields = [
            "service_name", "service_url", "service_type",
            "status", "is_connected", "api_key", "auth_token", "profile",
        ]


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_videos_for_display(raw_videos, limit: int = None) -> list:
    """
    Mirrors megamind.utils.feed_cache._normalize_videos — kept in sync
    on purpose so a video looks and plays identically on the profile
    page as it does in the public engine feed, regardless of which
    producer (scraper.py / service_fetcher.py) wrote extracted_videos.

    Accepts scraper dicts like {"url": ..., "type": "video/*"|"embed"},
    bare URL strings, or already-normalized video_info dicts — anything
    get_video_info_cached() can resolve a URL out of.
    """
    normalized = []
    for v in raw_videos or []:
        if isinstance(v, dict):
            url = v.get('url') or v.get('embed_url') or v.get('src')
        else:
            url = v
        if not url:
            continue
        info = get_video_info_cached(url)
        if info:
            normalized.append(info)
        if limit and len(normalized) >= limit:
            break
    return normalized


def _serialize_service(service: ConnectedService) -> dict:
    """
    Serialize a service to a dict.
    Reads new flat fields first, falls back to last_fetched_data for
    backwards compatibility.

    Images/links/videos are run through the same shared normalizers
    used by the public engine feed (megamind.utils.media_info,
    megamind.utils.video_info) instead of ad-hoc per-view coercion, so
    this page's media renders identically to the feed regardless of
    which producer (scraper.py vs service_fetcher.py) wrote the raw
    extracted_* fields, and videos come back embed-ready
    (platform/embed_url/watch_url/thumbnail/type) rather than the raw
    {"url", "type"} shape.
    """
    og_title       = service.og_title       or ""
    og_description = service.og_description or ""
    og_thumbnail   = service.og_thumbnail   or ""
    og_site_name   = service.og_site_name   or ""
    og_type        = service.og_type        or ""
    text           = service.extracted_text or ""

    raw = service.last_fetched_data or {}
    if raw and not og_title:
        og_title       = raw.get("title")       or raw.get("og_title")       or ""
        og_description = raw.get("description") or raw.get("og_description") or ""
        og_thumbnail   = (raw.get("og_image")   or raw.get("og_thumbnail")
                          or raw.get("thumbnail") or "")
        og_site_name   = raw.get("site_name")   or raw.get("og_site_name")   or ""
        og_type        = raw.get("og_type")     or raw.get("type")           or ""

    # Prefer the flat extracted_* fields; fall back to the raw scrape
    # dump only when the flat field is empty (older rows / partial writes).
    images_source = service.extracted_images or (raw.get("images") if raw else []) or []
    videos_source = service.extracted_videos or (raw.get("videos") if raw else []) or []
    links_source  = service.extracted_links  or (raw.get("links")  if raw else []) or []

    if not text and raw:
        text = (raw.get("full_content") or raw.get("text_content")
                or raw.get("content")   or raw.get("text") or "")

    images = normalize_images(images_source)
    videos = _normalize_videos_for_display(videos_source)
    links  = normalize_links(links_source)

    return {
        "id":               service.id,
        "service_name":     service.service_name,
        "service_url":      service.service_url,
        "service_type":     service.service_type,
        "status":           service.status,
        "is_connected":     service.is_connected,
        "og_title":         og_title,
        "og_description":   og_description,
        "og_thumbnail":     og_thumbnail,
        "og_site_name":     og_site_name,
        "og_type":          og_type,
        "extracted_images": images,
        "extracted_videos": videos,
        "extracted_links":  links,
        "extracted_text":   text,
        "fetch_status":     service.fetch_status,
        "fetch_error":      service.fetch_error,
        "last_fetch_time":  service.last_fetch_time.isoformat() if service.last_fetch_time else None,
        "created_at":       service.created_at.isoformat() if service.created_at else None,
        "updated_at":       service.updated_at.isoformat() if service.updated_at else None,
    }


def _should_fetch(service: ConnectedService, max_age_seconds: int = 3600) -> bool:
    """
    Stale if never fetched, or older than max_age.
    Back off for 10 min after an error so timeouts don't hammer the same host.
    """
    if service.last_fetch_time is None:
        return True
    age = (timezone.now() - service.last_fetch_time).total_seconds()
    if service.fetch_status == 'error':
        return age > 600   # 10-minute cooldown after errors / timeouts
    return age > max_age_seconds


def _trigger_background_refresh(service_id: int, user_id: int) -> None:
    """
    Fire-and-forget background thread.
    Replace with Celery task if available:
        refresh_service_task.delay(service_id)
    """
    try:
        svc = ConnectedService.objects.get(pk=service_id)
        fetch_service_data(svc)
    except Exception:
        logger.exception(
            "Background refresh failed service_id=%s uid=%s", service_id, user_id
        )


# ═══════════════════════════════════════════════════════════════════════════
# Main View
# ═══════════════════════════════════════════════════════════════════════════

@login_required(login_url='/customer/signin/')
@ensure_csrf_cookie
def CrawlEngineView(request):
    """
    Enterprise-level identity view with data validation and comprehensive user data.
    """
    user = request.user
    profile_info = get_object_or_404(ProfileInfo, user=user)

    # ── Security Alerts ───────────────────────────────────────────────────
    security_alerts = []
    if user.failed_login_attempts > 0:
        if user.failed_login_attempts >= 5:
            alert_type = 'danger'
        elif user.failed_login_attempts >= 3:
            alert_type = 'warning'
        else:
            alert_type = 'info'
        security_alerts.append({
            'type': alert_type,
            'icon': 'exclamation-triangle',
            'message': f"{user.failed_login_attempts} failed login attempt(s) detected",
        })

    # ── Display Name & Initials ───────────────────────────────────────────
    if '@' in user.email_or_phone:
        display_name = user.email_or_phone.split('@')[0].replace('.', ' ').replace('_', ' ').title()
    else:
        display_name = user.email_or_phone
    initials = display_name[0].upper() if display_name else 'U'

    # ── Badges ────────────────────────────────────────────────────────────
    badges = []
    badges.sort(key=lambda x: x['priority'])

    # ── 2FA ──────────────────────────────────────────────────────────────
    is_2fa_enabled = getattr(user, 'is_2fa_enabled', False) or request.session.get('2fa_enabled', False)

    # ── User Data ─────────────────────────────────────────────────────────
    user_data = {
        'email_or_phone':   user.email_or_phone,
        'role':             user.get_role_display(),
        'role_raw':         user.role,
        'is_staff':         user.is_staff,
        'is_superuser':     user.is_superuser,
        'is_active':        user.is_active,
        'deleted_at':       user.deleted_at,
        'locked_until':     user.locked_until,
        'groups':           user.groups.all(),
        'user_permissions': user.user_permissions.all(),
    }

    stats = {
        'failed_attempts': user.failed_login_attempts,
        'is_2fa_enabled':  is_2fa_enabled,
    }

    # ── Services ──────────────────────────────────────────────────────────
    # Never fetch inside a view; serve stale (already-serialized) data
    # and trigger a background refresh only when stale.
    services = ConnectedService.objects.filter(user=user)
    connected_services = services.filter(is_connected=True)

    services_data = []
    for service in services:
        services_data.append(_serialize_service(service))

        # Schedule a background refresh only if stale — never block the view
        if service.is_connected and _should_fetch(service):
            t = threading.Thread(
                target=_trigger_background_refresh,
                args=(service.pk, user.pk),
                daemon=True,
            )
            t.start()

    # ── Context ───────────────────────────────────────────────────────────
    context = {
        'user':               user,
        'profile_info':       profile_info,
        'user_data':          user_data,
        'stats':              stats,
        'badges':             badges,
        'security_alerts':    security_alerts,
        'page_title':         'Identity Profile',
        'notification_count': 0,
        'services':           services,
        'connected_services': json.dumps(services_data, ensure_ascii=False),
        'total_services':     connected_services.count(),
    }

    return render(request, "personal/engine_profile.html", context)


# ═══════════════════════════════════════════════════════════════════════════
# Service API Views
# ═══════════════════════════════════════════════════════════════════════════

@login_required(login_url='/customer/signin/')
@require_http_methods(["POST"])
def add_service(request):
    try:
        data = json.loads(request.body)
        service_name = data.get('service_name')
        service_url  = data.get('service_url')
        # 'other' isn't a valid ConnectedService.SERVICE_TYPES choice —
        # fall back to the model's own default ('product') instead of
        # silently writing an invalid value that later chokes anything
        # keyed on service_type (e.g. video_info.FALLBACK_VIDEOS pools,
        # feed_cache attribution).
        service_type = data.get('service_type') or 'product'

        if not service_name or not service_url:
            return JsonResponse({'success': False, 'error': 'Service name and URL are required'}, status=400)

        valid_types = {choice[0] for choice in ConnectedService.SERVICE_TYPES}
        if service_type not in valid_types:
            return JsonResponse(
                {'success': False, 'error': f"Invalid service_type '{service_type}'"},
                status=400,
            )

        service = ConnectedService.objects.create(
            user=request.user,
            service_name=service_name,
            service_url=service_url,
            service_type=service_type,
            is_connected=False,
            status='private',
        )

        return JsonResponse({
            'success': True,
            'service': {
                'id':           service.id,
                'service_name': service.service_name,
                'service_url':  service.service_url,
                'service_type': service.service_type,
                'is_connected': service.is_connected,
                'status':       service.status,
            },
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(["POST"])
def toggle_service_connection(request, service_id):
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user)
        service.is_connected = not service.is_connected
        service.save()

        if service.is_connected:
            fetch_service_data(service)

        return JsonResponse({
            'success':      True,
            'is_connected': service.is_connected,
            'status':       service.status,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(["DELETE"])
def delete_service(request, service_id):
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user)
        service.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(["POST"])
def refresh_service_data(request, service_id):
    """POST engine/api/services/<id>/refresh/ — refresh one service."""
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user)

        if not service.is_connected:
            return JsonResponse({'success': False, 'error': 'Service is not connected'}, status=400)

        fetch_service_data(service)
        service.refresh_from_db()
        return JsonResponse({'success': True, **_serialize_service(service)})

    except Exception as e:
        logger.exception("refresh_service_data failed for service_id=%s", service_id)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='/customer/signin/')
@require_http_methods(["POST"])
def batch_refresh_services(request):
    """POST engine/api/services/batch-refresh/ — refresh multiple services."""
    try:
        body = json.loads(request.body or '{}')
        service_ids = body.get('service_ids', [])

        qs = ConnectedService.objects.filter(user=request.user, is_connected=True)
        if service_ids:
            qs = qs.filter(id__in=service_ids)

        results = []
        for service in qs:
            fetch_service_data(service)
            service.refresh_from_db()
            results.append(_serialize_service(service))

        return JsonResponse({
            'success':    True,
            'results':    results,
            'total':      len(results),
            'successful': sum(1 for r in results if r['fetch_status'] == 'success'),
        })

    except Exception as e:
        logger.exception("batch_refresh_services failed")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='/customer/signin/')
def get_service_media(request, service_id):
    """GET engine/api/services/<id>/media/ — return cached scraped media."""
    try:
        service = get_object_or_404(ConnectedService, id=service_id, user=request.user)

        if service.fetch_status != 'success':
            return JsonResponse({'success': False, 'error': 'No data available'}, status=404)

        return JsonResponse({'success': True, **_serialize_service(service)})

    except Exception as e:
        logger.exception("get_service_media failed for service_id=%s", service_id)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ═══════════════════════════════════════════════════════════════════════════
# DRF ViewSet  (mounted via megamind/urls.py → /api/connected-services/)
# ═══════════════════════════════════════════════════════════════════════════

class ConnectedServiceViewSet(ModelViewSet):
    """
    GET    /api/connected-services/              → list
    POST   /api/connected-services/              → create
    GET    /api/connected-services/{id}/         → retrieve
    PUT    /api/connected-services/{id}/         → update
    PATCH  /api/connected-services/{id}/         → partial_update
    DELETE /api/connected-services/{id}/         → destroy
    POST   /api/connected-services/{id}/fetch/   → scrape & persist
    POST   /api/connected-services/{id}/refresh/ → alias of fetch/
    GET    /api/connected-services/{id}/preview/ → cached data only
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ConnectedService.objects.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return ConnectedServiceWriteSerializer
        return ConnectedServiceSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def _do_scrape(self, service: ConnectedService):
        logger.info("Scraping service_id=%s url=%s", service.id, service.service_url)

        result = scrape_url(
            url=service.service_url,
            api_key=service.api_key,
            auth_token=service.auth_token,
        )

        if result["error"]:
            logger.warning(
                "Scrape failed for service_id=%s url=%s error=%s",
                service.id, service.service_url, result["error"],
            )
            service.fetch_status = "error"
            service.fetch_error  = result["error"]
            service.save(update_fields=["fetch_status", "fetch_error", "updated_at"])

            return False, Response(
                {
                    "success":      False,
                    "fetch_status": "error",
                    "fetch_error":  result["error"],
                    "detail":       "Could not fetch data from this URL. The site may block automated access.",
                },
                status=status.HTTP_200_OK,
            )

        og = result["og"]
        service.og_title          = og.get("title")       or ""
        service.og_description    = og.get("description") or ""
        service.og_thumbnail      = og.get("thumbnail")   or ""
        service.og_site_name      = og.get("site_name")   or ""
        service.og_type           = og.get("type")        or ""
        service.extracted_images  = result["images"]
        service.extracted_videos  = result["videos"]
        service.extracted_links   = result["links"]
        service.extracted_text    = result["text"]
        service.last_fetched_data = result["raw"]
        service.last_fetch_time   = timezone.now()
        service.fetch_status      = "success"
        service.fetch_error       = None

        service.save(update_fields=[
            "og_title", "og_description", "og_thumbnail", "og_site_name", "og_type",
            "extracted_images", "extracted_videos", "extracted_links", "extracted_text",
            "last_fetched_data", "last_fetch_time", "fetch_status", "fetch_error",
            "updated_at",
        ])

        # This is the other producer path that writes extracted_images/
        # extracted_videos/extracted_links (alongside service_fetcher.
        # fetch_service_data, which already does this). Without this
        # call, any service refreshed via /fetch/ or /refresh/ on the
        # DRF endpoint would leave cached_feed_payload stale until the
        # next fetch_service_data-driven update — same "never let
        # feed-cache bookkeeping fail the actual fetch" contract as
        # fetch_service_data.
        try:
            refresh_feed_cache(service)
        except Exception:
            logger.exception(
                "refresh_feed_cache failed for service %s after successful scrape",
                service.pk,
            )

        return True, Response(
            {"success": True, **ConnectedServiceSerializer(service).data},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="fetch")
    def fetch(self, request, pk=None):
        """Scrape service_url and persist all extracted fields."""
        _, response = self._do_scrape(self.get_object())
        return response

    @action(detail=True, methods=["post"], url_path="refresh")
    def refresh(self, request, pk=None):
        """Dashboard refresh — identical to fetch/ but at /refresh/ path."""
        _, response = self._do_scrape(self.get_object())
        return response

    @action(detail=True, methods=["get"], url_path="preview")
    def preview(self, request, pk=None):
        """Return cached scraped content without triggering a new scrape."""
        service = self.get_object()

        if service.fetch_status != "success":
            return Response(
                {
                    "detail":       "No data fetched yet. POST to /fetch/ first.",
                    "fetch_status": service.fetch_status,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "id":              service.id,
                "service_name":    service.service_name,
                "service_url":     service.service_url,
                "last_fetch_time": service.last_fetch_time,
                "og": {
                    "title":       service.og_title,
                    "description": service.og_description,
                    "thumbnail":   service.og_thumbnail,
                    "site_name":   service.og_site_name,
                    "type":        service.og_type,
                },
                "images": service.extracted_images,
                "videos": service.extracted_videos,
                "links":  service.extracted_links,
                "text":   service.extracted_text,
            },
            status=status.HTTP_200_OK,
        )