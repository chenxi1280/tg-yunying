from __future__ import annotations

from datetime import timedelta

ONLINE_LOGIN_REQUIRED_RETRY_AFTER = timedelta(minutes=30)
ONLINE_PROBE_FAILURE_RETRY_AFTER = timedelta(minutes=5)
ONLINE_PROBE_INTERVAL = timedelta(minutes=1)
ONLINE_STALE_AFTER = timedelta(minutes=2)
