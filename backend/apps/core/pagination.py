"""Pagination classes shared by every API in the project."""

from __future__ import annotations

from collections import OrderedDict

from rest_framework.pagination import CursorPagination, PageNumberPagination
from rest_framework.response import Response

__all__ = ["StandardPagination", "CompactPagination", "FeedCursorPagination"]


class StandardPagination(PageNumberPagination):
    """
    Default for detail-ish collections.

    Adds ``page`` / ``pages`` to the payload so front-end code does not have to
    parse the ``next``/``previous`` URLs to render a pager.
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response(
            OrderedDict(
                [
                    ("count", self.page.paginator.count),
                    ("pages", self.page.paginator.num_pages),
                    ("page", self.page.number),
                    ("page_size", self.get_page_size(self.request)),
                    ("next", self.get_next_link()),
                    ("previous", self.get_previous_link()),
                    ("results", data),
                ]
            )
        )


class CompactPagination(StandardPagination):
    """For lightweight rows (notifications, logs, autocomplete)."""

    page_size = 50
    max_page_size = 200


class FeedCursorPagination(CursorPagination):
    """
    Stable pagination for append-heavy feeds.

    Offset pagination shifts rows when new items land at the head, which makes
    an infinite-scroll feed repeat or skip entries. Cursor pagination does not.
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
    ordering = "-created_at"
