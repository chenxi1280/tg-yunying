from __future__ import annotations


def pagination_headers(*, total_count: int, page: int, page_size: int) -> dict[str, str]:
    return {
        "X-Total-Count": str(total_count),
        "X-Page": str(page),
        "X-Page-Size": str(page_size),
    }
