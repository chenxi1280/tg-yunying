from __future__ import annotations

import argparse
import json

from app.workers.authorization_dr_artifact_recovery import recover_artifact
from app.workers.authorization_dr_node import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover an approved authorization DR artifact without login")
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    result = recover_artifact(load_config(), args.operation_id)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
