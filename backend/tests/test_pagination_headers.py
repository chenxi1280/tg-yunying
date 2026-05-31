from __future__ import annotations


def test_pagination_headers_are_string_values():
    from app.api.pagination import pagination_headers

    assert pagination_headers(total_count=55, page=2, page_size=20) == {
        "X-Total-Count": "55",
        "X-Page": "2",
        "X-Page-Size": "20",
    }
