from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.no_postgres


def test_post_login_package_keeps_worker_imports_lazy() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": str(backend_root),
        "WORKER_ROLE": "account-login",
    }
    code = """
import importlib
import sys

import app.services.account_post_login_init

eager_prefix = 'app.services.account_post_login_init.'
assert not any(name.startswith(eager_prefix) for name in sys.modules)
post_init = importlib.import_module('app.services.account_post_login_init.drain')
account_login = importlib.import_module('app.services.account_login.drain')
assert callable(post_init.drain_account_post_login_initializations)
assert callable(account_login.drain_account_login_batches)
"""
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_root,
        env=environment,
        check=True,
        timeout=30,
    )
