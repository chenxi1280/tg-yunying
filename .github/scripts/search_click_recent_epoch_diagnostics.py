from __future__ import annotations

import json
import os

from sqlalchemy import exists, select

from app.database import SessionLocal
from app.models import (
    SearchClickAssignmentEpoch,
    SearchClickSolverCarrierUnitBinding,
    SearchClickSolverProblemSnapshot,
    Task,
)


SAMPLE_LIMIT = 12


def _epoch_row(epoch, snapshot, task_id: str) -> dict:
    problem = dict(snapshot.canonical_problem_payload or {})
    demands = [
        item for item in problem.get("demands", [])
        if item.get("task_id") == task_id
    ]
    obligation_ids = {item.get("obligation_id") for item in demands}
    paths = [
        item for item in problem.get("paths", [])
        if obligation_ids.intersection(item.get("eligible_obligation_ids") or [])
    ]
    return {
        "created_at": epoch.created_at.isoformat(),
        "window_id": epoch.dispatch_claim_window_id,
        "allocation_epoch": epoch.dispatch_allocation_epoch,
        "finalize_status": epoch.finalize_status,
        "outcome": epoch.outcome,
        "matched_unit_count": epoch.matched_unit_count,
        "released_unit_count": epoch.released_unit_count,
        "total_demand_count": len(problem.get("demands", [])),
        "total_path_count": len(problem.get("paths", [])),
        "task_demand_count": len(demands),
        "task_path_count": len(paths),
        "task_path_capacity": sum(
            int(item.get("hard_safe_remaining_capacity") or 0)
            for item in paths
        ),
    }


def main() -> None:
    with SessionLocal() as session:
        task_ids = tuple(filter(None, (
            item.strip() for item in
            os.environ["SEARCH_CLICK_DIAGNOSTIC_TASK_IDS"].split(",")
        )))
        task = session.scalar(select(Task).where(
            Task.id.in_(task_ids), Task.type == "search_click"
        ).limit(1))
        if task is None:
            print(json.dumps({"task_id": None, "epochs": []}))
            return
        task_id = task.id
        rows = session.execute(
            select(SearchClickAssignmentEpoch, SearchClickSolverProblemSnapshot)
            .join(
                SearchClickSolverProblemSnapshot,
                SearchClickSolverProblemSnapshot.search_click_assignment_epoch_id
                == SearchClickAssignmentEpoch.id,
            )
            .where(exists(select(1).where(
                SearchClickSolverCarrierUnitBinding.search_click_solver_snapshot_id
                == SearchClickSolverProblemSnapshot.id,
                SearchClickSolverCarrierUnitBinding.task_id == task_id,
            )))
            .order_by(SearchClickAssignmentEpoch.created_at.desc())
            .limit(SAMPLE_LIMIT)
        )
        payload = {
            "task_id": task_id,
            "last_error": task.last_error if task else None,
            "task_stats": {
                key: value for key, value in dict(task.stats or {}).items()
                if key.startswith("search_click")
                or key in {
                    "hard_safe_attempt_capacity",
                    "projected_eligible_attempt_capacity",
                }
            } if task else {},
            "epochs": [
                _epoch_row(epoch, snapshot, task_id)
                for epoch, snapshot in rows
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
