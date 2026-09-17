"""The approval gate.

These tests exist because this is the only thing standing between an LLM-influenced
translation and someone's real AWS account.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.domain.deployment import (
    ApprovalDecision,
    ApprovalRecord,
    DeploymentPlan,
    DeploymentStage,
    DeploymentState,
    PlannedChange,
    ResourceChangeAction,
)


def make_plan(fingerprint: str = "abc123", **kwargs) -> DeploymentPlan:
    return DeploymentPlan(
        project_fingerprint=fingerprint,
        changes=(
            PlannedChange(address="aws_vpc.main", action=ResourceChangeAction.CREATE),
            PlannedChange(address="aws_subnet.a", action=ResourceChangeAction.CREATE),
        ),
        **kwargs,
    )


def approve(plan: DeploymentPlan, actor: str = "wael") -> ApprovalRecord:
    return ApprovalRecord(
        deployment_id="d1",
        plan_id=plan.id,
        plan_checksum=plan.checksum(),
        decision=ApprovalDecision.APPROVED,
        actor=actor,
    )


class TestPlanChecksum:
    def test_checksum_is_stable_across_identical_plans(self) -> None:
        assert make_plan().checksum() == make_plan().checksum()

    def test_checksum_ignores_plan_id_and_timestamp(self) -> None:
        first = make_plan()
        second = make_plan()
        assert first.id != second.id
        assert first.checksum() == second.checksum()

    def test_checksum_is_order_independent(self) -> None:
        plan = make_plan()
        reversed_plan = plan.model_copy(update={"changes": tuple(reversed(plan.changes))})
        assert plan.checksum() == reversed_plan.checksum()

    def test_checksum_changes_when_a_resource_is_added(self) -> None:
        plan = make_plan()
        extended = plan.model_copy(
            update={
                "changes": (
                    *plan.changes,
                    PlannedChange(address="aws_db_instance.x", action=ResourceChangeAction.CREATE),
                )
            }
        )
        assert plan.checksum() != extended.checksum()

    def test_checksum_changes_when_an_action_changes(self) -> None:
        plan = make_plan()
        destructive = plan.model_copy(
            update={
                "changes": (
                    PlannedChange(address="aws_vpc.main", action=ResourceChangeAction.DELETE),
                    plan.changes[1],
                )
            }
        )
        assert plan.checksum() != destructive.checksum()

    def test_checksum_changes_when_the_generated_code_changes(self) -> None:
        # Same planned changes, different generated project: not the same approval.
        assert make_plan("abc").checksum() != make_plan("def").checksum()


class TestApprovalAuthorisation:
    def test_matching_approval_authorises(self) -> None:
        plan = make_plan()
        assert approve(plan).authorises(plan) is True

    def test_rejection_never_authorises(self) -> None:
        plan = make_plan()
        rejection = ApprovalRecord(
            deployment_id="d1",
            plan_id=plan.id,
            plan_checksum=plan.checksum(),
            decision=ApprovalDecision.REJECTED,
            actor="wael",
        )
        assert rejection.authorises(plan) is False

    def test_approval_does_not_carry_over_to_a_changed_plan(self) -> None:
        plan = make_plan()
        approval = approve(plan)
        changed = plan.model_copy(
            update={
                "changes": (
                    PlannedChange(address="aws_vpc.main", action=ResourceChangeAction.DELETE),
                )
            }
        )
        assert approval.authorises(changed) is False

    def test_approval_for_one_plan_does_not_authorise_another(self) -> None:
        first = make_plan()
        second = make_plan()  # identical content, different identity
        assert approve(first).authorises(second) is False

    def test_a_forged_checksum_does_not_help(self) -> None:
        plan = make_plan()
        forged = ApprovalRecord(
            deployment_id="d1",
            plan_id=plan.id,
            plan_checksum="0" * 64,
            decision=ApprovalDecision.APPROVED,
            actor="attacker",
        )
        assert forged.authorises(plan) is False


class TestDeploymentState:
    def test_may_apply_requires_plan_and_matching_approval(self) -> None:
        plan = make_plan()
        assert DeploymentState(plan=plan).may_apply is False
        assert DeploymentState().may_apply is False
        assert DeploymentState(plan=plan, approval=approve(plan)).may_apply is True

    def test_approval_cannot_be_attached_to_a_different_plan(self) -> None:
        with pytest.raises(PydanticValidationError, match="different plan"):
            DeploymentState(plan=make_plan(), approval=approve(make_plan()))

    def test_destructive_changes_are_flagged_for_review(self) -> None:
        plan = make_plan().model_copy(
            update={
                "changes": (
                    PlannedChange(address="aws_instance.x", action=ResourceChangeAction.REPLACE),
                )
            }
        )
        assert plan.has_destructive_changes is True

    def test_empty_plan_is_recognised(self) -> None:
        plan = DeploymentPlan(
            changes=(PlannedChange(address="a", action=ResourceChangeAction.NO_OP),)
        )
        assert plan.is_empty is True

    def test_progress_marks_stages_in_order(self) -> None:
        state = DeploymentState().advanced_to(DeploymentStage.PLAN_READY)
        rows = {row["stage"]: row["status"] for row in state.progress()}
        assert rows["cloud_architecture"] == "complete"
        assert rows["plan_ready"] == "active"
        assert rows["infrastructure_applied"] == "pending"

    def test_failure_is_shown_at_the_stage_that_failed(self) -> None:
        state = DeploymentState(failed=True).advanced_to(DeploymentStage.TERRAFORM_VALIDATED)
        rows = {row["stage"]: row["status"] for row in state.progress()}
        assert rows["terraform_validated"] == "failed"
