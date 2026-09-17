"""The pipeline invariant, asserted against the graph itself.

    IMAGE / PROMPT
      -> network processing
      -> network_architecture.json
      -> RAG
      -> cloud_architecture.json
      -> deterministic generators
      -> validation -> plan -> approval -> deployment

Every rule below is checked structurally rather than by convention, so a future change
that routes around the RAG, deploys without approval, or lets a generator reach a model
fails the build.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.infrastructure.orchestration.langgraph.spec import END, GRAPH_SPEC

PROJECT_ROOT = Path(__file__).resolve().parents[2]

RAG_NODE = "rag_translate"
NETWORK_ARTIFACT_NODE = "finalise_network_architecture"
CLOUD_VALIDATION_NODE = "validate_cloud_architecture"
GENERATOR_NODES = ("generate_terraform", "generate_ansible")
APPLY_NODE = "terraform_apply"
APPROVAL_NODE = "await_approval"


class TestBothInputModesConverge:
    def test_image_and_prompt_reach_the_same_completeness_node(self) -> None:
        assert GRAPH_SPEC.reaches("preprocess_image", "analyze_completeness")
        assert GRAPH_SPEC.reaches("prompt_llm", "analyze_completeness")

    def test_neither_mode_can_reach_the_rag_without_validation(self) -> None:
        for start in ("preprocess_image", "prompt_llm"):
            assert GRAPH_SPEC.reaches(start, "validate_network")
            assert GRAPH_SPEC.reaches(start, RAG_NODE)

    def test_the_image_pipeline_cannot_deploy_directly(self) -> None:
        # It can only reach deployment *through* the RAG, which the next test pins down.
        assert RAG_NODE in _every_path_bottleneck("preprocess_image", APPLY_NODE)

    def test_the_prompt_llm_cannot_deploy_directly(self) -> None:
        assert RAG_NODE in _every_path_bottleneck("prompt_llm", APPLY_NODE)


class TestTheRAGBoundary:
    def test_the_rag_has_exactly_one_predecessor(self) -> None:
        assert GRAPH_SPEC.predecessors(RAG_NODE) == (NETWORK_ARTIFACT_NODE,)

    def test_the_only_way_into_the_rag_is_through_network_validation(self) -> None:
        assert GRAPH_SPEC.predecessors(NETWORK_ARTIFACT_NODE) == ("validate_network",)

    def test_the_rag_leads_only_to_cloud_validation(self) -> None:
        assert GRAPH_SPEC.successors(RAG_NODE) == (CLOUD_VALIDATION_NODE,)

    def test_no_generator_feeds_back_into_the_rag(self) -> None:
        for node in (*GENERATOR_NODES, "validate_terraform", "validate_ansible", APPLY_NODE):
            assert not GRAPH_SPEC.reaches(node, RAG_NODE), (
                f"{node} can reach the RAG; generated code must never flow back into it"
            )

    def test_generators_are_reachable_only_after_cloud_validation(self) -> None:
        for generator in GENERATOR_NODES:
            assert CLOUD_VALIDATION_NODE in _every_path_bottleneck(RAG_NODE, generator)

    def test_there_is_exactly_one_rag_node(self) -> None:
        rag_nodes = [
            node.name
            for node in GRAPH_SPEC.nodes
            if "rag" in node.name or "translate" in node.name
        ]
        assert rag_nodes == [RAG_NODE], (
            "a second translation node would be a competing pipeline"
        )


class TestDeploymentIsGated:
    def test_apply_is_only_reachable_through_the_approval_interrupt(self) -> None:
        assert GRAPH_SPEC.predecessors(APPLY_NODE) == (APPROVAL_NODE,)

    def test_the_approval_node_interrupts(self) -> None:
        node = GRAPH_SPEC.node(APPROVAL_NODE)
        assert node is not None and node.interrupt_before is True

    def test_rejection_ends_the_workflow_without_applying(self) -> None:
        branches = GRAPH_SPEC.conditional_edges[APPROVAL_NODE]
        assert branches["reject"] == "rejected"
        assert not GRAPH_SPEC.reaches("rejected", APPLY_NODE)
        assert GRAPH_SPEC.successors("rejected") == (END,)

    def test_a_plan_is_always_produced_before_approval(self) -> None:
        assert GRAPH_SPEC.predecessors(APPROVAL_NODE) == ("terraform_plan",)

    def test_generation_and_validation_precede_the_plan(self) -> None:
        for node in ("generate_terraform", "validate_terraform", "deployment_review"):
            assert GRAPH_SPEC.reaches(node, "terraform_plan")

    def test_configuration_runs_only_after_a_real_inventory(self) -> None:
        assert GRAPH_SPEC.predecessors("run_configuration") == ("generate_inventory",)
        assert GRAPH_SPEC.predecessors("generate_inventory") == ("collect_outputs",)
        assert GRAPH_SPEC.predecessors("collect_outputs") == (APPLY_NODE,)


class TestClarificationIsResumable:
    def test_the_clarification_node_interrupts(self) -> None:
        node = GRAPH_SPEC.node("await_clarification")
        assert node is not None and node.interrupt_before is True

    def test_clarification_returns_into_the_pipeline(self) -> None:
        assert GRAPH_SPEC.successors("await_clarification") == ("enrich_network",)

    def test_validation_can_send_the_user_back_to_clarification(self) -> None:
        assert GRAPH_SPEC.conditional_edges["validate_network"]["clarify"] == (
            "await_clarification"
        )


class TestGraphIntegrity:
    def test_every_edge_names_a_real_node(self) -> None:
        known = {*GRAPH_SPEC.node_names(), END}
        for source, target in GRAPH_SPEC.edges:
            assert source in known, source
            assert target in known, target
        for source, branches in GRAPH_SPEC.conditional_edges.items():
            assert source in known, source
            for target in branches.values():
                assert target in known, target

    def test_every_node_is_reachable_from_the_entry_point(self) -> None:
        unreachable = [
            node.name
            for node in GRAPH_SPEC.nodes
            if node.name != GRAPH_SPEC.entry_point
            and not GRAPH_SPEC.reaches(GRAPH_SPEC.entry_point, node.name)
        ]
        assert unreachable == []

    def test_every_node_leads_somewhere(self) -> None:
        for node in GRAPH_SPEC.nodes:
            assert GRAPH_SPEC.successors(node.name), f"{node.name} is a dead end"

    def test_every_path_can_terminate(self) -> None:
        for node in GRAPH_SPEC.nodes:
            assert GRAPH_SPEC.reaches(node.name, END), f"{node.name} cannot reach the end"


class TestSourceLevelBoundaries:
    """The graph could be right while the code cheats. These check the code."""

    def test_no_generator_module_imports_a_model_or_the_rag(self) -> None:
        offenders: list[str] = []
        for path in (PROJECT_ROOT / "src/infrastructure/generators").rglob("*.py"):
            for name in _imported_names(path):
                if name.split(".")[0] in {
                    "openai",
                    "anthropic",
                    "groq",
                    "langchain",
                    "transformers",
                } or name.startswith("src.infrastructure.rag"):
                    offenders.append(f"{path.name}: {name}")
        assert offenders == []

    def test_no_rag_module_imports_a_generator(self) -> None:
        offenders: list[str] = []
        for path in (PROJECT_ROOT / "src/infrastructure/rag").rglob("*.py"):
            for name in _imported_names(path):
                if name.startswith("src.infrastructure.generators"):
                    offenders.append(f"{path.name}: {name}")
        assert offenders == []

    def test_the_rag_port_returns_a_cloud_architecture_and_nothing_file_shaped(self) -> None:
        source = (PROJECT_ROOT / "src/application/ports/rag.py").read_text()
        assert "CloudArchitecture" in source
        for forbidden in ("GeneratedProject", "TerraformProject", "AnsibleProject", "GeneratedFile"):
            assert forbidden not in source, (
                f"the RAG port references {forbidden}; the RAG must stop at "
                "cloud_architecture.json"
            )


# --------------------------------------------------------------------------- #
def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _every_path_bottleneck(source: str, target: str) -> set[str]:
    """Nodes that appear on *every* path from ``source`` to ``target``.

    A node in this set cannot be bypassed, which is exactly what "the RAG is always the
    last AI component before deployment" has to mean structurally.
    """
    if not GRAPH_SPEC.reaches(source, target):
        pytest.fail(f"{target} is not reachable from {source}")

    bottlenecks: set[str] = set()
    for candidate in GRAPH_SPEC.node_names():
        if candidate in {source, target}:
            continue
        if not _reaches_without(source, target, blocked=candidate):
            bottlenecks.add(candidate)
    return bottlenecks


def _reaches_without(source: str, target: str, *, blocked: str) -> bool:
    seen: set[str] = set()
    frontier = [source]
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current in seen or current == blocked:
            continue
        seen.add(current)
        frontier.extend(GRAPH_SPEC.successors(current))
    return False
