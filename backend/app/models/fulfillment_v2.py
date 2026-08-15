from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class FulfillmentObligationProjection(Base):
    __tablename__ = "fulfillment_obligation_projections"
    __table_args__ = (
        UniqueConstraint("obligation_type", "obligation_id", name="uq_fop_obligation"),
        Index(
            "ix_fop_claim_ready",
            "tenant_id",
            "work_lane",
            "opened_at",
            "task_id",
            "obligation_id",
            postgresql_where=text("state = 'open'"),
            sqlite_where=text("state = 'open'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_day_ledger_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    obligation_type: Mapped[str] = mapped_column(String(48))
    obligation_id: Mapped[str] = mapped_column(String(255))
    work_lane: Mapped[str] = mapped_column(String(32))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    materialization_version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default="open")
    active_action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class FulfillmentRemoteFact(Base):
    __tablename__ = "fulfillment_remote_facts"
    __table_args__ = (
        UniqueConstraint(
            "remote_mutation_key_hash",
            "gateway_request_hash",
            "fact_kind",
            name="uq_fulfillment_remote_fact_identity",
        ),
        Index("ix_fulfillment_remote_fact_obligation", "obligation_type", "obligation_id"),
        Index(
            "ix_fulfillment_remote_fact_account_timeline",
            "tenant_id",
            "observed_at",
            "action_id",
            postgresql_where=text(
                "fact_kind IN ('remote_message_observed','view_observed','reaction_observed')"
            ),
            sqlite_where=text(
                "fact_kind IN ('remote_message_observed','view_observed','reaction_observed')"
            ),
        ),
        Index(
            "ix_fulfillment_remote_fact_action_typed",
            "tenant_id",
            "action_id",
            "fact_kind",
            "observed_at",
            postgresql_where=text(
                "fact_kind IN ('remote_message_observed','view_observed','reaction_observed')"
            ),
            sqlite_where=text(
                "fact_kind IN ('remote_message_observed','view_observed','reaction_observed')"
            ),
        ),
    )

    fact_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_type: Mapped[str] = mapped_column(String(30))
    task_id: Mapped[str] = mapped_column(String(36))
    task_day_ledger_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    obligation_type: Mapped[str] = mapped_column(String(48))
    obligation_id: Mapped[str] = mapped_column(String(255))
    action_id: Mapped[str] = mapped_column(String(36))
    attempt_id: Mapped[str] = mapped_column(String(36))
    mutation_kind: Mapped[str] = mapped_column(String(40))
    remote_mutation_key_hash: Mapped[str] = mapped_column(String(64))
    gateway_request_hash: Mapped[str] = mapped_column(String(64))
    fact_kind: Mapped[str] = mapped_column(String(40))
    fact_identity_hash: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FulfillmentFactProjectionState(Base):
    __tablename__ = "fulfillment_fact_projection_states"
    __table_args__ = (
        UniqueConstraint("fact_id", "projection_kind", name="uq_fact_projection_kind"),
        Index(
            "ix_fact_projection_pending",
            "next_retry_at",
            "fact_id",
            "projection_kind",
            postgresql_where=text("state IN ('pending','failed')"),
            sqlite_where=text("state IN ('pending','failed')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    fact_id: Mapped[str] = mapped_column(ForeignKey("fulfillment_remote_facts.fact_id", ondelete="CASCADE"))
    projection_kind: Mapped[str] = mapped_column(String(40))
    expected_target_version: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    last_error: Mapped[str] = mapped_column(Text, default="")
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SearchClickAssignment(Base):
    __tablename__ = "search_click_assignments"
    __table_args__ = (
        Index(
            "ix_search_assignments_claim_ready",
            "obligation_deadline_at",
            "id",
            postgresql_where=text("state = 'open'"),
            sqlite_where=text("state = 'open'"),
        ),
        Index(
            "uq_search_assignment_open_obligation",
            "obligation_id",
            unique=True,
            postgresql_where=text(
                "state IN ('open','action_bound','claiming','executing','gateway_unknown')"
            ),
            sqlite_where=text(
                "state IN ('open','action_bound','claiming','executing','gateway_unknown')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    obligation_id: Mapped[str] = mapped_column(String(255))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    authorization_id: Mapped[int] = mapped_column(
        ForeignKey("tg_account_authorizations.id", ondelete="CASCADE")
    )
    keyword_hash: Mapped[str] = mapped_column(String(64))
    proxy_route_id: Mapped[str] = mapped_column(String(80))
    protocol_sample_version: Mapped[str] = mapped_column(String(80))
    resource_snapshot_hash: Mapped[str] = mapped_column(String(64))
    action_id: Mapped[str] = mapped_column(String(36), default="")
    solver_input_hash: Mapped[str] = mapped_column(String(64))
    assignment_version: Mapped[int] = mapped_column(Integer, default=1)
    execution_policy_version: Mapped[int] = mapped_column(Integer, default=1)
    obligation_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    binding_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default="open")
    version: Mapped[int] = mapped_column(Integer, default=1)
    owner_id: Mapped[str] = mapped_column(String(160), default="")
    owner_fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        Index(
            "ix_generation_jobs_claim_ready",
            "state",
            "lease_expires_at",
            "created_at",
            "id",
        ),
        Index(
            "uq_generation_jobs_open_obligation",
            "obligation_type",
            "obligation_id",
            unique=True,
            postgresql_where=text("state IN ('pending','generating','unknown')"),
            sqlite_where=text("state IN ('pending','generating','unknown')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    obligation_type: Mapped[str] = mapped_column(String(48))
    obligation_id: Mapped[str] = mapped_column(String(255))
    generation_sequence: Mapped[int] = mapped_column(Integer)
    context_snapshot_version: Mapped[int] = mapped_column(Integer)
    generation_owner_id: Mapped[str] = mapped_column(String(160), default="")
    generation_lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    job_version: Mapped[int] = mapped_column(Integer, default=1)
    generation_not_before_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    context_snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    assignment_revision: Mapped[int] = mapped_column(Integer, default=1)
    intent_revision: Mapped[int] = mapped_column(Integer, default=1)
    candidate_hash: Mapped[str] = mapped_column(String(64), default="")
    evaluator_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SearchProtocolSession(Base):
    __tablename__ = "search_protocol_sessions"
    __table_args__ = (UniqueConstraint("assignment_id", name="uq_search_protocol_assignment"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("search_click_assignments.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    keyword_id: Mapped[str] = mapped_column(String(64), default="")
    approved_target_ref: Mapped[str] = mapped_column(String(255), default="")
    next_page_identity: Mapped[str] = mapped_column(String(160), default="")
    challenge_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    protocol_sample_version: Mapped[str] = mapped_column(String(80), default="")
    phase: Mapped[str] = mapped_column(String(40), default="assignment_created")
    phase_version: Mapped[int] = mapped_column(Integer, default=1)
    request_identity: Mapped[str] = mapped_column(String(160))
    viewer_cursor: Mapped[str] = mapped_column(String(160), default="")
    page_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    protocol_state: Mapped[dict] = mapped_column(JSON, default=dict)
    owner_id: Mapped[str] = mapped_column(String(160), default="")
    owner_fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecoverableWorkLease(Base):
    __tablename__ = "recoverable_work_leases"
    __table_args__ = (
        UniqueConstraint("work_type", "work_id", name="uq_recoverable_work_lease"),
        Index(
            "ix_recoverable_leases_due",
            "lease_expires_at",
            "work_type",
            "work_id",
            postgresql_where=text("owner_id <> ''"),
            sqlite_where=text("owner_id <> ''"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    work_type: Mapped[str] = mapped_column(String(40))
    work_id: Mapped[str] = mapped_column(String(80))
    owner_id: Mapped[str] = mapped_column(String(160), default="")
    owner_fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    work_version: Mapped[int] = mapped_column(Integer, default=1)


class TaskGroupBotAdmission(Base):
    __tablename__ = "task_group_bot_admissions"
    __table_args__ = (
        UniqueConstraint("task_id", "account_id", "target_group_id", name="uq_task_group_bot_admission"),
        Index(
            "ix_admissions_observation_due",
            "no_prompt_pass_at",
            "task_id",
            "account_id",
            postgresql_where=text("state = 'observing' AND observation_gap = false"),
            sqlite_where=text("state = 'observing' AND observation_gap = 0"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    target_group_id: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="observing")
    requirement_set_version: Mapped[int] = mapped_column(Integer, default=1)
    observation_version: Mapped[int] = mapped_column(Integer, default=1)
    observation_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    no_prompt_pass_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation_gap: Mapped[bool] = mapped_column(Boolean, default=False)
    surface_identity_hash: Mapped[str] = mapped_column(String(64))
    surface_identity: Mapped[dict] = mapped_column(JSON)
    terminal_reason: Mapped[str] = mapped_column(String(80), default="")
    terminal_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class AccountGroupAdmissionFact(Base):
    __tablename__ = "account_group_admission_facts"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "target_group_id",
            "fact_kind",
            "fact_identity_hash",
            name="uq_account_group_admission_fact",
        ),
        Index("ix_account_group_admission_fact_scope", "account_id", "target_group_id", "fact_kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    target_group_id: Mapped[int] = mapped_column(Integer)
    fact_kind: Mapped[str] = mapped_column(String(40))
    fact_identity_hash: Mapped[str] = mapped_column(String(64))
    fact_version: Mapped[int] = mapped_column(Integer, default=1)
    outcome: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "AccountGroupAdmissionFact",
    "FulfillmentFactProjectionState",
    "FulfillmentObligationProjection",
    "FulfillmentRemoteFact",
    "GenerationJob",
    "RecoverableWorkLease",
    "SearchClickAssignment",
    "SearchProtocolSession",
    "TaskGroupBotAdmission",
]
