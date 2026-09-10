"""Record the actual connection owner separately from the business worker."""
from app.config import get_settings

from .request_context import bind_identity
from .rpc import OwnerGateway


def prepare_attempt(attempt):
    settings = get_settings()
    if settings.telegram_owner_mode != 'client':
        return
    status = OwnerGateway(settings).call('__status__')
    identity = 'telegram-gateway:' + attempt.id
    bind_identity(identity, status['instance_id'])
    attempt.result_snapshot = {**dict(attempt.result_snapshot or {}),
                               'transport_owner_kind': 'telegram_owner',
                               'transport_owner_instance_id': status['instance_id'],
                               'transport_owner_release_sha': status['release_sha']}
