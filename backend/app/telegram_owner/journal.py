"""Content-free, fsynced ownership receipts for issued mutations and task calls."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

READ_METHODS = frozenset({
    'check_account_health', 'check_account_health_isolated', 'list_authorizations',
    'authorization_identity', 'get_two_fa_status', 'list_groups', 'resolve_group_by_public_username',
    'poll_verification_codes', 'list_contacts', 'probe_search_join_membership',
    'search_rank_deboost_candidates', 'probe_message_visible', 'probe_target_capabilities',
    'read_current_authorization', 'pull_profile', 'pull_profile_avatar_fingerprint',
    'fetch_verification_context', 'fetch_verification_media', 'fetch_group_archive',
    'fetch_group_messages', 'fetch_group_message', 'download_cached_material',
    'fetch_channel_messages', 'fetch_channel_discussion_identity', 'fetch_channel_message_deletions',
    'fetch_channel_reaction_capability', 'fetch_channel_comments', 'fetch_raw_channel_boundary',
    'fetch_raw_group_admin_rights', 'fetch_raw_authorization_update_state',
    'fetch_raw_authorization_difference', 'fetch_raw_channel_difference',
    'fetch_raw_pinned_message_id', 'fetch_raw_forum_topic', 'invalidate_session_cache',
})


class InvocationJournal:
    def __init__(self, directory: Path, instance_id: str):
        self.directory = directory
        self.instance_id = instance_id
        self._lock = threading.Lock()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)

    def write(self, request: dict, state: str, **facts):
        if request['method'] in READ_METHODS and not request.get('root_identity'):
            return
        now = datetime.now(timezone.utc)
        row = {'at': now.isoformat(), 'instance_id': self.instance_id,
               'request_id': request['request_id'], 'root_identity': request.get('root_identity', ''),
               'method': request['method'], 'state': state, **facts}
        path = self.directory / (now.strftime('%Y-%m-%d') + '.jsonl')
        with self._lock:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                encoded = (json.dumps(row, sort_keys=True) + '\n').encode()
                if os.write(fd, encoded) != len(encoded):
                    raise OSError('telegram_owner_journal_short_write')
                os.fsync(fd)
            finally:
                os.close(fd)
