"""Read exact container image, current release symlink, and runtime SHA from production."""

from __future__ import annotations
import subprocess

def main():
    print("=== EXACT PRODUCTION RUNTIME FACTS ===")
    
    # 1. read current symlink
    try:
        current_symlink = subprocess.check_output(["readlink", "-f", "/data/tgyunying/current"], text=True).strip()
        print(f"HOST_CURRENT_RELEASE_SYMLINK: {current_symlink}")
    except Exception as e:
        print(f"HOST_CURRENT_RELEASE_SYMLINK_ERROR: {e}")
    
    # 2. read docker image tags
    try:
        backend_image = subprocess.check_output(["docker", "inspect", "tgyunying-backend", "--format", "{{.Config.Image}}"], text=True).strip()
        print(f"BACKEND_CONTAINER_IMAGE: {backend_image}")
    except Exception as e:
        print(f"BACKEND_CONTAINER_IMAGE_ERROR: {e}")
    
    # 3. read running code inside backend container
    try:
        code_version = subprocess.check_output([
            "docker", "exec", "tgyunying-backend", "python", "-c",
            "import os; from app.services.task_center import ai_group_prompt; print('AI_GROUP_PROMPT_EXISTS=True')"
        ], text=True).strip()
        print(f"RUNNING_BACKEND_CODE_PROBE: {code_version}")
    except Exception as e:
        print(f"RUNNING_BACKEND_CODE_PROBE_ERROR: {e}")

if __name__ == "__main__":
    main()
