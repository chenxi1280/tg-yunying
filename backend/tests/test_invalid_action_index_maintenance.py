import importlib.util
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/manage_invalid_action_index.py"


def _module():
    spec = importlib.util.spec_from_file_location("invalid_action_index", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(module):
    return [{"oid": oid, "relname": name, "indisvalid": name == module.FORMAL,
             "indisready": True, "indisprimary": False, "indisunique": False, "indisreplident": False,
             "definition": f"CREATE INDEX {name} ON public.actions USING btree (id)"}
            for oid, name in enumerate([module.FORMAL, module.TARGET], start=1)]


@pytest.mark.parametrize("change", ["valid", "not_ready", "formal_invalid", "unique", "definition", "oid"])
def test_index_drift_and_business_constraints_prevent_drop(change):
    module = _module()
    rows = _rows(module)
    expected = module.fingerprint(rows)
    if change == "valid":
        rows[1]["indisvalid"] = True
    elif change == "not_ready":
        rows[1]["indisready"] = False
    elif change == "formal_invalid":
        rows[0]["indisvalid"] = False
    elif change == "unique":
        rows[1]["indisunique"] = True
    elif change == "definition":
        rows[1]["definition"] += " WHERE status = 'pending'"
    else:
        rows[1]["oid"] = 99
    with pytest.raises(ValueError, match="drift"):
        module.validate(rows, expected)
    if change != "oid":
        with pytest.raises(ValueError):
            module.validate(rows, module.fingerprint(rows))


def test_only_invalid_nonconstraint_equivalent_index_is_eligible():
    module = _module()
    rows = _rows(module)
    module.validate(rows, module.fingerprint(rows))
