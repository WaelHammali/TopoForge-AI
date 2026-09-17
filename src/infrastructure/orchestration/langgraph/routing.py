"""Conditional routing.

Pure functions over the workflow state: no I/O, no imports from LangGraph, and therefore
directly unit-testable. Every branch in the graph is decided here, so the workflow's
control flow can be reasoned about — and tested — without running it.
"""

from __future__ import annotations

from typing import Any

from src.infrastructure.orchestration.langgraph.state import InputMode, NetworkWorkflowState

__all__ = [
    "route_after_cloud_validation",
    "route_after_completeness",
    "route_after_generation",
    "route_after_input_mode",
    "route_after_network_validation",
    "route_after_approval",
    "route_configuration_stage",
]


def route_after_input_mode(state: NetworkWorkflowState) -> str:
    """Image extraction or prompt generation."""
    mode = state.get("input_mode")
    if mode == InputMode.IMAGE.value:
        return "image"
    if mode in {InputMode.PROMPT.value, InputMode.REVISION.value}:
        return "prompt"
    return "fail"


def route_after_completeness(state: NetworkWorkflowState) -> str:
    """Pause for the user only when something REQUIRED is missing and unanswered.

    A question that has already been answered must not be asked again on resume, which is
    why answered ids are subtracted rather than the questions simply being counted.
    """
    if state.get("errors"):
        return "fail"

    answered = {
        answer.get("question_id")
        for answer in state.get("clarification_answers", [])
        if isinstance(answer, dict)
    }
    outstanding = [
        question
        for question in state.get("clarification_questions", [])
        if isinstance(question, dict)
        and question.get("required", True)
        and question.get("id") not in answered
    ]
    return "clarify" if outstanding else "enrich"


def route_after_network_validation(state: NetworkWorkflowState) -> str:
    """The one gate into the RAG.

    Errors block. Warnings do not. Unanswered REQUIRED gaps send the user back to
    clarification rather than through to translation.
    """
    validation = state.get("network_validation") or {}
    if validation.get("errors"):
        return "invalid"

    architecture = state.get("network_architecture") or {}
    if any(
        item.get("need") in {"REQUIRED", "ERROR"}
        for item in architecture.get("missing_information", [])
        if isinstance(item, dict)
    ):
        return "clarify"
    return "translate"


def route_after_cloud_validation(state: NetworkWorkflowState) -> str:
    """Generators run only on a valid, non-empty cloud architecture."""
    validation = state.get("cloud_validation") or {}
    if validation.get("errors"):
        return "invalid"
    if not _has_infrastructure(state.get("cloud_architecture") or {}):
        return "invalid"
    return "generate"


def route_configuration_stage(state: NetworkWorkflowState) -> str:
    """Skip Ansible entirely when the translation declares no configuration.

    Generating an empty playbook would be noise, and would make the deployment view claim
    a configuration stage that has nothing to do.
    """
    cloud = state.get("cloud_architecture") or {}
    configuration = cloud.get("configuration") or {}
    has_work = bool(configuration.get("hosts")) and bool(configuration.get("roles"))
    return "ansible" if has_work else "skip"


def route_after_generation(state: NetworkWorkflowState) -> str:
    """Both validations must pass before a plan is produced."""
    for key in ("terraform_validation", "ansible_validation"):
        result = state.get(key)
        if result and result.get("errors"):
            return "invalid"
    if not state.get("terraform_project_uri"):
        return "invalid"
    return "review"


def route_after_approval(state: NetworkWorkflowState) -> str:
    """Apply only on an explicit approval.

    Anything other than a recorded approval — a rejection, an absent decision, a
    truthy-looking value that is not ``True`` — means reject. Defaulting to apply would
    make an interrupted or malformed resume deploy infrastructure.
    """
    if state.get("deployment_approved") is not True:
        return "reject"
    approval = state.get("approval") or {}
    if approval.get("decision") != "approved":
        return "reject"
    return "apply"


def route_after_apply(state: NetworkWorkflowState) -> str:
    """Configure the hosts only if there is configuration to apply."""
    if state.get("errors"):
        return "fail"
    return "configure" if route_configuration_stage(state) == "ansible" else "complete"


def _has_infrastructure(cloud: dict[str, Any]) -> bool:
    infrastructure = cloud.get("infrastructure") or {}
    return any(
        infrastructure.get(key)
        for key in (
            "networks",
            "subnets",
            "compute",
            "databases",
            "load_balancers",
            "gateways",
            "route_tables",
            "security_groups",
        )
    )
