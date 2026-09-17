"""Topology graph: nodes, edges, and the graph they form.

This is the internal representation produced by *both* input modes. It is deliberately
close to the canonical architecture but still mutable-by-copy and free of deployment
concerns; :class:`~src.domain.architecture.models.NetworkArchitecture` is what gets
validated, persisted, versioned and handed to the RAG.
"""

from __future__ import annotations

from collections import defaultdict, deque
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.domain.common.geometry import BoundingBox
from src.domain.common.provenance import Evidence, Sourced
from src.domain.network.models import Interface, Network, VLAN
from src.domain.topology.node_types import L2_TYPES, L3_FORWARDING_TYPES, NodeType

__all__ = ["Edge", "EdgeKind", "LinkEndpoint", "Node", "NodeType", "Topology"]


class Node(BaseModel):
    """A device in the topology."""

    model_config = ConfigDict(frozen=True)

    id: str
    type: NodeType
    #: Human label, e.g. "R1". Sourced because it usually comes from OCR.
    name: Sourced[str] | None = None
    #: The detector's own class string, kept verbatim. Never invented.
    raw_class: str | None = None
    #: Where the node was detected, when it came from an image.
    bbox: BoundingBox | None = None
    evidence: Evidence | None = None
    interface_ids: tuple[str, ...] = ()
    vendor: str | None = None
    model: str | None = None
    #: Free-form attributes, e.g. {"is_bastion": True, "exposure": "public"}.
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("node id must not be empty")
        return value.strip()

    @property
    def display_name(self) -> str:
        return self.name.value if self.name is not None else self.id

    @property
    def is_l3_forwarder(self) -> bool:
        return self.type in L3_FORWARDING_TYPES

    @property
    def is_l2(self) -> bool:
        return self.type in L2_TYPES

    @property
    def confidence(self) -> float:
        return self.evidence.confidence if self.evidence is not None else 1.0


class EdgeKind(str, Enum):
    PHYSICAL = "physical"
    LOGICAL = "logical"
    WIRELESS = "wireless"
    TRUNK = "trunk"
    TUNNEL = "tunnel"
    UNKNOWN = "unknown"


class LinkEndpoint(BaseModel):
    """One end of an edge: a node, and (when known) the interface it lands on."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    interface_id: str | None = None
    #: Pixel coordinates of the detected connector endpoint, when image-derived.
    point_x: float | None = None
    point_y: float | None = None


class Edge(BaseModel):
    """A link between two nodes. Undirected unless ``directed`` is set."""

    model_config = ConfigDict(frozen=True)

    id: str
    source: LinkEndpoint
    target: LinkEndpoint
    kind: EdgeKind = EdgeKind.PHYSICAL
    directed: bool = False
    label: Sourced[str] | None = None
    #: Network this link belongs to, once addressing is resolved.
    network_id: str | None = None
    vlan_id: int | None = None
    evidence: Evidence | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @property
    def node_ids(self) -> tuple[str, str]:
        return (self.source.node_id, self.target.node_id)

    @property
    def confidence(self) -> float:
        return self.evidence.confidence if self.evidence is not None else 1.0

    def connects(self, node_id: str) -> bool:
        return node_id in self.node_ids

    def other_end(self, node_id: str) -> str | None:
        if self.source.node_id == node_id:
            return self.target.node_id
        if self.target.node_id == node_id:
            return self.source.node_id
        return None

    def unordered_key(self) -> tuple[str, str]:
        """Order-independent identity, used to detect duplicate links."""
        return tuple(sorted(self.node_ids))  # type: ignore[return-value]


class Topology(BaseModel):
    """The reconstructed network graph.

    Graph algorithms are implemented here on plain Python structures; NetworkX is used in
    the infrastructure layer where richer algorithms are worth the dependency, but the
    domain stays importable without it.
    """

    model_config = ConfigDict(frozen=True)

    nodes: tuple[Node, ...] = ()
    edges: tuple[Edge, ...] = ()
    interfaces: tuple[Interface, ...] = ()
    networks: tuple[Network, ...] = ()
    vlans: tuple[VLAN, ...] = ()

    # -- lookups ---------------------------------------------------------- #
    def node(self, node_id: str) -> Node | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    def interface(self, interface_id: str) -> Interface | None:
        return next((iface for iface in self.interfaces if iface.id == interface_id), None)

    def edge(self, edge_id: str) -> Edge | None:
        return next((edge for edge in self.edges if edge.id == edge_id), None)

    def nodes_of_type(self, *types: NodeType) -> tuple[Node, ...]:
        wanted = set(types)
        return tuple(node for node in self.nodes if node.type in wanted)

    def interfaces_of(self, node_id: str) -> tuple[Interface, ...]:
        return tuple(iface for iface in self.interfaces if iface.node_id == node_id)

    def edges_of(self, node_id: str) -> tuple[Edge, ...]:
        return tuple(edge for edge in self.edges if edge.connects(node_id))

    # -- graph ------------------------------------------------------------ #
    def adjacency(self) -> dict[str, list[str]]:
        adjacency: dict[str, list[str]] = {node.id: [] for node in self.nodes}
        for edge in self.edges:
            source, target = edge.node_ids
            if source in adjacency and target in adjacency and source != target:
                adjacency[source].append(target)
                adjacency[target].append(source)
        return adjacency

    def degree(self, node_id: str) -> int:
        return len(self.edges_of(node_id))

    def isolated_node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes if self.degree(node.id) == 0)

    def connected_components(self) -> list[set[str]]:
        adjacency = self.adjacency()
        seen: set[str] = set()
        components: list[set[str]] = []
        for start in adjacency:
            if start in seen:
                continue
            component: set[str] = set()
            queue = deque([start])
            seen.add(start)
            while queue:
                current = queue.popleft()
                component.add(current)
                for neighbour in adjacency[current]:
                    if neighbour not in seen:
                        seen.add(neighbour)
                        queue.append(neighbour)
            components.append(component)
        return components

    def is_connected(self) -> bool:
        return len(self.connected_components()) <= 1

    def shortest_path(self, source: str, target: str) -> list[str] | None:
        if source == target:
            return [source]
        adjacency = self.adjacency()
        if source not in adjacency or target not in adjacency:
            return None
        previous: dict[str, str | None] = {source: None}
        queue = deque([source])
        while queue:
            current = queue.popleft()
            for neighbour in adjacency[current]:
                if neighbour in previous:
                    continue
                previous[neighbour] = current
                if neighbour == target:
                    path = [neighbour]
                    while (parent := previous[path[-1]]) is not None:
                        path.append(parent)
                    return list(reversed(path))
                queue.append(neighbour)
        return None

    def duplicate_edge_keys(self) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = defaultdict(int)
        for edge in self.edges:
            counts[edge.unordered_key()] += 1
        return {key: count for key, count in counts.items() if count > 1}

    # -- mutation by copy -------------------------------------------------- #
    def with_nodes(self, nodes: tuple[Node, ...]) -> Topology:
        return self.model_copy(update={"nodes": nodes})

    def with_edges(self, edges: tuple[Edge, ...]) -> Topology:
        return self.model_copy(update={"edges": edges})

    def with_interfaces(self, interfaces: tuple[Interface, ...]) -> Topology:
        return self.model_copy(update={"interfaces": interfaces})

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for node in self.nodes:
            counts[node.type.value] += 1
        return {
            **counts,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "interfaces": len(self.interfaces),
            "networks": len(self.networks),
            "vlans": len(self.vlans),
        }
