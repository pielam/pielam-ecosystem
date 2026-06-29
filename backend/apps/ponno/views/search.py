# backend/apps/ponno/views/ajax_search.py
"""
Ajax Search View — Optimized for high concurrency.

Optimizations applied:
──────────────────────────────────────────────────────────────────────
1.  Redis cache per query string — identical searches share one DB hit.
2.  .only() — fetches the 8 columns we actually serialize, nothing more.
3.  .values() list — avoids constructing full ORM objects for JSON.
4.  Input sanitization — length cap prevents cache-key poisoning.
5.  Soft-delete guard — active_products() manager used explicitly.
6.  Graceful null handling — brand/category may be NULL on some rows.
──────────────────────────────────────────────────────────────────────
"""

import hashlib

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.db.models import Q

from apps.ponno.models.product import Product


# Cache search results for 60 seconds.
# At 100k searches/min for the same popular term, the DB only sees
# one query per 60-second window instead of 100k.
SEARCH_CACHE_TTL = 60
MAX_RESULTS      = 10
MAX_QUERY_LEN    = 100  # prevent abuse / cache-key bloat


@require_GET
def AjaxSearchView(request):
    query = request.GET.get('q', '').strip()[:MAX_QUERY_LEN]

    if len(query) < 2:
        # Don't bother querying for single-character input
        return JsonResponse([], safe=False)

    # ── Cache-aside ───────────────────────────────────────────────────
    cache_key = 'ajax_search_' + hashlib.md5(query.lower().encode()).hexdigest()
    cached    = cache.get(cache_key)
    if cached is not None:
        return JsonResponse(cached, safe=False)

    # ── Build filter ──────────────────────────────────────────────────
    keywords = query.split()
    q_obj    = Q()
    for word in keywords:
        q_obj |= (
            Q(product_name__icontains=word)          |
            Q(product_title__icontains=word)         |
            Q(category__category_name__icontains=word)|
            Q(brand__brand_name__icontains=word)
        )

    # ── Single DB query — only needed columns, via .values() ──────────
    # .values() returns dicts directly from the DB driver — no Python
    # object construction, no __dict__ allocation, ~40% faster than
    # iterating over model instances for pure read/serialize use-cases.
    rows = (
        Product.objects
        .active_products()          # is_active=True, deleted_at__isnull=True
        .filter(q_obj)
        .select_related('brand', 'category')
        .only(
            'product_name', 'slug', 'selling_price', 'brand_price',
            'image', 'brand__brand_name', 'category__category_name',
        )
        .distinct()
        .values(
            'product_name',
            'slug',
            'selling_price',
            'brand_price',
            'image',
            'brand__brand_name',
            'category__category_name',
        )[:MAX_RESULTS]
    )

    # ── Serialize ─────────────────────────────────────────────────────
    results = [
        {
            'name':          row['product_name'],
            'slug':          row['slug'],
            'category':      row['category__category_name'] or 'Uncategorized',
            'brand':         row['brand__brand_name']       or 'No Brand',
            'brand_price':   str(row['brand_price'])   if row['brand_price']   else None,
            'selling_price': str(row['selling_price']) if row['selling_price'] else None,
            'image':         f"/media/{row['image']}"  if row['image'] else '/static/ponno/img/no-image.png',
        }
        for row in rows
    ]

    cache.set(cache_key, results, SEARCH_CACHE_TTL)
    return JsonResponse(results, safe=False)