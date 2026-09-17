"""Canonical node vocabulary and detector-class mapping.

The canonical vocabulary below is *this system's* domain language. It is **not** a claim
about what any YOLO model was trained to detect — those classes are read from the weights
file at load time and are unknown until weights are supplied.

:class:`NodeTypeMapper` translates whatever class names a detector reports into the
canonical vocabulary. Unrecognised classes map to :data:`NodeType.UNKNOWN` and the raw
class name is preserved on the node, so nothing is lost and the gap is visible in the UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "CANONICAL_NODE_TYPES",
    "L2_TYPES",
    "L3_FORWARDING_TYPES",
    "NodeType",
    "NodeTypeMapper",
    "default_node_type_mapper",
]


class NodeType(str, Enum):
    ROUTER = "router"
    SWITCH = "switch"
    FIREWALL = "firewall"
    SERVER = "server"
    DATABASE = "database"
    PC = "pc"
    LOAD_BALANCER = "load_balancer"
    ACCESS_POINT = "access_point"
    GATEWAY = "gateway"
    CLOUD = "cloud"
    STORAGE = "storage"
    INTERNET = "internet"
    UNKNOWN = "unknown"


CANONICAL_NODE_TYPES: frozenset[NodeType] = frozenset(NodeType)

#: Nodes that terminate L3 and can hold routed interfaces / routing configuration.
L3_FORWARDING_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.ROUTER, NodeType.FIREWALL, NodeType.GATEWAY, NodeType.LOAD_BALANCER}
)

#: Nodes that form a broadcast domain rather than terminating one.
L2_TYPES: frozenset[NodeType] = frozenset({NodeType.SWITCH, NodeType.ACCESS_POINT})

#: Nodes that are endpoints: they hold an address but do not forward for others.
ENDPOINT_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.SERVER, NodeType.PC, NodeType.DATABASE, NodeType.STORAGE}
)

# Synonyms commonly seen in diagram labels and detector class names. Extendable at
# runtime through configuration; nothing here is assumed to exist in any given model.
_DEFAULT_SYNONYMS: dict[str, NodeType] = {
    "router": NodeType.ROUTER,
    "rtr": NodeType.ROUTER,
    "l3switch": NodeType.ROUTER,
    "layer3switch": NodeType.ROUTER,
    "multilayerswitch": NodeType.ROUTER,
    "switch": NodeType.SWITCH,
    "sw": NodeType.SWITCH,
    "l2switch": NodeType.SWITCH,
    "hub": NodeType.SWITCH,
    "bridge": NodeType.SWITCH,
    "firewall": NodeType.FIREWALL,
    "fw": NodeType.FIREWALL,
    "asa": NodeType.FIREWALL,
    "utm": NodeType.FIREWALL,
    "server": NodeType.SERVER,
    "srv": NodeType.SERVER,
    "host": NodeType.SERVER,
    "vm": NodeType.SERVER,
    "webserver": NodeType.SERVER,
    "appserver": NodeType.SERVER,
    "database": NodeType.DATABASE,
    "db": NodeType.DATABASE,
    "dbserver": NodeType.DATABASE,
    "sql": NodeType.DATABASE,
    "pc": NodeType.PC,
    "workstation": NodeType.PC,
    "desktop": NodeType.PC,
    "laptop": NodeType.PC,
    "client": NodeType.PC,
    "loadbalancer": NodeType.LOAD_BALANCER,
    "lb": NodeType.LOAD_BALANCER,
    "elb": NodeType.LOAD_BALANCER,
    "alb": NodeType.LOAD_BALANCER,
    "accesspoint": NodeType.ACCESS_POINT,
    "ap": NodeType.ACCESS_POINT,
    "wifi": NodeType.ACCESS_POINT,
    "wirelessap": NodeType.ACCESS_POINT,
    "gateway": NodeType.GATEWAY,
    "gw": NodeType.GATEWAY,
    "cloud": NodeType.CLOUD,
    "wan": NodeType.CLOUD,
    "storage": NodeType.STORAGE,
    "nas": NodeType.STORAGE,
    "san": NodeType.STORAGE,
    "internet": NodeType.INTERNET,
}

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize(value: str) -> str:
    return _NORMALIZE_RE.sub("", value.strip().lower())


@dataclass(slots=True)
class NodeTypeMapper:
    """Maps detector / LLM / label strings onto the canonical vocabulary."""

    synonyms: dict[str, NodeType] = field(default_factory=lambda: dict(_DEFAULT_SYNONYMS))
    #: Class names seen that could not be mapped. Surfaced so the gap is visible.
    unmapped: set[str] = field(default_factory=set)

    def register(self, raw: str, node_type: NodeType) -> None:
        self.synonyms[_normalize(raw)] = node_type

    def register_many(self, mapping: dict[str, str | NodeType]) -> None:
        for raw, node_type in mapping.items():
            self.register(raw, NodeType(node_type) if isinstance(node_type, str) else node_type)

    def map(self, raw: str) -> NodeType:
        """Map ``raw`` to a canonical type, recording it when unmappable."""
        key = _normalize(raw)
        if not key:
            return NodeType.UNKNOWN

        direct = self.synonyms.get(key)
        if direct is not None:
            return direct

        # Trailing digits are instance numbers, not type information: "router2" -> router.
        stripped = key.rstrip("0123456789")
        if stripped and stripped in self.synonyms:
            return self.synonyms[stripped]

        # Longest containing synonym, so "cisco_router_2901" resolves to router.
        containing = [name for name in self.synonyms if len(name) >= 3 and name in key]
        if containing:
            return self.synonyms[max(containing, key=len)]

        self.unmapped.add(raw)
        return NodeType.UNKNOWN

    def map_all(self, raws: list[str]) -> dict[str, NodeType]:
        return {raw: self.map(raw) for raw in raws}


def default_node_type_mapper() -> NodeTypeMapper:
    return NodeTypeMapper()
