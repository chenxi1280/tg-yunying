from __future__ import annotations

import pytest

from app.services.task_center.ai_group_vocabulary_catalog import (
    ADULT_VOCABULARY_CATALOG,
    ADULT_COMPATIBILITY_MANIFEST,
    GENERAL_VOCABULARY_CATALOG,
    GENERAL_COMPATIBILITY_MANIFEST,
    get_vocabulary_catalog,
    validate_vocabulary_catalog,
)

pytestmark = pytest.mark.no_postgres


def test_adult_vocabulary_catalog_completeness_and_validity():
    ok, msg = validate_vocabulary_catalog(ADULT_VOCABULARY_CATALOG, min_units=120)
    assert ok is True, f"Adult catalog invalid: {msg}"
    assert len(ADULT_VOCABULARY_CATALOG) >= 120

    categories = {u.category for u in ADULT_VOCABULARY_CATALOG}
    assert len(categories) >= 10
    assert "appearance_authenticity" in categories
    assert "attitude_communication" in categories
    assert "cooperation_pacing" in categories
    assert "environment_access" in categories
    assert "schedule_timing" in categories
    assert "cautious_verification" in categories
    assert "statement_emotion" in categories
    assert "minimal_natural_react" in categories
    assert "transition_continuation" in categories
    assert "persona_nuance" in categories


def test_general_vocabulary_catalog_completeness_and_validity():
    ok, msg = validate_vocabulary_catalog(GENERAL_VOCABULARY_CATALOG, min_units=120)
    assert ok is True, f"General catalog invalid: {msg}"
    assert len(GENERAL_VOCABULARY_CATALOG) >= 120


def test_get_vocabulary_catalog_family_isolation():
    adult_cat = get_vocabulary_catalog("adult")
    gen_cat = get_vocabulary_catalog("general")
    assert adult_cat is ADULT_VOCABULARY_CATALOG
    assert gen_cat is GENERAL_VOCABULARY_CATALOG
    assert len(adult_cat) >= 120
    assert len(gen_cat) >= 120


def test_only_compatibility_cells_with_at_least_twelve_units_are_published():
    assert ADULT_COMPATIBILITY_MANIFEST
    assert GENERAL_COMPATIBILITY_MANIFEST
    for catalog, manifest in (
        (ADULT_VOCABULARY_CATALOG, ADULT_COMPATIBILITY_MANIFEST),
        (GENERAL_VOCABULARY_CATALOG, GENERAL_COMPATIBILITY_MANIFEST),
    ):
        for theme, route, act_type, stance, fact_class in manifest:
            count = sum(
                theme in unit.theme_tags
                and route in unit.allowed_routes
                and act_type in unit.allowed_act_types
                and stance in unit.allowed_stances
                and fact_class == unit.fact_class
                for unit in catalog
            )
            assert count >= 12


def test_validator_rejects_catalog_drift_from_fixed_published_manifest():
    modified = [
        unit
        for unit in ADULT_VOCABULARY_CATALOG
        if unit.vocabulary_id != "adult_app_03"
    ]

    ok, message = validate_vocabulary_catalog(modified, min_units=120)

    assert ok is False
    assert message.startswith("published_cell_size_insufficient:")
