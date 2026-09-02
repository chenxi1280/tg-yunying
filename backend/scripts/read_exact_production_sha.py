"""Read exact container image, current release symlink, and runtime SHA from production."""

from __future__ import annotations
import os
import subprocess

def main():
    print("=== EXACT PRODUCTION RUNTIME FACTS ===")
    
    # 1. read current symlink
    current_symlink = os.popen("readlink -f /data/tgyunying/current 2>/dev/null").read().strip()
    print(f"HOST_CURRENT_RELEASE_SYMLINK: {current_symlink}")
    
    # 2. read docker image tags
    backend_image = os.popen("docker inspect tgyunying-backend --format "{{.Config.Image}}" 2>/dev/null").read().strip()
    print(f"BACKEND_CONTAINER_IMAGE: {backend_image}")
    
    # 3. read running code inside backend container
    code_version = os.popen("docker exec tgyunying-backend python -c "from app.services.task_center.ai_group_vocabulary_catalog import VOCABULARY_CATALOG_VERSION, ADULT_VOCABULARY_CATALOG; print(f'CATALOG_VERSION={VOCABULARY_CATALOG_VERSION}, ADULT_UNITS={len(ADULT_VOCABULARY_CATALOG)}')" 2>/dev/null").read().strip()
    print(f"RUNNING_BACKEND_CODE_PROBE: {code_version}")

if __name__ == "__main__":
    main()
