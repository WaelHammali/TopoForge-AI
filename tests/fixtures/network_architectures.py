"""Canonical network architectures used across the test suite.

``two_router_ospf()`` mirrors the topology in the external RAG's own
``examples/architecture.json`` — PC1—SW1—R1—R2—SW2—PC2 with an HTTP server on SW2 and OSPF
on both routers — so that the adapter is exercised against the shape the RAG documents.
"""

from __future__ import annotations

from src.domain.architecture import (
    ArchitectureMetadata,
    ArchitectureSource,
    NetworkArchitecture,
)
from src.domain.common.provenance import Evidence, ProvenanceSource, Sourced
from src.domain.network import IPAddressSpec, Interface, InterfaceRole
from src.domain.routing import (
    OSPFArea,
    OSPFConfiguration,
    RoutingMode,
    RoutingPlan,
    StaticRoute,
)
from src.domain.network import SubnetSpec
from src.domain.topology import Edge, LinkEndpoint, Node, NodeType

__all__ = ["single_router_lan", "two_router_ospf", "two_router_static"]

_OCR = Evidence(source=ProvenanceSource.OCR, confidence=0.94)


def _address(value: str, prefix: int) -> Sourced[IPAddressSpec]:
    return Sourced(value=IPAddressSpec(address=value, prefix_length=prefix), evidence=_OCR)


def _named(node_id: str, node_type: NodeType, **attributes: object) -> Node:
    return Node(
        id=node_id,
        type=node_type,
        name=Sourced.from_user(node_id),
        attributes=dict(attributes),
    )


def single_router_lan() -> NetworkArchitecture:
    """R1 — SW1 — {WEB1, PC1}. One routed domain, one LAN."""
    nodes = (
        _named("R1", NodeType.ROUTER),
        _named("SW1", NodeType.SWITCH),
        _named(
            "WEB1",
            NodeType.SERVER,
            services=[
                {
                    "protocol": "http",
                    "implementation": "nginx",
                    "enabled": True,
                    "listen": {"port": 80},
                }
            ],
            os={"distribution": "debian", "version": "12"},
        ),
        _named("PC1", NodeType.PC),
    )
    interfaces = (
        Interface(
            id="R1:lan0",
            node_id="R1",
            name="lan0",
            role=InterfaceRole.LAN,
            addresses=(_address("10.10.10.1", 24),),
        ),
        Interface(id="SW1:port1", node_id="SW1", name="port1"),
        Interface(id="SW1:port2", node_id="SW1", name="port2"),
        Interface(id="SW1:port3", node_id="SW1", name="port3"),
        Interface(
            id="WEB1:eth0",
            node_id="WEB1",
            name="eth0",
            addresses=(_address("10.10.10.30", 24),),
        ),
        Interface(
            id="PC1:eth0", node_id="PC1", name="eth0", addresses=(_address("10.10.10.10", 24),)
        ),
    )
    edges = (
        Edge(
            id="link-1",
            source=LinkEndpoint(node_id="R1", interface_id="R1:lan0"),
            target=LinkEndpoint(node_id="SW1", interface_id="SW1:port1"),
        ),
        Edge(
            id="link-2",
            source=LinkEndpoint(node_id="SW1", interface_id="SW1:port2"),
            target=LinkEndpoint(node_id="WEB1", interface_id="WEB1:eth0"),
        ),
        Edge(
            id="link-3",
            source=LinkEndpoint(node_id="SW1", interface_id="SW1:port3"),
            target=LinkEndpoint(node_id="PC1", interface_id="PC1:eth0"),
        ),
    )
    return NetworkArchitecture(
        metadata=ArchitectureMetadata(
            architecture_id="arch_single_router",
            name="single router LAN",
            source=ArchitectureSource.IMAGE,
        ),
        nodes=nodes,
        interfaces=interfaces,
        edges=edges,
    )


def two_router_ospf() -> NetworkArchitecture:
    """PC1—SW1—R1—R2—SW2—{PC2, WEB1}, OSPF area 0 on both routers."""
    nodes = (
        _named("PC1", NodeType.PC),
        _named("SW1", NodeType.SWITCH),
        _named("R1", NodeType.ROUTER),
        _named("R2", NodeType.ROUTER),
        _named("SW2", NodeType.SWITCH),
        _named("PC2", NodeType.PC),
        _named(
            "WEB1",
            NodeType.SERVER,
            services=[
                {
                    "protocol": "http",
                    "implementation": "nginx",
                    "enabled": True,
                    "listen": {"address": "10.20.20.30", "port": 80},
                    "document_root": "/var/www/html",
                }
            ],
            os={"distribution": "debian", "version": "12"},
        ),
    )
    interfaces = (
        Interface(id="PC1:eth0", node_id="PC1", name="eth0", addresses=(_address("10.10.10.10", 24),)),
        Interface(id="SW1:port1", node_id="SW1", name="port1", vlan_id=10),
        Interface(id="SW1:port2", node_id="SW1", name="port2", vlan_id=10),
        Interface(
            id="R1:lan0",
            node_id="R1",
            name="lan0",
            role=InterfaceRole.LAN,
            addresses=(_address("10.10.10.1", 24),),
        ),
        Interface(
            id="R1:wan0",
            node_id="R1",
            name="wan0",
            role=InterfaceRole.TRANSIT,
            addresses=(_address("10.255.0.1", 30),),
        ),
        Interface(
            id="R2:wan0",
            node_id="R2",
            name="wan0",
            role=InterfaceRole.TRANSIT,
            addresses=(_address("10.255.0.2", 30),),
        ),
        Interface(
            id="R2:lan0",
            node_id="R2",
            name="lan0",
            role=InterfaceRole.LAN,
            addresses=(_address("10.20.20.1", 24),),
        ),
        Interface(id="SW2:port1", node_id="SW2", name="port1", vlan_id=20),
        Interface(id="SW2:port2", node_id="SW2", name="port2", vlan_id=20),
        Interface(id="SW2:port3", node_id="SW2", name="port3", vlan_id=20),
        Interface(id="PC2:eth0", node_id="PC2", name="eth0", addresses=(_address("10.20.20.20", 24),)),
        Interface(id="WEB1:eth0", node_id="WEB1", name="eth0", addresses=(_address("10.20.20.30", 24),)),
    )
    edges = (
        Edge(
            id="link-1",
            source=LinkEndpoint(node_id="PC1", interface_id="PC1:eth0"),
            target=LinkEndpoint(node_id="SW1", interface_id="SW1:port1"),
        ),
        Edge(
            id="link-2",
            source=LinkEndpoint(node_id="SW1", interface_id="SW1:port2"),
            target=LinkEndpoint(node_id="R1", interface_id="R1:lan0"),
        ),
        Edge(
            id="link-3",
            source=LinkEndpoint(node_id="R1", interface_id="R1:wan0"),
            target=LinkEndpoint(node_id="R2", interface_id="R2:wan0"),
        ),
        Edge(
            id="link-4",
            source=LinkEndpoint(node_id="R2", interface_id="R2:lan0"),
            target=LinkEndpoint(node_id="SW2", interface_id="SW2:port1"),
        ),
        Edge(
            id="link-5",
            source=LinkEndpoint(node_id="SW2", interface_id="SW2:port2"),
            target=LinkEndpoint(node_id="PC2", interface_id="PC2:eth0"),
        ),
        Edge(
            id="link-6",
            source=LinkEndpoint(node_id="SW2", interface_id="SW2:port3"),
            target=LinkEndpoint(node_id="WEB1", interface_id="WEB1:eth0"),
        ),
    )
    routing = RoutingPlan(
        mode=RoutingMode.OSPF,
        ospf=(
            OSPFConfiguration(
                id="ospf-R1",
                node_id="R1",
                router_id="1.1.1.1",
                areas=(OSPFArea(area_id="0.0.0.0", interface_ids=("R1:lan0", "R1:wan0")),),
                passive_interfaces=("lan0",),
            ),
            OSPFConfiguration(
                id="ospf-R2",
                node_id="R2",
                router_id="2.2.2.2",
                areas=(OSPFArea(area_id="0.0.0.0", interface_ids=("R2:lan0", "R2:wan0")),),
                passive_interfaces=("lan0",),
            ),
        ),
    )
    return NetworkArchitecture(
        metadata=ArchitectureMetadata(
            architecture_id="arch_two_router_ospf",
            name="two router OSPF",
            source=ArchitectureSource.PROMPT,
        ),
        nodes=nodes,
        interfaces=interfaces,
        edges=edges,
        routing=routing,
    )


def two_router_static() -> NetworkArchitecture:
    """The same topology with static routes instead of OSPF."""
    base = two_router_ospf()
    routing = RoutingPlan(
        mode=RoutingMode.STATIC,
        static_routes=(
            StaticRoute(
                id="sr-1",
                node_id="R1",
                destination=SubnetSpec(cidr="10.20.20.0/24"),
                next_hop="10.255.0.2",
                outgoing_interface_id="R1:wan0",
            ),
            StaticRoute(
                id="sr-2",
                node_id="R2",
                destination=SubnetSpec(cidr="10.10.10.0/24"),
                next_hop="10.255.0.1",
                outgoing_interface_id="R2:wan0",
            ),
        ),
    )
    return base.model_copy(
        update={
            "metadata": base.metadata.model_copy(
                update={"architecture_id": "arch_two_router_static", "name": "two router static"}
            ),
            "routing": routing,
        }
    )
