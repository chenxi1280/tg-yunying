"""Current-authorization transport fields shared by rank planning and real candidate search."""
from app.config import get_settings
from app.services.task_center.direct_search_runtime import build_direct_search_environment


def direct_rank_transport_fields(session, account) -> dict:
    environment = build_direct_search_environment(session, account, settings=get_settings())
    return {
        "proxy_airport_node_id": None,
        "runtime_environment": {
            **environment.runtime_environment,
            "account_pool_id": account.pool_id,
        },
    }
