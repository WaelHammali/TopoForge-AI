"""Deployment domain: plans, approvals and state.

The central object here is :class:`ApprovalRecord`. Infrastructure is never mutated
without one, and an approval is bound to *one specific plan* by checksum. If the plan
changes after approval — a regenerated project, a drifted remote state — the approval
stops matching and apply is refused. That is the whole point: approving a plan is not the
same as approving a future plan.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.domain.common.identifiers import new_id

__all__ = [
    "ApprovalDecision",
    "ApprovalRecord",
    "DeploymentPlan",
    "DeploymentStage",
    "DeploymentState",
    "DeploymentTarget",
    "PlannedChange",
    "ResourceChangeAction",
]


class DeploymentTarget(BaseModel):
    """Where a deployment goes. Never carries credentials."""

    model_config = ConfigDict(frozen=True)

    provider: str = "aws"
    region: str = "eu-west-1"
    account_alias: str | None = None
    #: Role to assume. Preferred over static keys; the value is an ARN, not a secret.
    role_arn: str | None = None
    environment: str = "development"


class ResourceChangeAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    REPLACE = "replace"
    DELETE = "delete"
    NO_OP = "no_op"
    READ = "read"

    @property
    def is_destructive(self) -> bool:
        """Changes a reviewer must look at twice."""
        return self in {ResourceChangeAction.DELETE, ResourceChangeAction.REPLACE}


class PlannedChange(BaseModel):
    """One resource change, normalised away from any tool's plan format."""

    model_config = ConfigDict(frozen=True)

    address: str
    action: ResourceChangeAction
    resource_type: str | None = None
    resource_name: str | None = None
    #: Cloud architecture resource this change corresponds to, when resolvable.
    cloud_resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DeploymentPlan(BaseModel):
    """A reviewable plan. Produced without mutating anything."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id("plan"))
    deployment_id: str | None = None
    target: DeploymentTarget = Field(default_factory=DeploymentTarget)
    changes: tuple[PlannedChange, ...] = ()
    #: Raw human-readable plan output, for the deployment view.
    summary_text: str | None = None
    #: Where the binary plan file lives, when the tool produced one.
    plan_artifact_uri: str | None = None
    #: Fingerprint of the generated project this plan was produced from.
    project_fingerprint: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    warnings: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys((action.value for action in ResourceChangeAction), 0)
        for change in self.changes:
            counts[change.action.value] += 1
        return counts

    @property
    def has_destructive_changes(self) -> bool:
        return any(change.action.is_destructive for change in self.changes)

    @property
    def is_empty(self) -> bool:
        return all(change.action is ResourceChangeAction.NO_OP for change in self.changes)

    def checksum(self) -> str:
        """Stable hash of what this plan actually does.

        Deliberately excludes the plan id and timestamp, and includes the project
        fingerprint: regenerating identical code and re-planning against unchanged state
        yields the same checksum, so an approval survives a harmless re-plan but not a
        real change.
        """
        digest = hashlib.sha256()
        digest.update((self.project_fingerprint or "").encode())
        for change in sorted(self.changes, key=lambda item: item.address):
            digest.update(f"\0{change.address}\0{change.action.value}".encode())
        return digest.hexdigest()


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRecord(BaseModel):
    """A human decision about one specific plan.

    Persisted before any apply runs, so there is always an audit trail that names who
    authorised what, and exactly which plan they saw.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id("approval"))
    deployment_id: str
    plan_id: str
    #: Checksum of the plan at the moment of the decision.
    plan_checksum: str
    decision: ApprovalDecision
    actor: str
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    comment: str | None = None
    #: Recorded for the audit trail.
    source_ip: str | None = None
    user_agent: str | None = None

    @property
    def is_approved(self) -> bool:
        return self.decision is ApprovalDecision.APPROVED

    def authorises(self, plan: DeploymentPlan) -> bool:
        """Does this record authorise applying *this* plan, right now?

        Every condition must hold: it is an approval, for this plan, and the plan's
        content has not changed since it was seen.
        """
        return (
            self.is_approved
            and self.plan_id == plan.id
            and self.plan_checksum == plan.checksum()
        )


class DeploymentStage(str, Enum):
    """Stages shown in the deployment view, in execution order."""

    CLOUD_ARCHITECTURE = "cloud_architecture"
    TERRAFORM_GENERATED = "terraform_generated"
    TERRAFORM_VALIDATED = "terraform_validated"
    ANSIBLE_GENERATED = "ansible_generated"
    ANSIBLE_VALIDATED = "ansible_validated"
    PLAN_READY = "plan_ready"
    AWAITING_APPROVAL = "awaiting_approval"
    INFRASTRUCTURE_APPLIED = "infrastructure_applied"
    INVENTORY_GENERATED = "inventory_generated"
    CONFIGURATION_APPLIED = "configuration_applied"
    COMPLETE = "complete"


#: Canonical stage order. The UI renders this; the workflow follows it.
DEPLOYMENT_STAGE_ORDER: tuple[DeploymentStage, ...] = (
    DeploymentStage.CLOUD_ARCHITECTURE,
    DeploymentStage.TERRAFORM_GENERATED,
    DeploymentStage.TERRAFORM_VALIDATED,
    DeploymentStage.ANSIBLE_GENERATED,
    DeploymentStage.ANSIBLE_VALIDATED,
    DeploymentStage.PLAN_READY,
    DeploymentStage.AWAITING_APPROVAL,
    DeploymentStage.INFRASTRUCTURE_APPLIED,
    DeploymentStage.INVENTORY_GENERATED,
    DeploymentStage.CONFIGURATION_APPLIED,
    DeploymentStage.COMPLETE,
)


class DeploymentState(BaseModel):
    """Where a deployment has got to."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id("deploy"))
    project_id: str | None = None
    cloud_architecture_id: str | None = None
    network_architecture_id: str | None = None
    target: DeploymentTarget = Field(default_factory=DeploymentTarget)
    stage: DeploymentStage = DeploymentStage.CLOUD_ARCHITECTURE
    completed_stages: tuple[DeploymentStage, ...] = ()
    plan: DeploymentPlan | None = None
    approval: ApprovalRecord | None = None
    #: Storage URIs of the generated projects. Content lives in object storage.
    terraform_project_uri: str | None = None
    ansible_project_uri: str | None = None
    inventory_uri: str | None = None
    failed: bool = False
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _approval_matches_plan(self) -> Self:
        """An approval can never be attached to a different plan than the one it names."""
        if self.approval is not None and self.plan is not None:
            if self.approval.plan_id != self.plan.id:
                raise ValueError(
                    "approval references a different plan than the one on this deployment"
                )
        return self

    @property
    def may_apply(self) -> bool:
        """The gate. Nothing applies without a plan and a matching approval for it."""
        return (
            self.plan is not None
            and self.approval is not None
            and self.approval.authorises(self.plan)
        )

    def advanced_to(self, stage: DeploymentStage) -> DeploymentState:
        completed = tuple(
            dict.fromkeys((*self.completed_stages, self.stage))
        ) if self.stage != stage else self.completed_stages
        return self.model_copy(
            update={
                "stage": stage,
                "completed_stages": completed,
                "updated_at": datetime.now(UTC),
            }
        )

    def progress(self) -> list[dict[str, Any]]:
        """Stage list with status, for the deployment view."""
        current_index = DEPLOYMENT_STAGE_ORDER.index(self.stage)
        rows: list[dict[str, Any]] = []
        for index, stage in enumerate(DEPLOYMENT_STAGE_ORDER):
            if self.failed and index == current_index:
                status = "failed"
            elif index < current_index or stage in self.completed_stages:
                status = "complete"
            elif index == current_index:
                status = "active"
            else:
                status = "pending"
            rows.append({"stage": stage.value, "status": status})
        return rows
