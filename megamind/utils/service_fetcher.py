# megamind/utils/service_fetcher.py

"""
Fetches a ConnectedService.service_url and, for content types beyond
plain HTML (images/video/audio/documents/JSON/plain text), returns
type-specific metadata. For HTML it does deeper structural extraction
than scraper.py — paragraphs, lists, tables, iframes as their own
list — on top of the same universal metadata scraper.py extracts (OG,
Twitter Card, page meta, icons/feeds, robots/indexability, article
metadata, hreflang alternates, JSON-LD sameAs/breadcrumb, commerce).

Contract: fetch_service_data(service, worker_id=None) takes a
*ConnectedService instance*, mutates it in place and persists it via
service.mark_fetch_success()/mark_fetch_error() (NOT a raw
service.save() — see "Enterprise crawling guards" below), and returns
True/False. This is a deliberately different contract from
megamind/utils/service_fetcher.py's fetch_service_data(url),
which takes a bare URL and returns a plain result dict — that one
backs apps.customer.services.connect_url's simpler "scrape a URL,
build a ConnectedService from scratch" flow, this one backs a richer
"preview/refresh an existing service, including non-HTML content"
flow. Do not merge the two; they serve different callers with
different inputs and outputs. (These two files used to be
accidentally concatenated together in one document — if you're
diffing against old history and see this module's docstring followed
by a second `import requests` / `USER_AGENT = "Mozilla/5.0
(compatible; ConnectedServiceBot/1.0...)"` block, that was a copy-paste
artifact, not intentional — that code belongs in the other file.)

Resilience: this used to be all-or-nothing — a single failing
extractor (a malformed table, a bad JSON-LD block) raised, and
fetch_service_data() discarded EVERYTHING, including the 90% of the
page that parsed fine, and marked the service 'error'. It's now built
the same way megamind/services/scraper.py is: every extraction step is
wrapped so one bad field can't take the rest down with it, and
fetch_status is only 'error' when the URL couldn't be reached at all
(see resilient_get). Partial failures show up as `warnings` instead.

Enterprise crawling guards, applied BEFORE any network request (same
guards scraper.scrape_and_store applies — a service doesn't get weaker
protection depending on which of the two fetchers happens to run it):
  1. SSRF re-validation via validate_crawl_url() — service.service_url
     is re-checked at fetch time, not just at model-clean time, since
     DNS can change between when a row was created and when a worker
     gets around to it. This was previously MISSING from this file
     entirely: it called resilient_get() directly on service_url with
     no validation, unlike connect_url.py and scraper.py's
     scrape_and_store, which both validate first.
  2. Domain policy — a manually blocked domain or a robots.txt
     `disallow_all` short-circuits before any request; a domain over
     its rate-limit window is deferred (fetch_status='error',
     RATE_LIMITED) instead of fetched.
Persistence now goes through service.mark_fetch_success()/
mark_fetch_error() instead of hand-setting fetch_status/fetch_error/
last_fetch_time and calling save() directly. This was also previously
missing: bypassing those methods meant no CrawlAttempt audit row, no
circuit-breaker/consecutive-failure tracking, no retry/backoff
scheduling, no next_crawl_at scheduling, and no lock release if this
ran against a row a worker had locked via claim_next_for_crawl — every
enterprise feature the model provides was silently skipped for
anything fetched through this path.

Shared logic: og/twitter/page-meta/icons+feeds/robots/article-meta/
alternate-languages/sameAs/breadcrumb/canonical/favicon/author/
published_time/headings/product_info extraction — including the
favicon exact-rel-token-match fix and the content-hashing helper — now
live once in scraper.py and are imported here rather than duplicated.
This file keeps only the extraction it does that scraper.py doesn't:
paragraphs/lists/tables/iframes and the non-HTML content-type
handlers.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

from django.conf import settings
from django.utils import timezone

from bs4 import BeautifulSoup

from megamind.utils.feed_cache import refresh_feed_cache
from megamind.utils.video_info import get_video_info_cached
from megamind.utils.media_info import is_probably_tracking_pixel
from megamind.utils.resilient_fetch import resilient_get
from megamind.utils.browser_render import should_attempt_render, render_with_browser
from megamind.utils.http_client import (
    decode_text,
    DEFAULT_TIMEOUT, DEFAULT_MAX_RETRIES, DEFAULT_BACKOFF_FACTOR,
    DEFAULT_MAX_CONTENT_BYTES, DEFAULT_USER_AGENT,
)
from megamind.models.connected_service import (
    CrawlAttempt,
    DomainCrawlPolicy,
    UnsafeCrawlURLError,
    validate_crawl_url,
)
# Reused rather than re-implemented — see module docstring. These are
# the same functions scraper.py uses, including the favicon exact-
# rel-token-match fix, the JSON-LD-first product/OG extraction, and
# the model-field coercion helpers that keep untrusted scraped strings
# from blowing past CharField max_lengths / Decimal field limits.
from megamind.services.scraper import (
    _safe,
    _backfill,
    _extract_og,
    _extract_twitter,
    _extract_page_meta,
    _extract_icons_and_feeds,
    _extract_robots_meta,
    _extract_article_meta,
    _extract_alternate_languages,
    _extract_same_as,
    _extract_breadcrumb,
    _extract_json_ld,
    _extract_canonical,
    _extract_favicon,
    _extract_author,
    _extract_published_time,
    _extract_headings,
    _extract_product_info,
    _guess_encoding,
    _hash_content,
    _truncate,
    _to_decimal,
    _to_positive_int,
)

logger = logging.getLogger(__name__)

# Layered on top of the generic SCRAPER_* settings in http_client.py —
# same pattern video_info.py uses for its per-platform overrides.
FETCH_TIMEOUT            = getattr(settings, 'SERVICE_FETCH_TIMEOUT', DEFAULT_TIMEOUT)
FETCH_MAX_RETRIES        = getattr(settings, 'SERVICE_FETCH_MAX_RETRIES', DEFAULT_MAX_RETRIES)
FETCH_BACKOFF_FACTOR     = getattr(settings, 'SERVICE_FETCH_BACKOFF_FACTOR', DEFAULT_BACKOFF_FACTOR)
FETCH_MAX_CONTENT_BYTES  = getattr(settings, 'SERVICE_FETCH_MAX_CONTENT_BYTES', DEFAULT_MAX_CONTENT_BYTES)

# A data: URI embedding the full image is only worth returning inline
# when it's small — large ones bloat last_fetched_data for no benefit
# since the caller already has `data` (the full base64) to work with.
_INLINE_PREVIEW_MAX_BYTES = 200 * 1024

# RFC 5987 extended notation (filename*=UTF-8''name.pdf) takes
# precedence over plain filename= when both are present, per RFC 6266.
_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*[^']*''([^;]+)", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)


class ServiceDataFetcher:
    """Enhanced fetcher that handles multiple content types"""

    # frozensets: fetch() checks membership against these on every
    # single fetch, so O(1) lookup beats an O(n) list scan here.
    SUPPORTED_IMAGE_TYPES = frozenset({'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/svg+xml'})
    SUPPORTED_VIDEO_TYPES = frozenset({'video/mp4', 'video/webm', 'video/ogg', 'video/quicktime'})
    SUPPORTED_AUDIO_TYPES = frozenset({'audio/mpeg', 'audio/wav', 'audio/ogg', 'audio/mp4', 'audio/webm'})
    SUPPORTED_DOCUMENT_TYPES = frozenset({
        'application/pdf', 'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    })

    def __init__(self, service):
        self.service = service
        self.timeout = FETCH_TIMEOUT
        self.max_retries = FETCH_MAX_RETRIES
        self.backoff_factor = FETCH_BACKOFF_FACTOR
        self.max_content_bytes = FETCH_MAX_CONTENT_BYTES
        self.headers: Dict[str, str] = {
            'User-Agent': getattr(settings, 'SERVICE_FETCH_USER_AGENT', DEFAULT_USER_AGENT),
        }

        if service.api_key:
            self.headers['Authorization'] = f'Bearer {service.api_key}'
        if service.auth_token:
            self.headers['X-Auth-Token'] = service.auth_token

    def fetch(self) -> Dict[str, Any]:
        """
        Main fetch method that routes to the appropriate handler.

        Uses resilient_get (same helper scraper.py uses) instead of a
        single build_session().get() + raise_for_status: a page that
        403s the default UA, or that times out once, used to kill the
        whole fetch immediately. Now it gets the same multi-UA / SSL-
        fallback retry pass scraper.py gets, so both pipelines succeed
        or fail on the same set of URLs instead of one being strictly
        worse than the other.
        """
        raw_bytes, resp_headers, final_url, fetch_error = resilient_get(
            self.service.service_url,
            self.headers,
            timeout=self.timeout,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
            max_content_bytes=self.max_content_bytes,
        )
        if raw_bytes is None:
            raise Exception(fetch_error or "Request failed for unknown reason")

        content_type = resp_headers.get('Content-Type', '').split(';')[0].strip()
        content = raw_bytes

        if content_type in self.SUPPORTED_IMAGE_TYPES:
            return self._handle_image(content, content_type)
        elif content_type in self.SUPPORTED_VIDEO_TYPES:
            return self._handle_video(content, content_type)
        elif content_type in self.SUPPORTED_AUDIO_TYPES:
            return self._handle_audio(content, content_type)
        elif content_type in self.SUPPORTED_DOCUMENT_TYPES:
            return self._handle_document(resp_headers, content, content_type)
        elif 'application/json' in content_type:
            return self._handle_json(content)
        elif 'text/html' in content_type or 'application/xhtml' in content_type:
            return self._handle_html(content, resp_headers, final_url)
        elif 'text/plain' in content_type:
            return self._handle_text(content, resp_headers)
        else:
            return self._handle_generic(content, content_type)

    def _handle_image(self, content: bytes, content_type: str) -> Dict[str, Any]:
        image_data = base64.b64encode(content).decode('utf-8')
        size = len(content)

        # Only ever embed a COMPLETE, valid data URI. A byte-sliced
        # base64 string plus "..." is not decodable — a consumer
        # trying to render it as an <img src> just gets a broken
        # image, which is worse than omitting preview_url entirely.
        preview_url = f"data:{content_type};base64,{image_data}" if size <= _INLINE_PREVIEW_MAX_BYTES else None

        result = {
            'type': 'image',
            'content_type': content_type,
            'size': size,
            'size_human': self._format_bytes(size),
            'data': image_data,
            'dimensions': self._get_image_dimensions(content),
            'fetched_at': timezone.now().isoformat(),
        }
        if preview_url is not None:
            result['preview_url'] = preview_url
        return result

    def _handle_video(self, content: bytes, content_type: str) -> Dict[str, Any]:
        size = len(content)
        return {
            'type': 'video',
            'content_type': content_type,
            'size': size,
            'size_human': self._format_bytes(size),
            'url': self.service.service_url,
            'message': 'Video content detected. File too large to store, showing metadata only.',
            'duration': self._extract_video_duration(content),
            'fetched_at': timezone.now().isoformat()
        }

    def _handle_audio(self, content: bytes, content_type: str) -> Dict[str, Any]:
        size = len(content)
        if size < 5 * 1024 * 1024:
            audio_data = base64.b64encode(content).decode('utf-8')
            return {
                'type': 'audio',
                'content_type': content_type,
                'size': size,
                'size_human': self._format_bytes(size),
                'data': audio_data,
                'url': self.service.service_url,
                'duration': self._extract_audio_duration(content),
                'fetched_at': timezone.now().isoformat()
            }
        return {
            'type': 'audio',
            'content_type': content_type,
            'size': size,
            'size_human': self._format_bytes(size),
            'url': self.service.service_url,
            'message': 'Audio file too large to store. Showing metadata only.',
            'fetched_at': timezone.now().isoformat()
        }

    def _handle_document(self, resp_headers: Dict[str, str], content: bytes, content_type: str) -> Dict[str, Any]:
        size = len(content)
        doc_data = base64.b64encode(content).decode('utf-8')
        return {
            'type': 'document',
            'content_type': content_type,
            'size': size,
            'size_human': self._format_bytes(size),
            'data': doc_data,
            'url': self.service.service_url,
            'filename': self._extract_filename(resp_headers),
            'fetched_at': timezone.now().isoformat()
        }

    def _handle_json(self, content: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(content)
            return {
                'type': 'json',
                'content_type': 'application/json',
                'data': data,
                'size': len(content),
                'size_human': self._format_bytes(len(content)),
                'fetched_at': timezone.now().isoformat()
            }
        except json.JSONDecodeError:
            return {
                'type': 'json',
                'content_type': 'application/json',
                'error': 'Invalid JSON response',
                'raw_content': content[:1000].decode('utf-8', errors='replace'),
                'fetched_at': timezone.now().isoformat()
            }

    def _handle_html(self, content: bytes, resp_headers: Dict[str, str], final_url: Optional[str]) -> Dict[str, Any]:
        """
        Structural HTML extraction. Every field below is wrapped in
        _safe() (imported from scraper.py) so one bad selector or
        malformed block only costs that one field — never the whole
        fetch. `warnings` on the returned dict lists anything that
        failed; fetch_service_data() surfaces it on
        service.fetch_error without flipping fetch_status to 'error'
        (there IS usable data).
        """
        warnings: List[str] = []
        encoding = _guess_encoding(resp_headers)
        html_text = _safe(lambda: decode_text(content, encoding), content.decode('utf-8', errors='replace'), warnings, 'decode_text')

        soup = _safe(lambda: BeautifulSoup(html_text, 'lxml'), None, warnings, 'parse_lxml')
        if soup is None:
            soup = _safe(lambda: BeautifulSoup(html_text, 'html.parser'), None, warnings, 'parse_html_parser')
        if soup is None:
            # Total parse failure — still return something rather than
            # raising and losing the fetch entirely.
            warnings.append('HTML parsing failed entirely')
            return {
                'type': 'html', 'content_type': 'text/html',
                'title': '', 'description': '', 'keywords': '',
                'canonical_url': final_url or self.service.service_url,
                'favicon': '', 'author': '', 'published_time': '',
                'og_data': {}, 'twitter': {}, 'meta': {}, 'icons_feeds': {},
                'robots': {'robots_meta': '', 'x_robots_tag': '', 'is_indexable': None},
                'article': {}, 'alternate_languages': [], 'same_as': [],
                'category_breadcrumb': [], 'structured_data': [],
                'headings': {'h1': [], 'h2': [], 'h3': []},
                'paragraphs': [], 'lists': [], 'tables': [],
                'links': [], 'images': [], 'videos': [], 'iframes': [],
                'product_info': {}, 'specifications': {},
                'content': '', 'full_content': '',
                'rendered_with_browser': False,
                'url': self.service.service_url,
                'size': len(content), 'size_human': self._format_bytes(len(content)),
                'warnings': warnings,
                'fetched_at': timezone.now().isoformat(),
            }

        base_url = final_url or self.service.service_url

        title_tag = soup.find('title')
        title_text = title_tag.get_text(strip=True) if title_tag else ''
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        description = meta_desc.get('content', '') if meta_desc else ''

        structured_data = _safe(_extract_json_ld, [], warnings, 'json_ld', soup)
        og_effective = _safe(_extract_og, {}, warnings, 'og', soup, structured_data)
        twitter = _safe(_extract_twitter, {}, warnings, 'twitter', soup)
        page_meta = _safe(_extract_page_meta, {}, warnings, 'page_meta', soup)
        icons_feeds = _safe(_extract_icons_and_feeds, {}, warnings, 'icons_feeds', soup, base_url)
        robots = _safe(
            _extract_robots_meta, {'robots_meta': '', 'x_robots_tag': '', 'is_indexable': None},
            warnings, 'robots_meta', soup, resp_headers,
        )
        article = _safe(_extract_article_meta, {}, warnings, 'article_meta', soup, base_url)
        alternate_languages = _safe(_extract_alternate_languages, [], warnings, 'alt_languages', soup, base_url)
        same_as = _safe(_extract_same_as, [], warnings, 'same_as', structured_data)
        category_breadcrumb = _safe(_extract_breadcrumb, [], warnings, 'breadcrumb', structured_data, soup)

        canonical_url = _safe(_extract_canonical, base_url, warnings, 'canonical', soup, base_url)
        favicon_url = _safe(_extract_favicon, '', warnings, 'favicon', soup, base_url)
        author = _safe(_extract_author, '', warnings, 'author', soup)
        published_time = _safe(_extract_published_time, '', warnings, 'published_time', soup)
        headings = _safe(_extract_headings, {'h1': [], 'h2': [], 'h3': []}, warnings, 'headings', soup)
        product_info = _safe(_extract_product_info, {}, warnings, 'product_info', structured_data, soup)

        # Images/videos/iframes/links pulled before the nav/footer/etc
        # strip below, matching where scraper.py's equivalents run.
        images = _safe(self._extract_images, [], warnings, 'images', soup, base_url)
        videos, iframes = _safe(self._extract_videos_and_iframes, ([], []), warnings, 'videos', soup, base_url)
        links = _safe(self._extract_links, [], warnings, 'links', soup, base_url)

        # Strip scripts, styles, nav, footer, header, aside BEFORE any
        # body-content extraction below, so paragraphs/lists/tables/
        # headings-adjacent text scope to real page content only.
        _safe(lambda: [el.decompose() for el in soup(['script', 'style', 'nav', 'footer', 'header', 'aside'])], None, warnings, 'strip_chrome')

        paragraphs = _safe(lambda: [p.get_text(strip=True) for p in soup.find_all('p') if p.get_text(strip=True)], [], warnings, 'paragraphs')
        lists = _safe(self._extract_lists, [], warnings, 'lists', soup)
        tables = _safe(self._extract_tables, [], warnings, 'tables', soup)

        main_content = soup.find(['main', 'article']) or soup.find('body')
        text_content = _safe(lambda: main_content.get_text(separator='\n', strip=True) if main_content else '', '', warnings, 'text_content')

        data = {
            'type': 'html',
            'content_type': 'text/html',

            'title': title_text,
            'description': description,
            'keywords': page_meta.get('keywords', ''),
            'canonical_url': canonical_url,
            'favicon': favicon_url,
            'author': author,
            'published_time': published_time,

            'og_data': og_effective,
            'twitter': twitter,
            'meta': page_meta,
            'icons_feeds': icons_feeds,
            'robots': robots,
            'article': article,
            'alternate_languages': alternate_languages,
            'same_as': same_as,
            'category_breadcrumb': category_breadcrumb,
            'structured_data': structured_data,

            'headings': headings,
            'paragraphs': paragraphs[:20],
            'lists': lists,
            'tables': tables,

            'links': links[:50],

            'images': images[:30],
            'videos': videos,
            'iframes': iframes,

            'product_info': product_info,
            'specifications': product_info.get('specifications', {}),

            'content': text_content[:10000],
            'full_content': text_content,

            'rendered_with_browser': False,

            'url': self.service.service_url,
            'size': len(content),
            'size_human': self._format_bytes(len(content)),
            'warnings': warnings,
            'fetched_at': timezone.now().isoformat()
        }

        # JS-rendered SPA fallback — same trigger scraper.py uses
        # (thin text + no links after a "successful" plain-HTTP pass).
        # Only fills fields that came back empty; real server-rendered
        # <head> metadata from the first pass is left alone.
        if should_attempt_render(data['full_content'], data['links']):
            _safe(self._apply_browser_render, None, warnings, 'playwright_render', data, base_url)

        return data

    def _apply_browser_render(self, data: Dict[str, Any], url: str) -> None:
        rendered = render_with_browser(url, self.headers.get('User-Agent', DEFAULT_USER_AGENT))
        if not rendered:
            return
        rendered_html, rendered_final_url = rendered

        warnings = data['warnings']
        rendered_soup = _safe(lambda: BeautifulSoup(rendered_html, 'lxml'), None, warnings, 'parse_rendered_lxml')
        if rendered_soup is None:
            rendered_soup = _safe(lambda: BeautifulSoup(rendered_html, 'html.parser'), None, warnings, 'parse_rendered_html_parser')
        if rendered_soup is None:
            warnings.append('playwright_render: rendered HTML failed to parse')
            return

        data['rendered_with_browser'] = True
        base_url = rendered_final_url or data['canonical_url']

        structured_data = _safe(_extract_json_ld, data['structured_data'], warnings, 'json_ld_rendered', rendered_soup)
        if structured_data and not data['structured_data']:
            data['structured_data'] = structured_data

        og = _safe(_extract_og, {}, warnings, 'og_rendered', rendered_soup, structured_data)
        _backfill(data['og_data'], og)

        twitter = _safe(_extract_twitter, {}, warnings, 'twitter_rendered', rendered_soup)
        _backfill(data['twitter'], twitter)

        page_meta = _safe(_extract_page_meta, {}, warnings, 'page_meta_rendered', rendered_soup)
        _backfill(data['meta'], page_meta)

        icons_feeds = _safe(_extract_icons_and_feeds, {}, warnings, 'icons_feeds_rendered', rendered_soup, base_url)
        _backfill(data['icons_feeds'], icons_feeds)

        article = _safe(_extract_article_meta, {}, warnings, 'article_rendered', rendered_soup, base_url)
        _backfill(data['article'], article)

        if not data['alternate_languages']:
            data['alternate_languages'] = _safe(
                _extract_alternate_languages, [], warnings, 'alt_languages_rendered', rendered_soup, base_url,
            )
        if not data['same_as']:
            data['same_as'] = _safe(_extract_same_as, [], warnings, 'same_as_rendered', structured_data)
        if not data['category_breadcrumb']:
            data['category_breadcrumb'] = _safe(
                _extract_breadcrumb, [], warnings, 'breadcrumb_rendered', structured_data, rendered_soup,
            )

        if not data['favicon']:
            data['favicon'] = _safe(_extract_favicon, '', warnings, 'favicon_rendered', rendered_soup, base_url)
        if not data['author']:
            data['author'] = _safe(_extract_author, '', warnings, 'author_rendered', rendered_soup)
        if not data['published_time']:
            data['published_time'] = _safe(_extract_published_time, '', warnings, 'published_time_rendered', rendered_soup)
        if not data['product_info']:
            data['product_info'] = _safe(_extract_product_info, {}, warnings, 'product_info_rendered', structured_data, rendered_soup)
            data['specifications'] = data['product_info'].get('specifications', {})

        rendered_images = _safe(self._extract_images, [], warnings, 'images_rendered', rendered_soup, base_url)
        if len(rendered_images) > len(data['images']):
            data['images'] = rendered_images[:30]

        rendered_videos, rendered_iframes = _safe(self._extract_videos_and_iframes, ([], []), warnings, 'videos_rendered', rendered_soup, base_url)
        if len(rendered_videos) > len(data['videos']):
            data['videos'] = rendered_videos
            data['iframes'] = rendered_iframes

        rendered_links = _safe(self._extract_links, [], warnings, 'links_rendered', rendered_soup, base_url)
        if len(rendered_links) > len(data['links']):
            data['links'] = rendered_links[:50]

        rendered_text = _safe(lambda: rendered_soup.get_text('\n', strip=True), '', warnings, 'text_rendered')
        if len(rendered_text) > len(data['full_content']):
            data['full_content'] = rendered_text
            data['content'] = rendered_text[:10000]

    def _extract_images(self, soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
        """
        Filtered through the same tracking-pixel heuristic scraper.py
        uses, so junk 1x1 beacons never make it into extracted_images
        regardless of which of the two extractors a service was
        fetched with.

        is_probably_tracking_pixel() is called directly (not through
        _safe()) since it's a small pure function that already
        defends against bad width/height input — wrapping it per
        <img> tag allocated a throwaway warnings list on every
        iteration for no real safety benefit, which adds up on
        image-heavy pages.
        """
        images = []
        for img in soup.find_all('img'):
            img_src = img.get('src') or img.get('data-src')
            if not img_src:
                continue
            img_url = urljoin(base_url, img_src)
            if is_probably_tracking_pixel(img_url, img.get('width'), img.get('height')):
                continue

            parent_href = ''
            for parent in img.parents:
                if getattr(parent, 'name', None) == 'a' and parent.get('href'):
                    parent_href = urljoin(base_url, parent['href'])
                    break

            images.append({
                'url': img_url,
                'alt': img.get('alt', ''),
                'title': img.get('title', ''),
                'href': parent_href,
            })
        return images

    def _extract_videos_and_iframes(self, soup: BeautifulSoup, base_url: str) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        """
        Videos normalized to the {"url", "type"} shape documented on
        ConnectedService.extracted_videos and used by scraper.py, so a
        service's extracted_videos shape doesn't depend on which fetch
        path populated it. <video>/<source> tags are always direct
        media; <iframe> embeds are only kept (both in `iframes` and
        merged into `videos`) when video_info recognizes the platform
        — ads, maps, chat widgets etc. are filtered out the same way
        scraper.py does it.
        """
        videos = []
        seen_video_urls = set()
        for video in soup.find_all('video'):
            video_src = video.get('src')
            if video_src:
                url = urljoin(base_url, video_src)
                if url not in seen_video_urls:
                    seen_video_urls.add(url)
                    videos.append({'url': url, 'type': video.get('type', 'video/*')})
            for source in video.find_all('source'):
                src = source.get('src')
                if src:
                    url = urljoin(base_url, src)
                    if url not in seen_video_urls:
                        seen_video_urls.add(url)
                        videos.append({'url': url, 'type': source.get('type', 'video/*')})

        iframes = []
        for iframe in soup.find_all('iframe'):
            iframe_src = iframe.get('src')
            if not iframe_src:
                continue
            resolved_src = urljoin(base_url, iframe_src)
            if resolved_src in seen_video_urls:
                continue
            info = _safe(get_video_info_cached, {}, [], 'video_info', resolved_src)
            if info.get('platform') and info['platform'] != 'unknown':
                iframes.append({
                    'url': resolved_src,
                    'title': iframe.get('title', ''),
                })
                seen_video_urls.add(resolved_src)
                videos.append({'url': resolved_src, 'type': 'embed'})

        return videos, iframes

    @staticmethod
    def _extract_links(soup: BeautifulSoup, base_url: str) -> List[Dict[str, str]]:
        links = []
        for a in soup.find_all('a', href=True):
            link_text = a.get_text(strip=True)
            link_url = urljoin(base_url, a['href'])
            if link_text and link_url:
                links.append({'text': link_text, 'url': link_url})
        return links

    @staticmethod
    def _extract_lists(soup: BeautifulSoup) -> List[List[str]]:
        lists = []
        for ul in soup.find_all(['ul', 'ol']):
            list_items = [li.get_text(strip=True) for li in ul.find_all('li')]
            if list_items:
                lists.append(list_items)
        return lists

    @staticmethod
    def _extract_tables(soup: BeautifulSoup) -> List[List[List[str]]]:
        tables = []
        for table in soup.find_all('table'):
            table_data = []
            for row in table.find_all('tr'):
                cells = [cell.get_text(strip=True) for cell in row.find_all(['td', 'th'])]
                if cells:
                    table_data.append(cells)
            if table_data:
                tables.append(table_data)
        return tables

    def _handle_text(self, content: bytes, resp_headers: Dict[str, str]) -> Dict[str, Any]:
        encoding = _guess_encoding(resp_headers)
        text = decode_text(content, encoding)
        return {
            'type': 'text',
            'content_type': 'text/plain',
            'content': text[:10000],
            'size': len(content),
            'size_human': self._format_bytes(len(content)),
            'fetched_at': timezone.now().isoformat()
        }

    def _handle_generic(self, content: bytes, content_type: str) -> Dict[str, Any]:
        return {
            'type': 'generic',
            'content_type': content_type,
            'size': len(content),
            'size_human': self._format_bytes(len(content)),
            'url': self.service.service_url,
            'message': f'Content type {content_type} detected but no specific handler available.',
            'fetched_at': timezone.now().isoformat()
        }

    @staticmethod
    def _format_bytes(size: float) -> str:
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} PB"

    @staticmethod
    def _get_image_dimensions(content: bytes) -> Optional[Dict[str, int]]:
        try:
            from PIL import Image
        except ImportError:
            logger.debug("Pillow not installed — skipping image dimension extraction")
            return None
        try:
            with Image.open(BytesIO(content)) as img:
                return {'width': img.width, 'height': img.height}
        except Exception:
            # Corrupt/truncated/unsupported image data — expected
            # often enough (partial downloads, exotic formats) that
            # it doesn't warrant more than a debug-level note.
            logger.debug("Failed to read image dimensions", exc_info=True)
            return None

    @staticmethod
    def _extract_video_duration(content: bytes) -> None:
        return None  # Implement with ffmpeg-python if needed

    @staticmethod
    def _extract_audio_duration(content: bytes) -> None:
        return None  # Implement with mutagen or similar

    def _extract_filename(self, resp_headers: Dict[str, str]) -> str:
        content_disp = resp_headers.get('Content-Disposition', '')
        if content_disp:
            # filename* (RFC 5987, e.g. filename*=UTF-8''report.pdf)
            # takes precedence over plain filename= per RFC 6266, and
            # is common from APIs/CDNs serving non-ASCII names.
            star_match = _FILENAME_STAR_RE.search(content_disp)
            if star_match:
                try:
                    return unquote(star_match.group(1).strip())
                except Exception:
                    pass
            match = _FILENAME_RE.search(content_disp)
            if match:
                name = match.group(1).strip().strip('"')
                if name:
                    return name

        path = urlparse(self.service.service_url).path
        return path.rsplit('/', 1)[-1] or 'download'


def _reset_html_only_fields(service) -> None:
    """Blank every field that only makes sense for an HTML fetch, so a
    service that used to serve HTML and later starts serving e.g. a PDF
    at the same URL doesn't keep showing stale OG/Twitter/article data
    left over from its last HTML fetch."""
    service.og_title = service.og_description = service.og_thumbnail = ''
    service.og_site_name = service.og_type = service.og_locale = ''
    service.og_video = service.og_audio = ''
    service.twitter_card = service.twitter_title = service.twitter_description = ''
    service.twitter_image = service.twitter_site = service.twitter_creator = ''
    service.page_title = service.meta_description = service.meta_keywords = ''
    service.meta_generator = service.charset = service.content_language = ''
    service.theme_color = service.viewport = ''
    service.canonical_url = service.favicon = ''
    service.apple_touch_icon = service.manifest_url = service.amp_url = ''
    service.rss_feed_url = service.atom_feed_url = ''
    service.author = service.author_url = service.published_time = service.modified_time = ''
    service.article_section = ''
    service.article_tags = []
    service.category_breadcrumb = []
    service.structured_data = []
    service.alternate_languages = []
    service.same_as_links = []
    service.price_amount = None
    service.price_currency = service.availability = ''
    service.product_sku = service.product_gtin = service.product_condition = ''
    service.brand_name = service.seller_name = ''
    service.rating_value = None
    service.rating_count = None
    service.robots_meta = service.x_robots_tag = ''
    service.is_indexable = None
    service.extracted_headings = {'h1': [], 'h2': [], 'h3': []}
    service.extracted_text = ''
    service.word_count = 0
    service.reading_time_minutes = 0
    service.extracted_images = []
    service.extracted_videos = []
    service.extracted_links = []


def _compute_non_html_hash(data: Dict[str, Any]) -> str:
    """Best-effort content hash for a non-HTML fetch, used the same way
    scraper.py hashes extracted text: to notice "this URL's content
    changed" between fetches without diffing full payloads. Prefers an
    actual content-derived value (decoded text, canonical JSON, the
    base64 payload already captured for small images/audio/documents)
    over hashing the whole result dict, since that would include large
    base64 blobs redundantly and be needlessly expensive. For types
    where no content is retained at all (video, oversized binaries,
    unrecognized content-types), falls back to a coarse type+size
    fingerprint — enough to notice a differently-sized file at the same
    URL, not fine-grained content-change detection.
    """
    dtype = data.get('type')
    if dtype == 'text':
        return _hash_content(data.get('content') or '')
    if dtype == 'json':
        try:
            return _hash_content(json.dumps(data.get('data'), sort_keys=True, default=str))
        except (TypeError, ValueError):
            return ''
    if dtype in ('image', 'audio', 'document') and data.get('data'):
        return _hash_content(data['data'])
    return _hash_content(f"{data.get('content_type', '')}:{data.get('size', '')}")


def fetch_service_data(service, worker_id: Optional[str] = None) -> bool:
    """
    Main function to fetch data from a connected service.
    Updates the service object with fetched data and persists it via
    service.mark_fetch_success()/mark_fetch_error() — see the module
    docstring's "Enterprise crawling guards" section for why this
    matters (CrawlAttempt audit log, circuit breaker, retry scheduling,
    lock release all depend on going through those methods rather than
    a raw service.save()).

    Resilient like scraper.scrape_and_store(): fetch_status is only
    'error' when service_url couldn't be reached at all, was rejected
    by SSRF validation, or is currently blocked/rate-limited by its
    DomainCrawlPolicy. A field-level failure inside HTML extraction
    doesn't discard the whole fetch — it's recorded in `warnings` and
    surfaced via fetch_error, same as the scraper.py pipeline, so
    callers don't need to know which of the two populated a given
    service to know how to interpret fetch_error.
    """
    started_at = timezone.now()

    try:
        validate_crawl_url(service.service_url)
    except UnsafeCrawlURLError as exc:
        service.mark_fetch_error(
            str(exc), error_category=CrawlAttempt.ErrorCategory.SSRF_BLOCKED, worker_id=worker_id,
        )
        logger.warning("Refused to fetch %s: %s", service.service_name, exc)
        return False

    policy = service.domain_policy
    if policy is None and service.domain:
        policy, _ = DomainCrawlPolicy.objects.get_or_create(domain=service.domain)
        service.domain_policy = policy

    if policy and (policy.is_blocked or (service.respect_robots_txt and policy.disallow_all)):
        reason = policy.blocked_reason or "Domain is blocked by crawl policy (manual block or robots.txt)"
        service.mark_fetch_error(reason, error_category=CrawlAttempt.ErrorCategory.ROBOTS_BLOCKED, worker_id=worker_id)
        return False

    if policy and not policy.is_request_allowed_now():
        reason = "Domain rate limit exceeded for the current window — deferring"
        service.mark_fetch_error(reason, error_category=CrawlAttempt.ErrorCategory.RATE_LIMITED, worker_id=worker_id)
        return False

    fetcher = ServiceDataFetcher(service)
    try:
        data = fetcher.fetch()
    except Exception as e:
        if policy:
            policy.record_request()
        duration_ms = int((timezone.now() - started_at).total_seconds() * 1000)
        error_message = str(e)
        service.mark_fetch_error(
            error_message, error_category=CrawlAttempt.ErrorCategory.CONNECTION_ERROR,
            duration_ms=duration_ms, worker_id=worker_id,
        )
        logger.error("Failed to fetch data from %s: %s", service.service_name, error_message)
        return False

    if policy:
        policy.record_request()

    duration_ms = int((timezone.now() - started_at).total_seconds() * 1000)

    service.last_fetched_data = data
    service.content_type = _truncate(data.get('content_type'), 200) or ''
    service.page_size_bytes = data.get('size') or None

    if data.get('type') == 'html':
        og_data = data.get('og_data') or {}
        twitter = data.get('twitter') or {}
        meta = data.get('meta') or {}
        icons_feeds = data.get('icons_feeds') or {}
        robots = data.get('robots') or {}
        article = data.get('article') or {}
        product_info = data.get('product_info') or {}

        service.og_title       = _truncate(og_data.get('title') or data.get('title'), 500) or ''
        service.og_description = og_data.get('description') or data.get('description') or ''
        service.og_thumbnail   = og_data.get('thumbnail')    or ''
        service.og_site_name   = _truncate(og_data.get('site_name'), 200) or ''
        service.og_type        = _truncate(og_data.get('type'), 100) or ''
        service.og_locale      = _truncate(og_data.get('locale'), 20) or ''
        service.og_video       = og_data.get('video') or ''
        service.og_audio       = og_data.get('audio') or ''

        service.twitter_card        = _truncate(twitter.get('card'), 50) or ''
        service.twitter_title       = _truncate(twitter.get('title'), 500) or ''
        service.twitter_description = twitter.get('description') or ''
        service.twitter_image       = twitter.get('image') or ''
        service.twitter_site        = _truncate(twitter.get('site'), 100) or ''
        service.twitter_creator     = _truncate(twitter.get('creator'), 100) or ''

        service.page_title       = _truncate(meta.get('title') or data.get('title'), 500) or ''
        service.meta_description = meta.get('description') or data.get('description') or ''
        service.meta_keywords    = _truncate(meta.get('keywords') or data.get('keywords'), 1000) or ''
        service.meta_generator   = _truncate(meta.get('generator'), 200) or ''
        service.charset          = _truncate(meta.get('charset'), 50) or ''
        service.content_language = _truncate(meta.get('language'), 20) or ''
        service.theme_color      = _truncate(meta.get('theme_color'), 20) or ''
        service.viewport         = _truncate(meta.get('viewport'), 200) or ''

        service.canonical_url  = data.get('canonical_url') or ''
        service.favicon        = data.get('favicon') or ''
        service.apple_touch_icon = icons_feeds.get('apple_touch_icon') or ''
        service.manifest_url     = icons_feeds.get('manifest_url') or ''
        service.amp_url          = icons_feeds.get('amp_url') or ''
        service.rss_feed_url     = icons_feeds.get('rss_feed_url') or ''
        service.atom_feed_url    = icons_feeds.get('atom_feed_url') or ''
        service.author         = data.get('author') or ''
        service.author_url     = article.get('author_url') or ''
        service.published_time = data.get('published_time') or ''
        service.modified_time  = article.get('modified_time') or ''
        service.article_section = _truncate(article.get('section'), 200) or ''
        service.article_tags    = article.get('tags') or []
        service.category_breadcrumb = data.get('category_breadcrumb') or []
        service.structured_data = data.get('structured_data') or []
        service.alternate_languages = data.get('alternate_languages') or []
        service.same_as_links = data.get('same_as') or []

        service.price_amount = _to_decimal(product_info.get('price'), 12, 2)
        service.price_currency = _truncate(product_info.get('currency'), 3) or ''
        service.availability = _truncate(product_info.get('availability'), 50) or ''
        service.product_sku = _truncate(product_info.get('sku'), 100) or ''
        service.product_gtin = _truncate(product_info.get('gtin'), 50) or ''
        service.product_condition = _truncate(product_info.get('condition'), 50) or ''
        service.brand_name = _truncate(product_info.get('brand'), 200) or ''
        service.seller_name = _truncate(product_info.get('seller'), 200) or ''
        service.rating_value = _to_decimal(product_info.get('rating_value'), 4, 2)
        service.rating_count = _to_positive_int(product_info.get('rating_count'))

        service.robots_meta = _truncate(robots.get('robots_meta'), 200) or ''
        service.x_robots_tag = _truncate(robots.get('x_robots_tag'), 200) or ''
        service.is_indexable = robots.get('is_indexable')

        service.extracted_headings = data.get('headings') or {'h1': [], 'h2': [], 'h3': []}
        service.extracted_text  = data.get('full_content') or ''
        service.word_count = len(service.extracted_text.split()) if service.extracted_text else 0
        service.reading_time_minutes = max(1, round(service.word_count / 200)) if service.word_count else 0

        service.extracted_images = data.get('images', [])
        # 'videos' already includes recognized iframe embeds merged in
        # (as {"url", "type": "embed"}), so no separate iframe-merge
        # step is needed here.
        service.extracted_videos = data.get('videos', [])
        service.extracted_links = data.get('links', [])

        content_hash = _hash_content(service.extracted_text)
    else:
        _reset_html_only_fields(service)
        content_hash = _compute_non_html_hash(data)

    warnings = data.get('warnings') or []

    service.mark_fetch_success(
        content_hash=content_hash or None,
        http_status_code=200,
        duration_ms=duration_ms,
        worker_id=worker_id,
    )
    # mark_fetch_success() unconditionally resets fetch_error to None
    # as part of clearing error state, so warnings must be applied
    # AFTER it runs (see the matching fix/comment in
    # scraper.scrape_and_store — same bug, same fix, kept consistent
    # across both fetch pipelines).
    if warnings:
        service.fetch_error = '; '.join(warnings)
        service.save(update_fields=['fetch_error', 'updated_at'])

    try:
        refresh_feed_cache(service)
    except Exception:
        logger.exception(
            "refresh_feed_cache failed for service %s after successful fetch",
            service.pk,
        )

    logger.info("Successfully fetched data from %s", service.service_name)
    return True