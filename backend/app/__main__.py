from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT_DIR / ".venv" / "bin" / "python"


def _restart_with_project_python() -> None:
    if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])


def main() -> None:
    _restart_with_project_python()
    sys.path.insert(0, str(ROOT_DIR))
    os.chdir(ROOT_DIR)
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        if exc.name != "uvicorn":
            raise
        raise SystemExit(
            "缺少后端依赖 uvicorn。\n"
            "请先在项目根目录执行：\n"
            "  uv venv backend/.venv --python python3.12\n"
            "  uv pip install --python backend/.venv/bin/python -e 'backend[dev]'\n"
            "然后重新运行。"
        ) from exc

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "true").strip().lower() in {"1", "true", "yes", "on"}
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
