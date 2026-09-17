"""Workflow state.

Strongly structured, and deliberately free of bulk: images, generated files and plan
artifacts live in object storage, and the state carries **references** to them. A graph
checkpoint should be cheap to write on every node transition.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, TypedDict

__all__ = ["InputMode", "NetworkWorkflowState", "Stage", "initial_state"]


class InputMode(str, Enum):
    IMAGE = "image"
    PROMPT = "prompt"
    #: An existing architecture modified conversationally.
    REVISION = "revision"


class Stage(str, Enum):
    """Where the workflow is. Mirrors the node names."""

    STARTED = "started"
    PREPROCESSING = "preprocessing"
    DETECTING = "detecting"
    EXTRACTING_TEXT = "extracting_text"
    DETECTING_CONNECTIONS = "detecting_connections"
    ASSOCIATING = "associating"
    BUILDING_TOPOLOGY = "building_topology"
    GENERATING_FROM_PROMPT = "generating_from_prompt"
    ANALYZING_COMPLETENESS = "analyzing_completeness"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    ENRICHING = "enriching"
    VALIDATING_NETWORK = "validating_network"
    NETWORK_READY = "network_ready"
    TRANSLATING = "translating"
    VALIDATING_CLOUD = "validating_cloud"
    GENERATING_TERRAFORM = "generating_terraform"
    GENERATING_ANSIBLE = "generating_ansible"
    VALIDATING_TERRAFORM = "validating_terraform"
    VALIDATING_ANSIBLE = "validating_ansible"
    REVIEWING_DEPLOYMENT = "reviewing_deployment"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    APPLYING = "applying"
    GENERATING_INVENTORY = "generating_inventory"
    CONFIGURING = "configuring"
    COMPLETE = "complete"
    REJECTED = "rejected"
    FAILED = "failed"


class NetworkWorkflowState(TypedDict, total=False):
    """The single state object threaded through the graph.

    Domain objects are stored as their serialised JSON form so that a checkpoint is a
    plain document and can be written by any backend. Nodes deserialise what they need.
    """

    # -- identity -------------------------------------------------------- #
    project_id: str
    architecture_id: str
    workflow_id: str
    thread_id: str
    correlation_id: str
    actor: str

    # -- input ----------------------------------------------------------- #
    input_mode: str
    #: Reference, never bytes.
    image_uri: str
    asset_id: str
    user_prompt: str
    #: Revision to modify, when the mode is REVISION.
    base_revision: int

    # -- image extraction ------------------------------------------------ #
    preprocessed: dict[str, Any]
    detections: list[dict[str, Any]]
    ocr_results: list[dict[str, Any]]
    detected_edges: list[dict[str, Any]]
    associations: dict[str, Any]

    # -- shared network stages ------------------------------------------- #
    topology: dict[str, Any]
    missing_information: list[dict[str, Any]]
    enrichment_options: list[dict[str, Any]]
    clarification_questions: list[dict[str, Any]]
    clarification_answers: list[dict[str, Any]]

    #: The canonical network architecture, serialised.
    network_architecture: dict[str, Any]
    network_validation: dict[str, Any]

    # -- RAG ------------------------------------------------------------- #
    #: The canonical cloud architecture, serialised. The RAG's only output.
    cloud_architecture: dict[str, Any]
    cloud_validation: dict[str, Any]
    rag_diagnostics: dict[str, Any]

    # -- generation ------------------------------------------------------ #
    #: URIs into object storage. Generated file content never enters the state.
    terraform_project_uri: str
    terraform_fingerprint: str
    terraform_validation: dict[str, Any]
    ansible_project_uri: str
    ansible_fingerprint: str
    ansible_validation: dict[str, Any]

    # -- deployment ------------------------------------------------------ #
    deployment_id: str
    deployment_plan: dict[str, Any]
    deployment_approved: bool
    approval: dict[str, Any]
    terraform_outputs: dict[str, Any]
    ansible_inventory_uri: str
    deployment_status: str

    # -- bookkeeping ----------------------------------------------------- #
    current_stage: str
    messages: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    #: Stage durations, for the UI's progress display.
    timings: dict[str, float]


def initial_state(
    *,
    project_id: str,
    architecture_id: str,
    workflow_id: str,
    input_mode: InputMode,
    image_uri: str | None = None,
    user_prompt: str | None = None,
    actor: str | None = None,
) -> NetworkWorkflowState:
    """Build a valid starting state. Exactly one input is required."""
    if input_mode is InputMode.IMAGE and not image_uri:
        raise ValueError("image mode requires an image_uri")
    if input_mode is InputMode.PROMPT and not user_prompt:
        raise ValueError("prompt mode requires a user_prompt")

    state: NetworkWorkflowState = {
        "project_id": project_id,
        "architecture_id": architecture_id,
        "workflow_id": workflow_id,
        # Stable across restarts, so a workflow resumes on the same thread.
        "thread_id": f"{project_id}:{architecture_id}",
        "input_mode": input_mode.value,
        "current_stage": Stage.STARTED.value,
        "messages": [],
        "errors": [],
        "timings": {},
        "deployment_approved": False,
    }
    if image_uri:
        state["image_uri"] = image_uri
    if user_prompt:
        state["user_prompt"] = user_prompt
    if actor:
        state["actor"] = actor
    return state
