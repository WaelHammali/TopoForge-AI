"""Workflow routing decisions.

Pure functions, so every branch in the graph is directly testable without running it.
"""

from __future__ import annotations

import pytest

from src.infrastructure.orchestration.langgraph.routing import (
    route_after_apply,
    route_after_approval,
    route_after_cloud_validation,
    route_after_completeness,
    route_after_generation,
    route_after_input_mode,
    route_after_network_validation,
    route_configuration_stage,
)
from src.infrastructure.orchestration.langgraph.state import (
    InputMode,
    Stage,
    initial_state,
)


class TestInitialState:
    def test_image_mode_requires_an_image(self) -> None:
        with pytest.raises(ValueError, match="image_uri"):
            initial_state(
                project_id="p", architecture_id="a", workflow_id="w", input_mode=InputMode.IMAGE
            )

    def test_prompt_mode_requires_a_prompt(self) -> None:
        with pytest.raises(ValueError, match="user_prompt"):
            initial_state(
                project_id="p", architecture_id="a", workflow_id="w", input_mode=InputMode.PROMPT
            )

    def test_the_thread_id_is_stable_so_a_workflow_can_resume(self) -> None:
        first = initial_state(
            project_id="p1",
            architecture_id="a1",
            workflow_id="w1",
            input_mode=InputMode.PROMPT,
            user_prompt="x",
        )
        second = initial_state(
            project_id="p1",
            architecture_id="a1",
            workflow_id="w2",
            input_mode=InputMode.PROMPT,
            user_prompt="x",
        )
        assert first["thread_id"] == second["thread_id"] == "p1:a1"

    def test_approval_starts_false(self) -> None:
        state = initial_state(
            project_id="p",
            architecture_id="a",
            workflow_id="w",
            input_mode=InputMode.PROMPT,
            user_prompt="x",
        )
        assert state["deployment_approved"] is False
        assert state["current_stage"] == Stage.STARTED.value

    def test_no_image_bytes_are_carried_in_state(self) -> None:
        state = initial_state(
            project_id="p",
            architecture_id="a",
            workflow_id="w",
            input_mode=InputMode.IMAGE,
            image_uri="s3://bucket/diagram.png",
        )
        assert state["image_uri"] == "s3://bucket/diagram.png"
        assert "image_bytes" not in state


class TestInputRouting:
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("image", "image"), ("prompt", "prompt"), ("revision", "prompt"), ("nonsense", "fail")],
    )
    def test_modes(self, mode: str, expected: str) -> None:
        assert route_after_input_mode({"input_mode": mode}) == expected

    def test_a_missing_mode_fails_rather_than_defaulting(self) -> None:
        assert route_after_input_mode({}) == "fail"


class TestCompletenessRouting:
    def test_no_questions_proceeds(self) -> None:
        assert route_after_completeness({}) == "enrich"

    def test_an_unanswered_required_question_pauses(self) -> None:
        state = {"clarification_questions": [{"id": "q1", "required": True}]}
        assert route_after_completeness(state) == "clarify"

    def test_an_answered_question_is_not_asked_again_on_resume(self) -> None:
        state = {
            "clarification_questions": [{"id": "q1", "required": True}],
            "clarification_answers": [{"question_id": "q1", "value": "10.0.0.1/24"}],
        }
        assert route_after_completeness(state) == "enrich"

    def test_a_skipped_answer_still_counts_as_answered(self) -> None:
        state = {
            "clarification_questions": [{"id": "q1", "required": True}],
            "clarification_answers": [{"question_id": "q1", "skipped": True}],
        }
        assert route_after_completeness(state) == "enrich"

    def test_optional_questions_do_not_pause(self) -> None:
        state = {"clarification_questions": [{"id": "q1", "required": False}]}
        assert route_after_completeness(state) == "enrich"


class TestTheGateIntoTheRAG:
    def test_a_clean_validation_proceeds(self) -> None:
        state = {"network_validation": {"errors": []}, "network_architecture": {}}
        assert route_after_network_validation(state) == "translate"

    def test_errors_block(self) -> None:
        state = {"network_validation": {"errors": [{"code": "duplicate_ip"}]}}
        assert route_after_network_validation(state) == "invalid"

    def test_warnings_do_not_block(self) -> None:
        state = {
            "network_validation": {"errors": [], "warnings": [{"code": "no_routing"}]},
            "network_architecture": {},
        }
        assert route_after_network_validation(state) == "translate"

    def test_unanswered_required_information_returns_to_clarification(self) -> None:
        state = {
            "network_validation": {"errors": []},
            "network_architecture": {"missing_information": [{"need": "REQUIRED"}]},
        }
        assert route_after_network_validation(state) == "clarify"

    def test_optional_gaps_do_not_block(self) -> None:
        state = {
            "network_validation": {"errors": []},
            "network_architecture": {"missing_information": [{"need": "OPTIONAL"}]},
        }
        assert route_after_network_validation(state) == "translate"


class TestCloudValidationRouting:
    def test_a_valid_non_empty_architecture_generates(self) -> None:
        state = {
            "cloud_validation": {"errors": []},
            "cloud_architecture": {"infrastructure": {"networks": [{"id": "v"}]}},
        }
        assert route_after_cloud_validation(state) == "generate"

    def test_errors_block_generation(self) -> None:
        state = {
            "cloud_validation": {"errors": [{"code": "subnet_outside_network"}]},
            "cloud_architecture": {"infrastructure": {"networks": [{"id": "v"}]}},
        }
        assert route_after_cloud_validation(state) == "invalid"

    def test_an_empty_translation_does_not_generate_an_empty_project(self) -> None:
        state = {"cloud_validation": {"errors": []}, "cloud_architecture": {"infrastructure": {}}}
        assert route_after_cloud_validation(state) == "invalid"


class TestConfigurationStage:
    def test_ansible_runs_when_there_is_configuration(self) -> None:
        state = {
            "cloud_architecture": {
                "configuration": {"hosts": [{"id": "h"}], "roles": [{"id": "r"}]}
            }
        }
        assert route_configuration_stage(state) == "ansible"

    def test_ansible_is_skipped_for_a_pure_infrastructure_translation(self) -> None:
        state = {"cloud_architecture": {"configuration": {"hosts": [], "roles": []}}}
        assert route_configuration_stage(state) == "skip"

    def test_hosts_without_roles_do_not_trigger_an_empty_playbook(self) -> None:
        state = {"cloud_architecture": {"configuration": {"hosts": [{"id": "h"}], "roles": []}}}
        assert route_configuration_stage(state) == "skip"


class TestGenerationRouting:
    def test_both_validations_must_pass(self) -> None:
        state = {
            "terraform_project_uri": "s3://x/terraform",
            "terraform_validation": {"errors": []},
            "ansible_validation": {"errors": []},
        }
        assert route_after_generation(state) == "review"

    def test_a_terraform_failure_blocks(self) -> None:
        state = {
            "terraform_project_uri": "s3://x",
            "terraform_validation": {"errors": [{"code": "terraform_validate_failed"}]},
        }
        assert route_after_generation(state) == "invalid"

    def test_an_ansible_failure_blocks(self) -> None:
        state = {
            "terraform_project_uri": "s3://x",
            "terraform_validation": {"errors": []},
            "ansible_validation": {"errors": [{"code": "syntax"}]},
        }
        assert route_after_generation(state) == "invalid"

    def test_a_missing_terraform_project_blocks(self) -> None:
        assert route_after_generation({"terraform_validation": {"errors": []}}) == "invalid"


class TestApprovalRouting:
    """The last gate before real infrastructure. Everything ambiguous means reject."""

    def test_an_explicit_approval_applies(self) -> None:
        state = {"deployment_approved": True, "approval": {"decision": "approved"}}
        assert route_after_approval(state) == "apply"

    def test_an_empty_state_rejects(self) -> None:
        assert route_after_approval({}) == "reject"

    def test_an_explicit_rejection_rejects(self) -> None:
        state = {"deployment_approved": False, "approval": {"decision": "rejected"}}
        assert route_after_approval(state) == "reject"

    @pytest.mark.parametrize("value", ["true", "yes", 1, "approved", [], {}, None])
    def test_only_the_boolean_true_counts_as_approval(self, value: object) -> None:
        # A truthy-looking value from a malformed resume must never deploy.
        state = {"deployment_approved": value, "approval": {"decision": "approved"}}
        assert route_after_approval(state) == "reject"

    def test_the_flag_alone_is_not_enough_without_a_record(self) -> None:
        assert route_after_approval({"deployment_approved": True}) == "reject"

    def test_a_record_alone_is_not_enough_without_the_flag(self) -> None:
        assert route_after_approval({"approval": {"decision": "approved"}}) == "reject"


class TestPostApplyRouting:
    def test_configuration_follows_apply_when_there_is_work(self) -> None:
        state = {
            "cloud_architecture": {
                "configuration": {"hosts": [{"id": "h"}], "roles": [{"id": "r"}]}
            }
        }
        assert route_after_apply(state) == "configure"

    def test_otherwise_the_workflow_completes(self) -> None:
        assert route_after_apply({"cloud_architecture": {"configuration": {}}}) == "complete"

    def test_an_error_fails(self) -> None:
        assert route_after_apply({"errors": [{"code": "apply_failed"}]}) == "fail"
