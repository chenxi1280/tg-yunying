from __future__ import annotations

import os


DEDICATED_WORKER_PROCESS = (os.getenv("WORKER_ROLE") or "").strip().lower() not in {
    "",
    "all",
}

if not DEDICATED_WORKER_PROCESS:
    from .service import *  # noqa: F401,F403
    from .account_coverage import *  # noqa: F401,F403
    from .ai_generation_worker import *  # noqa: F401,F403
    from .list_page import *  # noqa: F401,F403
