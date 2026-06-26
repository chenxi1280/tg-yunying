from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGET_PROFILE_ROUTER = PROJECT_ROOT / "backend/app/api/routers/target_profile.py"
ROUTERS_INIT = PROJECT_ROOT / "backend/app/api/routers/__init__.py"
PERMISSION_MIDDLEWARE = PROJECT_ROOT / "backend/app/permission_middleware.py"

EXPECTED_ENDPOINTS = (
    ("get", "/api/target-profile"),
    ("patch", "/api/target-profile/settings"),
    ("get", "/api/target-profile/usage"),
    ("get", "/api/target-profile/source-candidates"),
    ("get", "/api/target-profile/sources"),
    ("put", "/api/target-profile/sources"),
    ("post", "/api/target-profile/sources/{source_id}/sync"),
    ("post", "/api/target-profile/sources/{source_id}/pull-history"),
    ("get", "/api/target-profile/runs"),
    ("get", "/api/target-profile/runs/{run_id}"),
    ("get", "/api/target-profile/samples"),
    ("patch", "/api/target-profile/samples/{sample_id}"),
    ("get", "/api/target-profile/quality-rules"),
    ("patch", "/api/target-profile/quality-rules"),
    ("post", "/api/target-profile/recompute-candidates"),
    ("post", "/api/target-profile/rebuild"),
    ("get", "/api/target-profile/versions"),
    ("post", "/api/target-profile/versions/{version_id}/restore"),
    ("post", "/api/target-profile/clear"),
)


def test_target_profile_router_exposes_prd_endpoint_contract():
    source = TARGET_PROFILE_ROUTER.read_text()

    missing = [
        f"{method.upper()} {path}"
        for method, path in EXPECTED_ENDPOINTS
        if f'@router.{method}("{path}")' not in source
    ]

    assert missing == []


def test_target_profile_router_is_registered():
    source = ROUTERS_INIT.read_text()

    assert "from .target_profile import router as target_profile_router" in source
    assert "target_profile_router," in source[source.index("for sub_router in ("):]


def test_target_profile_permission_middleware_maps_read_and_write_permissions():
    source = PERMISSION_MIDDLEWARE.read_text()

    assert '_compile("GET", r"^/api/target-profile(?:/.*)?$", "target_profile.view")' in source
    for method in ("POST", "PATCH", "PUT"):
        expected = f'_compile("{method}", r"^/api/target-profile(?:/.*)?$", "target_profile.manage")'
        assert expected in source
