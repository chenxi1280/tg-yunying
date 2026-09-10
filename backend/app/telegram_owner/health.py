"""Read-only owner readiness used by Compose."""
import json
import os

from app.config import get_settings
from .rpc import OwnerGateway


def main():
    status = OwnerGateway(get_settings()).call('__status__')
    if status['release_sha'] != os.getenv('RELEASE_SHA', ''):
        raise RuntimeError('telegram_owner_release_mismatch')
    print(json.dumps(status, sort_keys=True))


if __name__ == '__main__':
    main()
