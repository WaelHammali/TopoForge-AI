"""``NetworkArchitecture`` -> the RAG's architecture-JSON input shape.

Deterministic and total: no LLM, no I/O. The target shape is the convention documented in
the RAG's ``docs/JSON_CONTRACT.md`` — ``components[]`` carrying interfaces, routing and
services; interface-level ``edges[]``; ``cloud``; ``translation_mode``; and ``ansible``.

Two rules govern this translation:

* **Absence is information.** An interface with no address is emitted without ``ipv4``,
  never with a guessed one. The RAG's own prompt says an omitted setting and a populated
  one carry different meanings, so inventing a value here would corrupt the translation.
* **Nothing is dropped silently.** Anything the target shape cannot express is recorded in
  :attr:`MappedArchitecture.unmapped` and surfaced to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.domain.architecture.models import NetworkArchitecture
from src.domain.network.models import Interface
from src.domain.routing.models import OSPFConfiguration, RoutingMode, StaticRoute
from src.domain.topology.models import Node, NodeType

__all__ = ["MappedArchitecture", "NetworkArchitectureMapper"]

#: Canonical node type -> the RAG's ``components[].type`` vocabulary. Its contract names
#: pc, switch, router, firewall and server; everything else is passed through as its own
#: name and recorded as unmapped, so the gap is visible rather than coerced.
_TYPE_MAP: dict[NodeType, str] = {
    NodeType.ROUTER: "router",
    NodeType.SWITCH: "switch",
    NodeType.FIREWALL: "firewall",
    NodeType.SERVER: "server",
    NodeType.PC: "pc",
    NodeType.DATABASE: "server",
    NodeType.LOAD_BALANCER: "load_balancer",
    NodeType.ACCESS_POINT: "access_point",
    NodeType.GATEWAY: "router",
    NodeType.STORAGE: "server",
    NodeType.CLOUD: "cloud",
    NodeType.INTERNET: "internet",
    NodeType.UNKNOWN: "unknown",
}

#: Types the RAG's contract names explicitly. Anything outside this is reported.
_CONTRACT_TYPES = frozenset({"pc", "switch", "router", "firewall", "server"})

#: Canonical types that imply a service, for architectures that carry no explicit one.
_IMPLIED_SERVICE: dict[NodeType, dict[str, Any]] = {
    NodeType.DATABASE: {"protocol": "database", "enabled": True},
}


@dataclass(slots=True)
class MappedArchitecture:
    """The RAG's input document, plus an account of what did not fit."""

    payload: dict[str, Any]
    unmapped: list[str] = field(default_factory=list)

    @property
    def component_count(self) -> int:
        return len(self.payload.get("components", []))


class NetworkArchitectureMapper:
    """Builds the RAG's input document from the canonical network architecture."""

    def __init__(self, *, provider: str = "aws", region: str = "eu-west-1"):
        self._provider = provider
        self._region = region

    def map(
        self,
        architecture: NetworkArchitecture,
        *,
        translation_mode: str = "behavioral_lab",
        options: dict[str, Any] | None = None,
    ) -> MappedArchitecture:
        unmapped: list[str] = []

        components = [
            self._component(node, architecture, unmapped) for node in architecture.nodes
        ]
        edges = [self._edge(edge, architecture, unmapped) for edge in architecture.edges]

        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "translation_mode": translation_mode,
            "cloud": {"provider": self._provider, "region": self._region, **(options or {})},
            "components": components,
            "edges": edges,
        }

        vlans = self._vlans(architecture)
        if vlans:
            payload["vlans"] = vlans

        ansible = self._ansible(architecture)
        if ansible:
            payload["ansible"] = ansible

        # Fields the canonical model carries that the RAG contract has no home for.
        if architecture.nat:
            unmapped.append(
                f"{len(architecture.nat)} NAT rule(s): the RAG input convention has no NAT "
                "field, so they are passed as component-level configuration only"
            )
        if architecture.acls:
            unmapped.append(
                f"{len(architecture.acls)} ACL(s): carried as component configuration; the "
                "RAG contract models filtering under security rather than as an ACL object"
            )
        for item in architecture.unresolved:
            unmapped.append(f"unresolved field {item.path}: {item.reason}")

        # Provenance is deliberately not sent: the RAG is asked to translate a network,
        # not to reason about how confident the extractor was. Confidence belongs to the
        # clarification stage, which has already finished by this point.
        return MappedArchitecture(payload=payload, unmapped=unmapped)

    # ------------------------------------------------------------------ #
    def _component(
        self, node: Node, architecture: NetworkArchitecture, unmapped: list[str]
    ) -> dict[str, Any]:
        component_type = _TYPE_MAP.get(node.type, node.type.value)
        if component_type not in _CONTRACT_TYPES:
            unmapped.append(
                f"component {node.id!r} has type {component_type!r}, which is outside the "
                "RAG's documented type vocabulary; it is sent verbatim"
            )

        component: dict[str, Any] = {"id": node.id, "type": component_type}

        if node.name is not None and node.name.value != node.id:
            component["name"] = node.name.value
        if node.vendor:
            component["vendor"] = node.vendor
        if node.model:
            component["model"] = node.model

        interfaces = [
            self._interface(interface, architecture)
            for interface in architecture.interfaces_of(node.id)
        ]
        if interfaces:
            component["interfaces"] = interfaces

        routing = self._routing(node, architecture)
        if routing:
            component["routing"] = routing

        services = self._services(node)
        if services:
            component["services"] = services

        # Node attributes are free-form and the contract keeps unknown fields, so they are
        # forwarded rather than dropped.
        reserved = {"services", "os", "interfaces", "routing"}
        extra = {k: v for k, v in node.attributes.items() if k not in reserved}
        if extra:
            component.update(extra)
        if "os" in node.attributes:
            component["os"] = node.attributes["os"]

        return component

    def _interface(self, interface: Interface, architecture: NetworkArchitecture) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": interface.name, "enabled": interface.enabled}

        # Only a complete address is sent. A bare address with no prefix is not an
        # interface address, and guessing a mask here would be inventing network design.
        for sourced in interface.addresses:
            if sourced.value.cidr is not None:
                payload["ipv4" if sourced.value.version == 4 else "ipv6"] = sourced.value.cidr

        if interface.vlan_id is not None:
            payload["access_vlan"] = interface.vlan_id
        if interface.allowed_vlans:
            payload["trunk_vlans"] = list(interface.allowed_vlans)
        if interface.mtu is not None:
            payload["mtu"] = interface.mtu
        if interface.mac is not None:
            payload["mac"] = interface.mac.value
        if interface.description:
            payload["description"] = interface.description
        if interface.role.value != "unknown":
            payload["role"] = interface.role.value
        payload.update(interface.attributes)
        return payload

    def _routing(self, node: Node, architecture: NetworkArchitecture) -> dict[str, Any]:
        routing: dict[str, Any] = {}
        plan = architecture.routing

        if node.is_l3_forwarder:
            routing["ipv4_forwarding"] = True

        gateway = node.attributes.get("default_gateway")
        if isinstance(gateway, dict):
            routing["default_gateway"] = gateway

        static_routes = plan.static_routes_for(node.id)
        if static_routes:
            routing["static_routes"] = [
                self._static_route(route, architecture) for route in static_routes
            ]
        elif plan.mode.includes_static:
            # The user chose static routing but declared none for this node. An empty list
            # says exactly that, and the RAG's contract treats it as meaningful.
            routing["static_routes"] = []

        protocols = [
            self._ospf(config, architecture) for config in plan.ospf_for(node.id)
        ]
        protocols.extend(
            entry
            for entry in plan.extensions.get("protocols", {}).get(node.id, [])
            if isinstance(entry, dict)
        )
        if protocols:
            routing["protocols"] = protocols
        elif plan.mode is RoutingMode.NONE and node.is_l3_forwarder:
            routing["protocols"] = []

        return routing

    def _static_route(
        self, route: StaticRoute, architecture: NetworkArchitecture
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"destination": route.destination.cidr}
        if route.next_hop:
            payload["via"] = route.next_hop
        interface_name = route.outgoing_interface_name
        if interface_name is None and route.outgoing_interface_id:
            interface = architecture.interface(route.outgoing_interface_id)
            interface_name = interface.name if interface else None
        if interface_name:
            payload["interface"] = interface_name
        payload["metric"] = route.metric
        return payload

    def _ospf(
        self, config: OSPFConfiguration, architecture: NetworkArchitecture
    ) -> dict[str, Any]:
        interfaces: list[dict[str, Any]] = []
        passive = set(config.passive_interfaces)

        for area in config.areas:
            for interface_id in area.interface_ids:
                interface = architecture.interface(interface_id)
                name = interface.name if interface else interface_id
                entry: dict[str, Any] = {"id": name, "area": area.area_id}
                if name in passive or interface_id in passive:
                    entry["passive"] = True
                interfaces.append(entry)

        payload: dict[str, Any] = {
            "name": "ospf",
            "enabled": True,
            "process_id": config.process_id,
        }
        if config.router_id:
            payload["router_id"] = config.router_id
        if interfaces:
            payload["interfaces"] = interfaces
        if config.network_statements:
            payload["networks"] = [
                {"network": statement.network.cidr, "area": statement.area_id}
                for statement in config.network_statements
            ]
        if config.passive_interfaces and not interfaces:
            payload["passive_interfaces"] = list(config.passive_interfaces)
        if config.default_information_originate:
            payload["default_information_originate"] = True
        if config.redistribute_static:
            payload["redistribute_static"] = True
        return payload

    @staticmethod
    def _services(node: Node) -> list[dict[str, Any]]:
        declared = node.attributes.get("services")
        if isinstance(declared, list):
            return [service for service in declared if isinstance(service, dict)]
        implied = _IMPLIED_SERVICE.get(node.type)
        return [implied] if implied else []

    @staticmethod
    def _edge(edge: Any, architecture: NetworkArchitecture, unmapped: list[str]) -> dict[str, Any]:
        def endpoint(end: Any) -> dict[str, Any]:
            payload: dict[str, Any] = {"component": end.node_id}
            if end.interface_id:
                interface = architecture.interface(end.interface_id)
                payload["interface"] = interface.name if interface else end.interface_id
            return payload

        source = endpoint(edge.source)
        target = endpoint(edge.target)
        if "interface" not in source or "interface" not in target:
            # The RAG's contract expects interface-level edges. A link that never got
            # resolved to interfaces is still sent, but the imprecision is recorded.
            unmapped.append(
                f"edge {edge.id!r} is not interface-level: "
                f"{edge.source.node_id} - {edge.target.node_id}"
            )

        payload: dict[str, Any] = {
            "id": edge.id,
            "source": source,
            "target": target,
            "enabled": True,
        }
        if edge.vlan_id is not None:
            payload["vlan"] = edge.vlan_id
        if edge.kind.value != "physical":
            payload["kind"] = edge.kind.value
        return payload

    @staticmethod
    def _vlans(architecture: NetworkArchitecture) -> list[dict[str, Any]]:
        vlans: list[dict[str, Any]] = []
        for vlan in architecture.vlans:
            payload: dict[str, Any] = {"id": vlan.id}
            if vlan.name:
                payload["name"] = vlan.name
            if vlan.subnet is not None:
                payload["ipv4"] = vlan.subnet.cidr
            if vlan.gateway_address:
                payload["gateway"] = vlan.gateway_address
            vlans.append(payload)
        return vlans

    @staticmethod
    def _ansible(architecture: NetworkArchitecture) -> dict[str, Any]:
        """Forward declared automation intent.

        Connections use symbolic ``deployment.management.<id>`` references rather than
        addresses, exactly as the RAG's contract requires: a real address does not exist
        until Terraform has applied.
        """
        declared = architecture.extensions.get("ansible")
        if isinstance(declared, dict):
            return declared

        targets = [
            node.id
            for node in architecture.nodes
            if node.type in {NodeType.SERVER, NodeType.DATABASE, NodeType.PC}
            and node.attributes.get("services")
        ]
        if not targets:
            return {}
        return {
            "connections": [
                {
                    "target_ids": [node_id],
                    "host_ref": f"deployment.management.{node_id}",
                }
                for node_id in targets
            ],
            "tasks": [],
        }
