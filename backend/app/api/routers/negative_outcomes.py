from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access, resolve_tenant_id
from app.database import get_session
from app.models import NegativeOutcomeCircuitState
from app.services.task_center.negative_outcome_review import circuit_snapshot, review_negative_outcome

router = APIRouter()


class NegativeOutcomeReviewRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    tenant_id: int | None = None
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)


@router.get("/api/negative-outcomes")
def list_negative_outcomes(tenant_id: int | None = None, *, offset: int = Query(0, ge=0),
                          limit: int = Query(100, ge=1, le=100), session: Session = Depends(get_session),
                          current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    resolved = resolve_tenant_id(current_user, tenant_id)
    rows = session.scalars(select(NegativeOutcomeCircuitState).where(
        NegativeOutcomeCircuitState.tenant_id == resolved, NegativeOutcomeCircuitState.level != "normal",
    ).order_by(NegativeOutcomeCircuitState.id).offset(offset).limit(limit))
    return [circuit_snapshot(row) for row in rows]


@router.post("/api/negative-outcomes/{circuit_id}/review")
def review_circuit(circuit_id: str, payload: NegativeOutcomeReviewRequest, *,
                   session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    tenant_id = resolve_tenant_id(current_user, payload.tenant_id)
    try:
        result = review_negative_outcome(session, circuit_id, tenant_id=tenant_id,
            expected_version=payload.expected_version, reason=payload.reason,
            evidence=payload.evidence, actor=current_user.name)
        session.commit()
        return result
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
