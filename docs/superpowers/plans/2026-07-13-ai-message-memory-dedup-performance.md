# AI 活群消息记忆去重性能治理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变租户级跨群去重语义的前提下，消除 `ai_group_message_memory` 完整 ORM 大行扫描和无合适索引导致的生产长事务，使真实 Planner / Dispatcher 批次恢复推进。

**Architecture:** `_window_memories` 只查询并构造不可变的 `id/normalized_text/raw_text` 轻量记录，后续相似度算法与窗口顺序保持不变。模型和 Alembic 新增 `(tenant_id, status, planned_at DESC)` 索引；PostgreSQL 生产升级/降级使用 `CONCURRENTLY`，其他测试方言使用等价普通索引，不吞掉任何迁移错误。

**Tech Stack:** Python 3.12、SQLAlchemy 2、Alembic、PostgreSQL、pytest、GitHub Actions、Docker Compose。

---

## 文件边界

- Create: `backend/tests/test_ai_group_message_memory_query_shape.py` — 轻投影、跨群范围、模型索引与迁移契约。
- Create: `backend/tests/test_ai_group_message_memory_postgres.py` — 40,741 行以上真实 PostgreSQL 查询/扫描性能门禁。
- Create: `backend/migrations/versions/0091_ai_message_memory_dedup_index.py` — 单一新增索引迁移。
- Modify: `backend/app/services/task_center/ai_message_memory.py:419-445` — 轻量记录和窗口查询。
- Modify: `backend/app/models/task_center.py:313-326` — 模型索引元数据。
- Modify: `backend/tests/test_merge_integrity.py:13-35` — 新 Alembic head。
- Modify: `docs/00-index/project-structure-index.md` — 新迁移/测试和最终文件行数。
- Modify: `docs/05-implementation/multi-agent-practice/agent-status-board.md` — dev、QA、Product、Release Gate 状态。
- Modify: `docs/05-implementation/multi-agent-practice/runs/2026-07-13-ai-group-planner-scale-fix.md` — 本地、发布和生产证据。

### Task 1: 用红测锁定轻投影与租户级跨群语义

**Files:**
- Create: `backend/tests/test_ai_group_message_memory_query_shape.py`
- Modify: `backend/app/services/task_center/ai_message_memory.py:1-45,350-445`

- [ ] **Step 1: 写查询形状红测**

创建测试文件，使用 SQLite 真实执行并捕获 SQL：

```python
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiGroupMessageMemory
from app.services._common import _now
from app.services.task_center.ai_message_memory import _window_memories


pytestmark = pytest.mark.no_postgres


def test_window_memories_selects_only_similarity_fields_across_groups() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().lower().startswith("select") and "ai_group_message_memory" in statement:
            statements.append(statement.lower())

    with Session(engine) as session:
        session.add(
            AiGroupMessageMemory(
                id="memory-across-group",
                tenant_id=1,
                group_id=22,
                raw_text="花花老师服务挺稳",
                normalized_text="<person>服务挺稳",
                status="success",
                planned_at=_now(),
                result={"large": "x" * 4096},
            )
        )
        session.commit()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            rows = _window_memories(session, 1, 999, _now() - timedelta(days=7))
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    assert [(row.id, row.normalized_text, row.raw_text) for row in rows] == [
        ("memory-across-group", "<person>服务挺稳", "花花老师服务挺稳")
    ]
    assert len(statements) == 1
    statement = statements[0]
    assert "ai_group_message_memory.id" in statement
    assert "ai_group_message_memory.normalized_text" in statement
    assert "ai_group_message_memory.raw_text" in statement
    assert "ai_group_message_memory.result" not in statement
    assert "ai_group_message_memory.group_id" not in statement
```

再增加失败显式传播测试：

```python
def test_window_memories_propagates_database_failures() -> None:
    class FailingSession:
        def execute(self, _statement):
            raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        _window_memories(FailingSession(), 1, 22, _now() - timedelta(days=7))
```

- [ ] **Step 2: 运行红测并确认失败原因**

Run:

```bash
cd backend
perl -e 'my $seconds = shift @ARGV; alarm $seconds; exec { $ARGV[0] } @ARGV or die $!' 60 /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest tests/test_ai_group_message_memory_query_shape.py -q
```

Expected: FAIL，因为当前 SELECT 包含 `result` 和完整 ORM 列。

- [ ] **Step 3: 实现不可变轻量记录和三列查询**

增加 SQLAlchemy 不可变结果行类型导入：

```python
from sqlalchemy.engine import Row
```

将 `_find_duplicate` 返回类型改成：

```python
) -> tuple[AiGroupMessageMemory | Row | None, str]:
```

将相似度窗口相关函数改为：

```python
def _find_similar_duplicate(
    session: Session,
    tenant_id: int,
    group_id: int,
    normalized: str,
    now: datetime,
    exclude_id: str = "",
) -> Row | None:
```

```python
def _find_semantic_duplicate(
    session: Session,
    tenant_id: int,
    group_id: int,
    normalized: str,
    now: datetime,
    exclude_id: str = "",
) -> Row | None:
```

用以下实现替换窗口查询和相似度入参类型：

```python
def _window_memories(session: Session, tenant_id: int, group_id: int, cutoff: datetime, exclude_id: str = "") -> list[Row]:
    return list(
        session.execute(
            select(
                AiGroupMessageMemory.id,
                AiGroupMessageMemory.normalized_text,
                AiGroupMessageMemory.raw_text,
            )
            .where(
                AiGroupMessageMemory.tenant_id == tenant_id,
                AiGroupMessageMemory.status.in_(DEDUP_STATUSES),
                AiGroupMessageMemory.planned_at >= cutoff,
                AiGroupMessageMemory.id != exclude_id,
            )
            .order_by(AiGroupMessageMemory.planned_at.desc())
        )
    )


def _first_similar_memory(
    rows: list[Row],
    normalized: str,
    threshold: float,
) -> Row | None:
```

保留未使用的 `group_id` 参数以维持既有内部接口；禁止把它加入 SQL `WHERE`。

- [ ] **Step 4: 运行轻投影和既有租户范围回归**

Run:

```bash
cd backend
perl -e 'my $seconds = shift @ARGV; alarm $seconds; exec { $ARGV[0] } @ARGV or die $!' 60 /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest tests/test_ai_group_message_memory_query_shape.py tests/test_ai_group_message_memory_tenant_scope.py tests/test_ai_group_message_memory_normalization.py tests/test_ai_group_message_memory.py -q
```

Expected: PASS；跨群 exact/semantic 仍阻断，SQL 只包含三列。

- [ ] **Step 5: 检查文件硬限制并提交**

Run:

```bash
wc -l backend/app/services/task_center/ai_message_memory.py
git diff --check
```

Expected: 服务文件不超过 500 行，diff check 通过。

Commit:

```bash
git add backend/app/services/task_center/ai_message_memory.py backend/tests/test_ai_group_message_memory_query_shape.py
git commit -m "fix: project AI message memory dedupe rows"
```

### Task 2: 用红测锁定复合索引和并发迁移

**Files:**
- Modify: `backend/tests/test_ai_group_message_memory_query_shape.py`
- Modify: `backend/app/models/task_center.py:313-326`
- Create: `backend/migrations/versions/0091_ai_message_memory_dedup_index.py`
- Modify: `backend/tests/test_merge_integrity.py:13-35`

- [ ] **Step 1: 增加模型与迁移红测**

在查询形状测试文件中增加 imports 与 helper：

```python
import importlib.util
from contextlib import contextmanager
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, inspect


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0091_ai_message_memory_dedup_index.py"
INDEX_NAME = "ix_ai_group_message_memory_tenant_status_planned"


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0091_ai_message_memory_index", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
```

增加以下测试：

```python
def test_message_memory_model_declares_tenant_status_planned_index() -> None:
    index = next(index for index in AiGroupMessageMemory.__table__.indexes if index.name == INDEX_NAME)
    compiled = [str(expression) for expression in index.expressions]
    assert compiled == [
        "ai_group_message_memory.tenant_id",
        "ai_group_message_memory.status",
        "planned_at DESC",
    ]


def test_message_memory_index_migration_uses_concurrent_postgres_ddl() -> None:
    source = MIGRATION_PATH.read_text()
    assert 'revision = "0091_ai_memory_index"' in source
    assert 'down_revision = "0090_ai_group_fallback"' in source
    assert "autocommit_block" in source
    assert "CREATE INDEX CONCURRENTLY" in source
    assert "DROP INDEX CONCURRENTLY" in source
    assert INDEX_NAME in source


def test_message_memory_index_migration_upgrades_and_downgrades_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = MetaData()
    Table(
        "ai_group_message_memory",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("tenant_id", Integer, nullable=False),
        Column("status", String(40), nullable=False),
        Column("planned_at", DateTime, nullable=False),
    )
    metadata.create_all(engine)
    migration = _migration_module()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert INDEX_NAME in {item["name"] for item in inspect(connection).get_indexes("ai_group_message_memory")}
        migration.downgrade()
        assert INDEX_NAME not in {item["name"] for item in inspect(connection).get_indexes("ai_group_message_memory")}


def test_message_memory_index_migration_propagates_postgres_ddl_failure(monkeypatch) -> None:
    migration = _migration_module()

    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class Context:
        @contextmanager
        def autocommit_block(self):
            yield

    class FailingOperations:
        def get_bind(self):
            return Bind()

        def get_context(self):
            return Context()

        def execute(self, _statement):
            raise RuntimeError("concurrent index failed")

    monkeypatch.setattr(migration, "op", FailingOperations())
    monkeypatch.setattr(migration, "_has_table", lambda: True)
    monkeypatch.setattr(migration, "_index_names", lambda: set())

    with pytest.raises(RuntimeError, match="concurrent index failed"):
        migration.upgrade()
```

并把 `test_merge_integrity.py` 的 head 期望更新为 `0091_ai_memory_index`。

- [ ] **Step 2: 运行红测确认缺少模型索引和迁移**

Run:

```bash
cd backend
perl -e 'my $seconds = shift @ARGV; alarm $seconds; exec { $ARGV[0] } @ARGV or die $!' 60 /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest tests/test_ai_group_message_memory_query_shape.py tests/test_merge_integrity.py -q
```

Expected: FAIL，因为 0091 和模型索引尚不存在。

- [ ] **Step 3: 在模型元数据增加索引**

在 `AiGroupMessageMemory.__table_args__` 增加：

```python
Index(
    "ix_ai_group_message_memory_tenant_status_planned",
    "tenant_id",
    "status",
    text("planned_at DESC"),
),
```

- [ ] **Step 4: 创建 0091 迁移**

创建：

```python
"""add tenant status planned index for AI message memory

Revision ID: 0091_ai_memory_index
Revises: 0090_ai_group_fallback
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0091_ai_memory_index"
down_revision = "0090_ai_group_fallback"
branch_labels = None
depends_on = None

TABLE_NAME = "ai_group_message_memory"
INDEX_NAME = "ix_ai_group_message_memory_tenant_status_planned"
INDEX_COLUMNS = ["tenant_id", "status", sa.text("planned_at DESC")]


def upgrade() -> None:
    if not _has_table() or INDEX_NAME in _index_names():
        return
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
                    f"ON {TABLE_NAME} (tenant_id, status, planned_at DESC)"
                )
            )
        return
    op.create_index(INDEX_NAME, TABLE_NAME, INDEX_COLUMNS)


def downgrade() -> None:
    if not _has_table() or INDEX_NAME not in _index_names():
        return
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY {INDEX_NAME}"))
        return
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)


def _index_names() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(TABLE_NAME)}


def _has_table() -> bool:
    return TABLE_NAME in sa.inspect(op.get_bind()).get_table_names()
```

`CONCURRENTLY` 只按数据库方言显式选择 DDL；两个分支都直接传播异常，不构成静默 fallback。

- [ ] **Step 5: 运行迁移测试、单 head 和编译检查**

Run:

```bash
cd backend
perl -e 'my $seconds = shift @ARGV; alarm $seconds; exec { $ARGV[0] } @ARGV or die $!' 60 /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest tests/test_ai_group_message_memory_query_shape.py tests/test_merge_integrity.py tests/test_database.py -q
/Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/alembic heads
/Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -m py_compile app/models/task_center.py app/services/task_center/ai_message_memory.py migrations/versions/0091_ai_message_memory_dedup_index.py
```

Expected: 全部 PASS，Alembic 只输出 `0091_ai_memory_index (head)`。

- [ ] **Step 6: 提交索引迁移**

```bash
git add backend/app/models/task_center.py backend/migrations/versions/0091_ai_message_memory_dedup_index.py backend/tests/test_ai_group_message_memory_query_shape.py backend/tests/test_merge_integrity.py
git commit -m "perf: index AI message memory dedupe window"
```

### Task 3: 用真实 PostgreSQL 规模门禁验证方案

**Files:**
- Create: `backend/tests/test_ai_group_message_memory_postgres.py`

- [ ] **Step 1: 编写 40,741 行规模测试**

测试使用独立 tenant，批量写入 `40_741` 条 7 天内历史，每行携带大 `result`，只计时轻量查询和最坏无匹配扫描；测试常量：

```python
TEST_TENANT_ID = 913_715
ROW_COUNT = 40_741
BATCH_SIZE = 2_000
MAX_QUERY_SECONDS = 2.0
MAX_SCAN_SECONDS = 5.0
LARGE_RESULT_TEXT = "message-memory-history" * 64
```

核心断言：

```python
started_at = perf_counter()
rows = _window_memories(session, TEST_TENANT_ID, 999, now_value - timedelta(days=7))
query_elapsed = perf_counter() - started_at

scan_started_at = perf_counter()
duplicate = _first_similar_memory(rows, "完全不相似的唯一候选文本", 0.99)
scan_elapsed = perf_counter() - scan_started_at

assert len(rows) == ROW_COUNT
assert duplicate is None
assert query_elapsed < MAX_QUERY_SECONDS
assert scan_elapsed < MAX_SCAN_SECONDS
```

fixture 必须在 `try/finally` 中按 tenant 删除 `AiGroupMessageMemory` 和 `Tenant`，不能污染后续 PostgreSQL 测试。

- [ ] **Step 2: 运行 PostgreSQL 规模测试**

Run:

```bash
cd backend
perl -e 'my $seconds = shift @ARGV; alarm $seconds; exec { $ARGV[0] } @ARGV or die $!' 60 /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest tests/test_ai_group_message_memory_postgres.py -q
```

Expected: PASS；输出查询和扫描均低于设计阈值。若失败，不提高阈值，回到 SQL / 算法根因继续修复。

- [ ] **Step 3: 运行消息记忆相关 PostgreSQL/SQLite 组合回归**

```bash
cd backend
perl -e 'my $seconds = shift @ARGV; alarm $seconds; exec { $ARGV[0] } @ARGV or die $!' 60 /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest tests/test_ai_group_message_memory_postgres.py tests/test_ai_group_message_memory_query_shape.py tests/test_ai_group_message_memory.py tests/test_ai_group_message_memory_normalization.py tests/test_ai_group_message_memory_tenant_scope.py -q
```

Expected: PASS。

- [ ] **Step 4: 提交规模门禁**

```bash
git add backend/tests/test_ai_group_message_memory_postgres.py
git commit -m "test: gate AI message memory postgres scale"
```

### Task 4: 全量验证、索引同步和 QA/Product 闸门

**Files:**
- Modify: `docs/00-index/project-structure-index.md`
- Modify: `docs/05-implementation/multi-agent-practice/agent-status-board.md`
- Modify: `docs/05-implementation/multi-agent-practice/runs/2026-07-13-ai-group-planner-scale-fix.md`

- [ ] **Step 1: 运行定向业务回归**

```bash
cd backend
perl -e 'my $seconds = shift @ARGV; alarm $seconds; exec { $ARGV[0] } @ARGV or die $!' 60 /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest tests/test_ai_group_message_memory.py tests/test_ai_group_message_memory_normalization.py tests/test_ai_group_message_memory_tenant_scope.py tests/test_ai_group_message_memory_query_shape.py tests/test_ai_group_message_memory_postgres.py tests/test_dispatcher_dataflow.py tests/test_ai_task_limits.py tests/test_channel_comment_config_update.py -q
```

Expected: PASS。

- [ ] **Step 2: 运行全量 no-PostgreSQL 回归**

```bash
cd backend
perl -e 'my $seconds = shift @ARGV; alarm $seconds; exec { $ARGV[0] } @ARGV or die $!' 60 /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest -m no_postgres -q
```

Expected: PASS within 60 seconds。

- [ ] **Step 3: 运行迁移和静态门禁**

```bash
cd backend
/Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/alembic heads
/Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -m compileall -q app tests/test_ai_group_message_memory_query_shape.py tests/test_ai_group_message_memory_postgres.py migrations/versions/0091_ai_message_memory_dedup_index.py
cd ..
git diff --check
```

Expected: 单 head、编译和 diff check 全通过。

- [ ] **Step 4: 更新索引和状态文档**

记录实际文件行数、0091、测试数量和耗时；状态板从 `product_design_complete_spec_review_pending` 进入 `dev_complete_qa_pending`，不能提前写 E4 或 `production_fixed`。

- [ ] **Step 5: 独立 QA 和 Product Acceptance**

QA 必须检查：行为不变量、SQL 投影、迁移并发 DDL、fixture 隔离、回归结果、文件硬限制。Product 必须确认实现未缩小租户级跨群去重且 Release Gate 足够；两者分别写证据，`qa_pass` 不等于 `product_accepted`。

- [ ] **Step 6: 提交验证文档**

```bash
git add docs/00-index/project-structure-index.md docs/05-implementation/multi-agent-practice/agent-status-board.md docs/05-implementation/multi-agent-practice/runs/2026-07-13-ai-group-planner-scale-fix.md
git commit -m "docs: record AI message memory performance verification"
```

### Task 5: 发布并监督到真实生产 E4

**Files:**
- Modify: `docs/05-implementation/multi-agent-practice/agent-status-board.md`
- Modify: `docs/05-implementation/multi-agent-practice/runs/2026-07-13-ai-group-planner-scale-fix.md`

- [ ] **Step 1: 发布 master -> release**

确认分支是 `origin/master` 的线性后继后推送 master；在干净 release worktree 合并 master 并推送 release，触发 `Deploy Production`。不得在生产主机补代码。

- [ ] **Step 2: 验证发布事实**

核对 GitHub Actions checks、build-images、deploy 均成功；真实 `/data/tgyunying/current`、所有相关容器镜像 commit、`alembic current` 与 `0091_ai_memory_index` 一致。

- [ ] **Step 3: 验证生产索引和查询计划**

只读检查 `pg_index.indisvalid=true`，列顺序为 tenant/status/planned_at；用 `EXPLAIN (ANALYZE, BUFFERS)` 验证真实 7 天时间窗查询，记录耗时和扫描方式。不得输出数据库密码或连接密钥。

- [ ] **Step 4: 连续三个周期验证运行时恢复**

每个周期核对 Planner / Dispatcher 完整事务小于 60 秒、无本根因导致的 400 秒事务、锁链消失、覆盖远端确认数持续增长；若仍失败，方案 A 判定失败并重新进入 Product Design，不提高 timeout。

- [ ] **Step 5: 单独验证评论任务**

分别核对太郎日记回复和阿哥日记的 Task 状态、next_run、Action、ExecutionAttempt、错误与 Telegram 远端 ID。只有新 `Action.success + ExecutionAttempt.success + remote_message_id` 才能写评论 `pass`；worker healthy 只能证明运行时存活。

- [ ] **Step 6: 监督完整北京时间自然日矩阵**

对 4 个 `all_accounts_daily` 任务核对全部 `2320` 项任务 × 群 × 账号义务。每项都必须有 Telegram 远端确认；`unknown_after_send`、reserved、Task stats 或本地测试不能计入。未完整覆盖时目标保持 active，继续诊断和修复。

- [ ] **Step 7: 完成 E4 记录**

只有完整 2320 远端矩阵和评论新远端成功都满足后，更新状态板为 `production_fixed`、提交最终证据，并完成持久目标。
