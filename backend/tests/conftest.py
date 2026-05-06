from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


TEST_DB_PATH = Path("/tmp") / f"tg_yunying_test_{uuid4().hex}.sqlite3"
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_DB_PATH}")
