"""The workflow, declared as data.

The graph's shape is expressed here as a plain structure, separately from any LangGraph
call. That buys three things:

* the pipeline invariant can be asserted mechanically, by a test that inspects edges;
* the UI can render an accurate progress diagram without running anything;
* the graph is reviewable in one place instead of being spread across builder calls.

:func:`build_graph` in :mod:`.graph` compiles exactly this specification, so the spec and
the running graph cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "END",
    "GRAPH_SPEC",
    "GraphSpec",
    "NodeSpec",
    "START",
    "SubgraphSpec",
]

START = "__start__"
END = "__end__"


@dataclass(frozen=True, slots=True)
class NodeSpec:
    name: str
    description: str
    #: The subgraph this node belongs to.
    subgraph: str
    #: True when the node pauses the workflow and waits for a human.
    interrupt_before: bool = False


@dataclass(frozen=True, slots=True)
class SubgraphSpec:
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class GraphSpec:
    nodes: tuple[NodeSpec, ...]
    #: Unconditional edges.
    edges: tuple[tuple[str, str], ...]
    #: source -> {branch label: destination}, decided by a routing function.
    conditional_edges: dict[str, dict[str, str]] = field(default_factory=dict)
    subgraphs: tuple[SubgraphSpec, ...] = ()
    entry_point: str = "detect_input_mode"

    def node(self, name: str) -> NodeSpec | None:
        return next((node for node in self.nodes if node.name == name), None)

    def node_names(self) -> tuple[str, ...]:
        return tuple(node.name for node in self.nodes)

    def successors(self, name: str) -> tuple[str, ...]:
        direct = tuple(target for source, target in self.edges if source == name)
        conditional = tuple(self.conditional_edges.get(name, {}).values())
        return tuple(dict.fromkeys(direct + conditional))

    def predecessors(self, name: str) -> tuple[str, ...]:
        direct = tuple(source for source, target in self.edges if target == name)
        conditional = tuple(
            source
            for source, branches in self.conditional_edges.items()
            if name in branches.values()
        )
        return tuple(dict.fromkeys(direct + conditional))

    def reaches(self, source: str, target: str) -> bool:
        """Is ``target`` reachable from ``source``?"""
        seen: set[str] = set()
        frontier = [source]
        while frontier:
            current = frontier.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(self.successors(current))
        return False

    def interrupt_nodes(self) -> tuple[str, ...]:
        return tuple(node.name for node in self.nodes if node.interrupt_before)


_SUBGRAPHS = (
    SubgraphSpec("main", "Input routing and terminal states"),
    SubgraphSpec("image_extraction", "Image to topology"),
    SubgraphSpec("prompt_architecture", "Prompt to topology"),
    SubgraphSpec("network_enrichment", "Completeness, clarification, enrichment, validation"),
    SubgraphSpec("rag_translation", "Network architecture to cloud architecture"),
    SubgraphSpec("generation", "Cloud architecture to Terraform and Ansible"),
    SubgraphSpec("deployment", "Review, plan, approval, apply, configure"),
)

_NODES = (
    NodeSpec("detect_input_mode", "Choose the image or prompt path", "main"),
    # --- image ---------------------------------------------------------- #
    NodeSpec("preprocess_image", "Normalise the image without destroying detail", "image_extraction"),
    NodeSpec("detect_components", "YOLO component detection", "image_extraction"),
    NodeSpec("extract_text", "OCR", "image_extraction"),
    NodeSpec("detect_connections", "Connector/edge detection", "image_extraction"),
    NodeSpec("spatial_association", "Bind text and endpoints to components", "image_extraction"),
    NodeSpec("build_topology_from_image", "Reconstruct the graph", "image_extraction"),
    # --- prompt --------------------------------------------------------- #
    NodeSpec("prompt_llm", "Structured generation from a description", "prompt_architecture"),
    NodeSpec("build_topology_from_prompt", "Reconstruct the graph", "prompt_architecture"),
    # --- shared network stages ------------------------------------------ #
    NodeSpec("analyze_completeness", "Find gaps and enrichment opportunities", "network_enrichment"),
    NodeSpec(
        "await_clarification",
        "Pause and ask the user",
        "network_enrichment",
        interrupt_before=True,
    ),
    NodeSpec("enrich_network", "Apply answers and enrichment", "network_enrichment"),
    NodeSpec("validate_network", "Run the network validators", "network_enrichment"),
    NodeSpec("finalise_network_architecture", "Emit network_architecture.json", "network_enrichment"),
    # --- RAG ------------------------------------------------------------ #
    NodeSpec("rag_translate", "Translate to a cloud architecture. The RAG ends here.", "rag_translation"),
    NodeSpec("validate_cloud_architecture", "Run the cloud validators", "rag_translation"),
    # --- generation ----------------------------------------------------- #
    NodeSpec("generate_terraform", "Deterministic Terraform generation", "generation"),
    NodeSpec("validate_terraform", "fmt / init / validate", "generation"),
    NodeSpec("generate_ansible", "Deterministic Ansible generation", "generation"),
    NodeSpec("validate_ansible", "Syntax check and lint", "generation"),
    # --- deployment ----------------------------------------------------- #
    NodeSpec("deployment_review", "Collect artifacts and validation results", "deployment"),
    NodeSpec("terraform_plan", "Produce a reviewable plan. Mutates nothing.", "deployment"),
    NodeSpec(
        "await_approval",
        "Pause for an explicit human decision",
        "deployment",
        interrupt_before=True,
    ),
    NodeSpec("terraform_apply", "Apply an approved plan", "deployment"),
    NodeSpec("collect_outputs", "Read real deployment outputs", "deployment"),
    NodeSpec("generate_inventory", "Build the inventory from those outputs", "deployment"),
    NodeSpec("run_configuration", "Configure the deployed hosts", "deployment"),
    # --- terminal ------------------------------------------------------- #
    NodeSpec("complete", "Finished", "main"),
    NodeSpec("rejected", "The user rejected the plan", "main"),
    NodeSpec("failed", "The workflow failed", "main"),
)

_EDGES: tuple[tuple[str, str], ...] = (
    # image
    ("preprocess_image", "detect_components"),
    ("detect_components", "extract_text"),
    ("extract_text", "detect_connections"),
    ("detect_connections", "spatial_association"),
    ("spatial_association", "build_topology_from_image"),
    ("build_topology_from_image", "analyze_completeness"),
    # prompt
    ("prompt_llm", "build_topology_from_prompt"),
    ("build_topology_from_prompt", "analyze_completeness"),
    # clarification loop
    ("await_clarification", "enrich_network"),
    ("enrich_network", "validate_network"),
    # the single path into the RAG
    ("finalise_network_architecture", "rag_translate"),
    ("rag_translate", "validate_cloud_architecture"),
    # generation
    ("generate_terraform", "validate_terraform"),
    ("generate_ansible", "validate_ansible"),
    # deployment
    ("deployment_review", "terraform_plan"),
    ("terraform_plan", "await_approval"),
    ("terraform_apply", "collect_outputs"),
    ("collect_outputs", "generate_inventory"),
    ("generate_inventory", "run_configuration"),
    ("run_configuration", "complete"),
    # terminal
    ("complete", END),
    ("rejected", END),
    ("failed", END),
)

_CONDITIONAL: dict[str, dict[str, str]] = {
    "detect_input_mode": {
        "image": "preprocess_image",
        "prompt": "prompt_llm",
        "fail": "failed",
    },
    "analyze_completeness": {
        "clarify": "await_clarification",
        "enrich": "enrich_network",
        "fail": "failed",
    },
    "validate_network": {
        "translate": "finalise_network_architecture",
        "clarify": "await_clarification",
        "invalid": "failed",
    },
    "validate_cloud_architecture": {
        "generate": "generate_terraform",
        "invalid": "failed",
    },
    # Ansible generation is skipped entirely when there is no configuration to apply.
    "validate_terraform": {
        "ansible": "generate_ansible",
        "skip": "deployment_review",
    },
    "validate_ansible": {
        "review": "deployment_review",
        "invalid": "failed",
    },
    "await_approval": {
        "apply": "terraform_apply",
        "reject": "rejected",
    },
    "collect_outputs": {
        "configure": "generate_inventory",
        "complete": "complete",
        "fail": "failed",
    },
}

GRAPH_SPEC = GraphSpec(
    nodes=_NODES,
    edges=_EDGES,
    conditional_edges=_CONDITIONAL,
    subgraphs=_SUBGRAPHS,
    entry_point="detect_input_mode",
)
