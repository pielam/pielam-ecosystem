# megamind/services/visit_logger.py

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone as dj_timezone

from megamind.models.visit_log import DiscoveryVisitLog

logger = logging.getLogger(__name__)

# Optional dependencies — degrade gracefully if not installed / not configured.
try:
    from user_agents import parse as parse_user_agent
except ImportError:  # pragma: no cover
    parse_user_agent = None

try:
    import geoip2.database
    _geoip_city_reader = None
    _geoip_asn_reader = None
    _GEOIP_CITY_DB = getattr(settings, 'GEOIP_CITY_DB_PATH', None)
    _GEOIP_ASN_DB = getattr(settings, 'GEOIP_ASN_DB_PATH', None)
except ImportError:  # pragma: no cover
    geoip2 = None
    _GEOIP_CITY_DB = None
    _GEOIP_ASN_DB = None


DEFAULT_RETENTION_DAYS = getattr(settings, 'DISCOVERY_VISIT_LOG_RETENTION_DAYS', 395)  # ~13 months


def _get_client_ip(request) -> str | None:
    """
    Best-effort client IP resolution. X-Forwarded-For is client-supplied
    and can be spoofed unless your load balancer/proxy strips and
    re-sets it — trust it only as far as your infra guarantees that.
    """
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _get_ua_object(ua_string: str):
    if not ua_string or parse_user_agent is None:
        return None
    try:
        return parse_user_agent(ua_string)
    except Exception:
        logger.debug('Failed to parse user agent: %s', ua_string, exc_info=True)
        return None


def _classify_device_type(ua) -> str:
    if ua is None:
        return DiscoveryVisitLog.DeviceType.UNKNOWN
    if ua.is_bot:
        return DiscoveryVisitLog.DeviceType.BOT
    if ua.is_mobile:
        return DiscoveryVisitLog.DeviceType.MOBILE
    if ua.is_tablet:
        return DiscoveryVisitLog.DeviceType.TABLET
    if ua.is_pc:
        return DiscoveryVisitLog.DeviceType.DESKTOP
    return DiscoveryVisitLog.DeviceType.UNKNOWN


def _geoip_lookup(ip: str | None) -> dict[str, Any]:
    """
    Returns geo/ASN fields if geoip2 + MaxMind db files are configured,
    otherwise an empty dict. Never raises — a lookup failure should
    never break the calling view.
    """
    result: dict[str, Any] = {}
    if not ip or geoip2 is None or not _GEOIP_CITY_DB:
        return result

    global _geoip_city_reader, _geoip_asn_reader
    try:
        if _geoip_city_reader is None:
            _geoip_city_reader = geoip2.database.Reader(_GEOIP_CITY_DB)
        city_resp = _geoip_city_reader.city(ip)
        result.update(
            country=city_resp.country.iso_code or '',
            region=(city_resp.subdivisions.most_specific.name or '') if city_resp.subdivisions else '',
            city=city_resp.city.name or '',
            postal_code=city_resp.postal.code or '',
            latitude=city_resp.location.latitude,
            longitude=city_resp.location.longitude,
        )
    except Exception:
        logger.debug('GeoIP city lookup failed for %s', ip, exc_info=True)

    if _GEOIP_ASN_DB:
        try:
            if _geoip_asn_reader is None:
                _geoip_asn_reader = geoip2.database.Reader(_GEOIP_ASN_DB)
            asn_resp = _geoip_asn_reader.asn(ip)
            result.update(
                isp=asn_resp.autonomous_system_organization or '',
                asn=str(asn_resp.autonomous_system_number or ''),
            )
        except Exception:
            logger.debug('GeoIP ASN lookup failed for %s', ip, exc_info=True)

    return result


def _get_referrer_domain(referrer: str) -> str:
    if not referrer:
        return ''
    try:
        return urlparse(referrer).netloc
    except Exception:
        return ''


def _get_utm_params(request) -> dict[str, str]:
    get = request.GET
    return {
        'utm_source': get.get('utm_source', '')[:100],
        'utm_medium': get.get('utm_medium', '')[:100],
        'utm_campaign': get.get('utm_campaign', '')[:100],
        'utm_term': get.get('utm_term', '')[:100],
        'utm_content': get.get('utm_content', '')[:100],
    }


def _get_click_id(request) -> str:
    for key in ('gclid', 'fbclid', 'msclkid', 'ttclid'):
        value = request.GET.get(key)
        if value:
            return value[:255]
    return ''


def _resolve_session_flags(session_key: str, device_fingerprint: str, user_id) -> dict[str, Any]:
    """
    Determine first-visit / returning-visitor / sequence-number flags
    with a couple of small read queries. Kept cheap and best-effort —
    a failure here should degrade to conservative defaults, never
    block the write.
    """
    flags = {
        'is_first_visit': True,
        'is_returning_visitor': False,
        'visit_sequence_number': 1,
    }
    try:
        if session_key:
            session_count = DiscoveryVisitLog.objects.filter(session_key=session_key).count()
            flags['is_first_visit'] = session_count == 0
            flags['visit_sequence_number'] = session_count + 1

        identity_filter = None
        if user_id:
            identity_filter = {'user_id': user_id}
        elif device_fingerprint:
            identity_filter = {'device_fingerprint': device_fingerprint}

        if identity_filter:
            flags['is_returning_visitor'] = DiscoveryVisitLog.objects.filter(**identity_filter).exists()
    except Exception:
        logger.debug('Failed to resolve session flags', exc_info=True)

    return flags


def record_discovery_visit(
    request,
    *,
    product=None,
    search_query: str = '',
    filter_slug: str = '',
    sort_by: str = '',
    page_number: int = 1,
    results_count: int | None = None,
    server_response_time_ms: int | None = None,
    device_fingerprint: str = '',
    consent_given: bool | None = None,
    consent_categories: list | None = None,
    extra_fields: dict | None = None,
) -> DiscoveryVisitLog | None:
    """
    Build and persist a DiscoveryVisitLog row from a Django request.
    Call this from any view that should be tracked — pass whatever
    view-specific context you have (search_query, filter_slug, product,
    etc); everything else (identity, network, device, attribution) is
    derived from the request automatically.

    Usage in a view:

        from megamind.services.visit_logger import record_discovery_visit

        def discovery_view(request):
            results = run_search(request)
            record_discovery_visit(
                request,
                search_query=request.GET.get('q', ''),
                filter_slug=request.GET.get('filter', ''),
                sort_by=request.GET.get('sort', ''),
                page_number=int(request.GET.get('page', 1)),
                results_count=results.count(),
            )
            return render(request, 'discovery.html', {'results': results})

        def product_detail_view(request, slug):
            product = get_product(slug)
            record_discovery_visit(request, product=product)
            ...

    Fails safe: logging errors are caught and logged, never raised,
    so a tracking failure can never break the page the user is on.
    For high-traffic views, prefer calling this via a background task
    (e.g. record_discovery_visit_task.delay(...)) instead of inline,
    to keep this off the request/response critical path.
    """
    try:
        ua_string = request.META.get('HTTP_USER_AGENT', '')[:512]
        ua = _get_ua_object(ua_string)

        ip = _get_client_ip(request)
        geo = _geoip_lookup(ip)

        referrer = request.META.get('HTTP_REFERER', '')[:1000]
        session_key = getattr(request.session, 'session_key', '') or ''

        user = request.user if getattr(request, 'user', None) and request.user.is_authenticated else None
        role_at_visit = getattr(user, 'role', None) if user else None
        account_tier_at_visit = getattr(user, 'account_tier', '') if user else ''

        flags = _resolve_session_flags(
            session_key=session_key,
            device_fingerprint=device_fingerprint,
            user_id=user.id if user else None,
        )

        retention_expires_at = dj_timezone.now() + timedelta(days=DEFAULT_RETENTION_DAYS)

        fields: dict[str, Any] = dict(
            # Identity / correlation
            request_id=request.META.get('HTTP_X_REQUEST_ID', '')[:64],
            trace_id=request.META.get('HTTP_TRACEPARENT', '')[:64],
            correlation_id=request.META.get('HTTP_X_CORRELATION_ID', '')[:64],

            # Who
            user=user,
            role_at_visit=role_at_visit,
            is_authenticated=user is not None,
            account_tier_at_visit=account_tier_at_visit or '',
            session_key=session_key,
            device_fingerprint=device_fingerprint[:64],
            product=product,
            **flags,

            # Network / geo
            ip_address=ip,
            connection_type=DiscoveryVisitLog.ConnectionType.UNKNOWN,
            **{k: v for k, v in geo.items()},

            # Client / device
            user_agent=ua_string,
            device_type=_classify_device_type(ua),
            browser_name=(ua.browser.family if ua else '') or '',
            browser_version=(ua.browser.version_string if ua else '') or '',
            os_name=(ua.os.family if ua else '') or '',
            os_version=(ua.os.version_string if ua else '') or '',
            accept_language=request.META.get('HTTP_ACCEPT_LANGUAGE', '')[:255],
            is_likely_bot=bool(ua.is_bot) if ua else False,

            # Security signals (populate from your WAF/CDN middleware if available)
            bot_score=getattr(request, 'bot_score', None),
            waf_action=getattr(request, 'waf_action', '') or '',
            threat_score=getattr(request, 'threat_score', None),

            # Acquisition / attribution
            referrer=referrer,
            referrer_domain=_get_referrer_domain(referrer),
            **_get_utm_params(request),
            click_id=_get_click_id(request),

            # Experimentation / product context
            ab_test_variant=getattr(request, 'ab_test_variant', '') or '',
            feature_flags=getattr(request, 'active_feature_flags', {}) or {},
            client_app_version=request.META.get('HTTP_X_APP_VERSION', '')[:32],
            environment=getattr(settings, 'ENVIRONMENT', ''),

            # What they were looking at
            query_string=request.META.get('QUERY_STRING', '')[:1000],
            search_query=search_query[:255],
            filter_slug=filter_slug[:255],
            sort_by=sort_by[:50],
            page_number=page_number or 1,
            results_count=results_count,

            # Performance
            server_response_time_ms=server_response_time_ms,

            # Consent / governance
            consent_given=consent_given,
            consent_categories=consent_categories or [],
            gdpr_applicable=getattr(request, 'gdpr_applicable', False),
            ccpa_applicable=getattr(request, 'ccpa_applicable', False),
            retention_expires_at=retention_expires_at,
        )

        if extra_fields:
            fields.update(extra_fields)

        return DiscoveryVisitLog.objects.create(**fields)

    except Exception:
        # Never let telemetry break the page the visitor is on.
        logger.exception('Failed to record discovery visit')
        return None