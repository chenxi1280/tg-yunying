# Grok CLI Production Dry Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `tg-yunying` 硅谷生产服务器安装并鉴权 Grok CLI，使用生产测试任务、测试群、测试账号和现有 AI 活群 Prompt 拼装逻辑生成一次候选内容，并证明没有创建发送 action 或 Telegram 消息。

**Architecture:** 不修改应用代码或租户 Provider。通过 SSH 在生产主机执行只读基线检查；在部署用户目录安装固定版本 Grok CLI；在 `tgyunying-backend` 一次性 Python 进程中读取暂停/草稿测试任务上下文，并临时截获 `generate_group_messages()` 最终传给 Provider 的 system/user Prompt；随后由宿主机 Grok CLI 以 `grok-4.5` 单轮生成。执行前后对所选任务的 action、execution attempt 和 remote message ID 做只读核对。

**Tech Stack:** SSH、Docker Compose、Python/SQLAlchemy、现有 `backend/app/services/task_center/ai_generator.py`、Grok Build CLI 0.2.93、JSON/JQ。

---

### Task 1: Production baseline and target discovery

**Files:**
- Read: `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`
- Read: `backend/app/models/task_center.py`
- Read: `backend/app/models/groups.py`
- Read: `backend/app/models/accounts.py`
- No production file writes

- [ ] **Step 1: Verify SSH, host identity, capacity, and application health**

Run:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=15 codex_usa01_server '
  set -e
  hostname
  uname -sm
  id
  uptime
  df -h / /data
  docker ps --filter name=tgyunying --format "{{.Names}} {{.Status}}"
  curl -fsS --max-time 10 http://127.0.0.1:18090/api/health
'
```

Expected: SSH exit 0; Linux architecture is `x86_64` or `aarch64`; `/data` has at least 1 GiB available; backend health returns `{"status":"ok"}`; no production container is restarted.

- [ ] **Step 2: Discover only paused/draft/stopped test AI tasks and linked test groups/accounts**

Run the following read-only command:

```bash
ssh codex_usa01_server 'docker exec -i tgyunying-backend python -' <<'PY'
import json
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Task, TgAccount, TgGroup, TgGroupAccount
from app.models.enums import AccountStatus

TEST_MARKERS = ("测试", "test", "smoke")
SAFE_STATUSES = {"draft", "paused", "stopped"}

with SessionLocal() as session:
    tasks = session.scalars(
        select(Task)
        .where(
            Task.type == "group_ai_chat",
            Task.status.in_(SAFE_STATUSES),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.updated_at.desc(), Task.created_at.desc())
    ).all()
    candidates = []
    for task in tasks:
        config = dict(task.type_config or {})
        group_id = int(config.get("target_group_id") or 0)
        group = session.get(TgGroup, group_id) if group_id else None
        labels = f"{task.name} {group.title if group else ''}".lower()
        if not group or not any(marker in labels for marker in TEST_MARKERS):
            continue
        links = session.scalars(
            select(TgGroupAccount)
            .where(
                TgGroupAccount.tenant_id == task.tenant_id,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.can_send.is_(True),
            )
            .order_by(TgGroupAccount.id.asc())
        ).all()
        accounts = []
        for link in links:
            account = session.get(TgAccount, link.account_id)
            if account and account.deleted_at is None and account.status == AccountStatus.ACTIVE.value:
                accounts.append({"id": account.id, "display_name": account.display_name})
        if accounts:
            candidates.append({
                "task_id": task.id,
                "task_name": task.name,
                "task_status": task.status,
                "tenant_id": task.tenant_id,
                "group_id": group.id,
                "group_title": group.title,
                "accounts": accounts,
            })
    print(json.dumps(candidates, ensure_ascii=False))
PY
```

Expected: JSON contains at least one candidate whose task or group name visibly contains `测试`, `test`, or `smoke`, task status is `draft`/`paused`/`stopped`, and at least one linked active account is returned. If none exists, stop with `blocked`; do not substitute a normal running production task.

### Task 2: Install and verify pinned Grok CLI

**Files:**
- Create on production host: `~/.grok/bin/grok` and Grok-managed files under `~/.grok/`
- No repository or application container changes

- [ ] **Step 1: Check whether Grok CLI already exists**

Run:

```bash
ssh codex_usa01_server 'if command -v grok >/dev/null 2>&1; then grok --version; elif [ -x "$HOME/.grok/bin/grok" ]; then "$HOME/.grok/bin/grok" --version; else echo GROK_NOT_INSTALLED; fi'
```

Expected: either an existing version is printed or the exact marker `GROK_NOT_INSTALLED` is printed.

- [ ] **Step 2: Install from the official xAI installer only when absent**

Run only if Step 1 prints `GROK_NOT_INSTALLED`:

```bash
ssh codex_usa01_server 'curl -fsSL https://x.ai/cli/install.sh | bash'
```

Expected: exit 0 and `$HOME/.grok/bin/grok` exists.

- [ ] **Step 3: Pin and verify version 0.2.93**

Run:

```bash
ssh codex_usa01_server '
  set -e
  GROK_BIN="$(command -v grok || true)"
  [ -n "$GROK_BIN" ] || GROK_BIN="$HOME/.grok/bin/grok"
  "$GROK_BIN" update --version 0.2.93
  "$GROK_BIN" --version
'
```

Expected: final output contains `grok 0.2.93`.

### Task 3: Authenticate Grok CLI on the production host

**Files:**
- Create/update on production host: Grok-managed OAuth credentials under `~/.grok/`
- No application secrets copied from the local machine

- [ ] **Step 1: Check authentication without displaying credentials**

Run:

```bash
ssh codex_usa01_server '$HOME/.grok/bin/grok models'
```

Expected: `grok-4.5` is listed. If output says `You are not authenticated`, continue to Step 2.

- [ ] **Step 2: Start official device-code authentication**

Run in a TTY:

```bash
ssh -t codex_usa01_server '$HOME/.grok/bin/grok login --device-auth'
```

Expected: CLI prints an `accounts.x.ai` URL and short device code. The user completes the xAI authorization in a browser. Do not copy local OAuth files to production.

- [ ] **Step 3: Re-verify model access**

Run:

```bash
ssh codex_usa01_server '$HOME/.grok/bin/grok models'
```

Expected: login is reported and `grok-4.5` is the default or available model. If xAI returns subscription/credit blockage, record `blocked` and do not continue.

### Task 4: Capture the real production Prompt without calling the existing Provider

**Files:**
- Create temporarily on production host: `/tmp/tgyunying-grok-dry-run/capture.json`
- Read: `backend/app/services/task_center/ai_generator.py`
- Read: `backend/app/services/task_center/account_voice_profiles.py`
- Read: `backend/app/services/task_center/executors/group_ai_chat.py`

- [ ] **Step 1: Create the temporary directory with private permissions**

Run:

```bash
ssh codex_usa01_server 'umask 077; rm -rf /tmp/tgyunying-grok-dry-run; mkdir -p /tmp/tgyunying-grok-dry-run'
```

Expected: directory exists with mode `700` and is owned by the SSH user.

- [ ] **Step 2: Capture final system/user Prompt in a process-local interception**

Use the first candidate from Task 1 only when it is the newest safe-status test candidate. The script revalidates all safety properties before capture and aborts otherwise.

Run:

```bash
ssh codex_usa01_server 'docker exec -i tgyunying-backend python -' > /tmp/tgyunying-grok-capture.local.json <<'PY'
import json
from types import SimpleNamespace

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Action, ExecutionAttempt, GroupContextMessage, Task, TgAccount, TgGroup, TgGroupAccount
from app.models.enums import AccountStatus
from app.services.task_center import ai_generator
from app.services.task_center.account_voice_profiles import voice_profile_prompt_details
from app.services.task_center.executors.group_ai_chat import _account_prompt_profiles, account_profile_summaries

TEST_MARKERS = ("测试", "test", "smoke")
SAFE_STATUSES = {"draft", "paused", "stopped"}
captured = {}

def capture_provider_call(_session, _provider, prompt, **kwargs):
    captured.update({
        "prompt": prompt,
        "system_prompt": kwargs.get("system_prompt") or "",
        "model_name": "grok-4.5",
        "purpose": kwargs.get("purpose") or "",
    })
    candidate = SimpleNamespace(content="先看看", material_intent="", allow_material=False, intent="", mood="")
    return SimpleNamespace(candidates=[candidate], usage=SimpleNamespace(total_tokens=0))

with SessionLocal() as session:
    tasks = session.scalars(
        select(Task)
        .where(
            Task.type == "group_ai_chat",
            Task.status.in_(SAFE_STATUSES),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.updated_at.desc(), Task.created_at.desc())
    ).all()
    selected = None
    for task in tasks:
        config = dict(task.type_config or {})
        group_id = int(config.get("target_group_id") or 0)
        group = session.get(TgGroup, group_id) if group_id else None
        labels = f"{task.name} {group.title if group else ''}".lower()
        if group and any(marker in labels for marker in TEST_MARKERS):
            selected = (task, group, config)
            break
    if not selected:
        raise RuntimeError("blocked: no safe-status test group_ai_chat task")
    task, group, config = selected
    link = session.scalar(
        select(TgGroupAccount)
        .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
        .where(
            TgGroupAccount.tenant_id == task.tenant_id,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.can_send.is_(True),
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.deleted_at.is_(None),
        )
        .order_by(TgGroupAccount.id.asc())
    )
    if not link:
        raise RuntimeError("blocked: test group has no active linked account")
    account = session.get(TgAccount, link.account_id)
    rows = list(session.scalars(
        select(GroupContextMessage)
        .where(
            GroupContextMessage.tenant_id == task.tenant_id,
            GroupContextMessage.group_id == group.id,
            GroupContextMessage.is_bot.is_(False),
        )
        .order_by(GroupContextMessage.sent_at.desc(), GroupContextMessage.id.desc())
        .limit(20)
    ))
    history = "\n".join(f"{row.sender_name}: {row.content}" for row in reversed(rows))
    voice = voice_profile_prompt_details(session, tenant_id=task.tenant_id, account_ids=[account.id])
    profiles = account_profile_summaries(session, task, [account.id])
    config["account_profiles"] = _account_prompt_profiles(profiles, voice, {})
    config["account_personas"] = {str(account.id): account.display_name}
    config["ai_model"] = "grok-4.5"
    config["max_message_length"] = 120
    before = {
        "action_count": session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id)) or 0,
        "attempt_count": session.scalar(
            select(func.count(ExecutionAttempt.id)).join(Action, Action.id == ExecutionAttempt.action_id).where(Action.task_id == task.id)
        ) or 0,
        "remote_message_count": session.scalar(
            select(func.count(ExecutionAttempt.id))
            .join(Action, Action.id == ExecutionAttempt.action_id)
            .where(Action.task_id == task.id, ExecutionAttempt.remote_message_id != "")
        ) or 0,
    }
    ai_generator._generate_with_provider_candidates = capture_provider_call
    ai_generator.generate_group_messages(
        session,
        task.tenant_id,
        config,
        count=1,
        target_label=group.title,
        history=history,
    )
    if not captured.get("prompt"):
        raise RuntimeError("blocked: production prompt was not captured")
    output = {
        "task_id": task.id,
        "task_name": task.name,
        "task_status": task.status,
        "tenant_id": task.tenant_id,
        "group_id": group.id,
        "group_title": group.title,
        "account_id": account.id,
        "account_display_name": account.display_name,
        "context_message_count": len(rows),
        "before": before,
        **captured,
    }
    print(json.dumps(output, ensure_ascii=False))
    session.rollback()
PY
ssh codex_usa01_server 'umask 077; cat > /tmp/tgyunying-grok-dry-run/capture.json' < /tmp/tgyunying-grok-capture.local.json
rm -f /tmp/tgyunying-grok-capture.local.json
```

Expected: `capture.json` contains non-empty `prompt` and `system_prompt`, `task_status` is safe, group/task is explicitly test-named, one active account is recorded, and `before` contains the three baseline counts. The process-local interception prevents the configured MiniMax Provider from being called.

### Task 5: Execute Grok CLI and validate the result

**Files:**
- Create temporarily on production host: `/tmp/tgyunying-grok-dry-run/result.json`
- Create temporarily on production host: `/tmp/tgyunying-grok-dry-run/stderr.log`
- Create temporarily on production host: `/tmp/tgyunying-grok-dry-run/exit_status`

- [ ] **Step 1: Execute one headless Grok request with tools disabled**

Run:

```bash
ssh codex_usa01_server '
  set -u
  GROK_BIN="$HOME/.grok/bin/grok"
  RUN_DIR=/tmp/tgyunying-grok-dry-run
  PROMPT="$(python3 -c '\''import json; print(json.load(open("/tmp/tgyunying-grok-dry-run/capture.json"))["prompt"])'\'')"
  SYSTEM_PROMPT="$(python3 -c '\''import json; print(json.load(open("/tmp/tgyunying-grok-dry-run/capture.json"))["system_prompt"])'\'')"
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  started_epoch="$(date +%s)"
  set +e
  "$GROK_BIN" \
    --model grok-4.5 \
    --single "$PROMPT" \
    --verbatim \
    --system-prompt-override "$SYSTEM_PROMPT" \
    --no-memory \
    --no-subagents \
    --disable-web-search \
    --permission-mode dontAsk \
    --cwd "$RUN_DIR" \
    --output-format json \
    > "$RUN_DIR/result.json" \
    2> "$RUN_DIR/stderr.log"
  status=$?
  ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  ended_epoch="$(date +%s)"
  printf "%s\n" "$status" > "$RUN_DIR/exit_status"
  printf "%s\n" "$started_at" > "$RUN_DIR/started_at"
  printf "%s\n" "$ended_at" > "$RUN_DIR/ended_at"
  printf "%s\n" "$((ended_epoch - started_epoch))" > "$RUN_DIR/duration_seconds"
  exit "$status"
'
```

Expected: command exit 0; `result.json` is valid JSON with non-empty `text`; `stopReason` is `EndTurn`; stderr contains no authentication, subscription, credit, or inference failure. Any nonzero exit is `blocked`, even if partial text exists.

- [ ] **Step 2: Read a redacted result summary**

Run:

```bash
ssh codex_usa01_server '
  python3 - <<"PY"
import json
from pathlib import Path

run_dir = Path("/tmp/tgyunying-grok-dry-run")
capture = json.loads((run_dir / "capture.json").read_text())
result = json.loads((run_dir / "result.json").read_text())
stderr = (run_dir / "stderr.log").read_text()
print(json.dumps({
    "task_id": capture["task_id"],
    "task_status": capture["task_status"],
    "tenant_id": capture["tenant_id"],
    "group_id": capture["group_id"],
    "group_title": capture["group_title"],
    "account_id": capture["account_id"],
    "model": capture["model_name"],
    "context_message_count": capture["context_message_count"],
    "text": result.get("text", ""),
    "stop_reason": result.get("stopReason", ""),
    "started_at": (run_dir / "started_at").read_text().strip(),
    "ended_at": (run_dir / "ended_at").read_text().strip(),
    "duration_seconds": int((run_dir / "duration_seconds").read_text().strip()),
    "stderr_tail": stderr[-1000:],
}, ensure_ascii=False))
PY
'
```

Expected: summary identifies only non-secret production object IDs/names and contains the generated text. It must not print Prompt contents, phone data, Telegram session, OAuth token, or provider keys.

### Task 6: Verify zero sending side effects and clean up

**Files:**
- Delete temporary production directory after evidence capture: `/tmp/tgyunying-grok-dry-run`
- No database writes

- [ ] **Step 1: Recount task actions, attempts, and remote message IDs**

Run:

```bash
capture_json="$(ssh codex_usa01_server 'cat /tmp/tgyunying-grok-dry-run/capture.json')"
task_id="$(printf '%s' "$capture_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')"
before_json="$(printf '%s' "$capture_json" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["before"], sort_keys=True))')"
after_json="$(ssh codex_usa01_server "docker exec -e TASK_ID='$task_id' -i tgyunying-backend python -" <<'PY'
import json
import os
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Action, ExecutionAttempt

task_id = os.environ["TASK_ID"]
with SessionLocal() as session:
    after = {
        "action_count": session.scalar(select(func.count(Action.id)).where(Action.task_id == task_id)) or 0,
        "attempt_count": session.scalar(
            select(func.count(ExecutionAttempt.id)).join(Action, Action.id == ExecutionAttempt.action_id).where(Action.task_id == task_id)
        ) or 0,
        "remote_message_count": session.scalar(
            select(func.count(ExecutionAttempt.id))
            .join(Action, Action.id == ExecutionAttempt.action_id)
            .where(Action.task_id == task_id, ExecutionAttempt.remote_message_id != "")
        ) or 0,
    }
    print(json.dumps(after, sort_keys=True))
PY
 )"
python3 - "$before_json" "$after_json" <<'PY'
import json
import sys

before = json.loads(sys.argv[1])
after = json.loads(sys.argv[2])
print(json.dumps({"before": before, "after": after, "equal": before == after}, sort_keys=True))
raise SystemExit(0 if before == after else 1)
PY
```

Expected: `equal` is `true`. If any count changes, stop and mark `blocked`; inspect the task before cleanup.

- [ ] **Step 2: Recheck production health**

Run:

```bash
ssh codex_usa01_server '
  docker ps --filter name=tgyunying --format "{{.Names}} {{.Status}}"
  curl -fsS --max-time 10 http://127.0.0.1:18090/api/health
'
curl -fsS --max-time 15 https://tgyunying.telema.cn/api/health
```

Expected: production containers remain healthy; local and public health return `{"status":"ok"}`.

- [ ] **Step 3: Remove prompt-bearing temporary files after collecting the result summary**

Run:

```bash
ssh codex_usa01_server 'rm -rf /tmp/tgyunying-grok-dry-run'
```

Expected: directory no longer exists. Keep the installed Grok CLI and its authorized account only because the approved design is testing deployment viability; no application process references it.

- [ ] **Step 4: Report layered acceptance**

Report:

```text
install: pass|blocked
authentication: pass|blocked
production_context: pass|blocked|unproven
prompt_capture: pass|blocked|unproven
grok_generation: pass|blocked
zero_send_side_effect: pass|blocked
production_health: pass|blocked
overall: pass|blocked|unproven
```

Expected: `overall=pass` only when every layer is `pass`; a successful CLI fixed-Prompt answer without production context and zero-send proof is `unproven`.
