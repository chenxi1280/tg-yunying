from __future__ import annotations

from datetime import timedelta

ONLINE_LOGIN_REQUIRED_RETRY_AFTER = timedelta(minutes=30)
ONLINE_PROBE_FAILURE_RETRY_AFTER = timedelta(minutes=5)
ONLINE_PROBE_INTERVAL = timedelta(minutes=5)
ONLINE_LOW_FREQUENCY_PROBE_INTERVAL = timedelta(minutes=15)
ONLINE_STALE_AFTER = timedelta(minutes=10)
ONLINE_STALE_GRACE = timedelta(minutes=5)
