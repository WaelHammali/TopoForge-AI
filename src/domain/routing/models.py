"""Routing, NAT and ACL domain models.

Routing is *enrichment*: a topology without routing is valid and can still be translated.
:class:`RoutingMode` records what the user chose — ``none``, ``static``, ``ospf``, or
``static_and_ospf`` — so downstream stages never have to guess whether missing routing is
an omission or a decision.

The model is deliberately extensible: BGP, HSRP/VRRP, DHCP, DNS and VPN slot in as further
members of :class:`RoutingPlan` without changing anything that already exists.
"""

from __future__ import annotations

import ipaddress
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.domain.common.provenance import Evidence
from src.domain.network.models import IPAddressSpec, SubnetSpec

__all__ = [
    "ACL",
    "ACLAction",
    "ACLRule",
    "NATRule",
    "NATType",
    "OSPFArea",
    "OSPFAreaType",
    "OSPFConfiguration",
    "OSPFNetworkStatement",
    "RoutingMode",
    "RoutingPlan",
    "StaticRoute",
]


class RoutingMode(str, Enum):
    NONE = "none"
    STATIC = "static"
    OSPF = "ospf"
    STATIC_AND_OSPF = "static_and_ospf"

    @property
    def includes_static(self) -> bool:
        return self in {RoutingMode.STATIC, RoutingMode.STATIC_AND_OSPF}

    @property
    def includes_ospf(self) -> bool:
        return self in {RoutingMode.OSPF, RoutingMode.STATIC_AND_OSPF}


class StaticRoute(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    node_id: str
    #: Destination prefix. ``0.0.0.0/0`` is the default route.
    destination: SubnetSpec
    next_hop: str | None = None
    outgoing_interface_id: str | None = None
    outgoing_interface_name: str | None = None
    metric: int = Field(default=1, ge=0, le=255)
    administrative_distance: int | None = Field(default=None, ge=0, le=255)
    description: str | None = None
    evidence: Evidence | None = None

    @field_validator("next_hop")
    @classmethod
    def _valid_next_hop(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(ipaddress.ip_address(value.strip()))

    @model_validator(mode="after")
    def _has_forwarding_target(self) -> StaticRoute:
        if self.next_hop is None and not (self.outgoing_interface_id or self.outgoing_interface_name):
            raise ValueError(
                "a static route needs a next hop or an outgoing interface; it has neither"
            )
        return self

    @property
    def is_default_route(self) -> bool:
        return self.destination.prefix_length == 0


class OSPFAreaType(str, Enum):
    STANDARD = "standard"
    STUB = "stub"
    TOTALLY_STUB = "totally_stub"
    NSSA = "nssa"


class OSPFNetworkStatement(BaseModel):
    """``network <prefix> area <id>`` — the prefix that activates OSPF on interfaces."""

    model_config = ConfigDict(frozen=True)

    network: SubnetSpec
    area_id: str = "0.0.0.0"

    @field_validator("area_id")
    @classmethod
    def _normalize_area(cls, value: str) -> str:
        return normalize_area_id(value)


class OSPFArea(BaseModel):
    model_config = ConfigDict(frozen=True)

    area_id: str = "0.0.0.0"
    type: OSPFAreaType = OSPFAreaType.STANDARD
    networks: tuple[SubnetSpec, ...] = ()
    #: Interfaces placed in this area (alternative to network statements).
    interface_ids: tuple[str, ...] = ()
    authentication: str | None = None

    @field_validator("area_id")
    @classmethod
    def _normalize_area(cls, value: str) -> str:
        return normalize_area_id(value)

    @property
    def is_backbone(self) -> bool:
        return self.area_id == "0.0.0.0"


class OSPFConfiguration(BaseModel):
    """OSPFv2 process on one node."""

    model_config = ConfigDict(frozen=True)

    id: str
    node_id: str
    process_id: int = Field(default=1, ge=1, le=65535)
    router_id: str | None = None
    areas: tuple[OSPFArea, ...] = ()
    network_statements: tuple[OSPFNetworkStatement, ...] = ()
    passive_interfaces: tuple[str, ...] = ()
    default_information_originate: bool = False
    reference_bandwidth_mbps: int | None = None
    redistribute_static: bool = False
    evidence: Evidence | None = None

    @field_validator("router_id")
    @classmethod
    def _valid_router_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        address = ipaddress.ip_address(value.strip())
        if address.version != 4:
            raise ValueError("an OSPF router-id must be in IPv4 dotted form")
        return str(address)

    @property
    def area_ids(self) -> tuple[str, ...]:
        explicit = {area.area_id for area in self.areas}
        implied = {statement.area_id for statement in self.network_statements}
        return tuple(sorted(explicit | implied))

    @property
    def has_backbone(self) -> bool:
        return "0.0.0.0" in self.area_ids

    @property
    def is_area_border_router(self) -> bool:
        return len(self.area_ids) > 1


class NATType(str, Enum):
    STATIC = "static"
    DYNAMIC = "dynamic"
    PAT = "pat"
    SOURCE = "source"
    DESTINATION = "destination"


class NATRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    node_id: str
    type: NATType = NATType.PAT
    inside_local: str | None = None
    inside_global: str | None = None
    outside_local: str | None = None
    outside_global: str | None = None
    #: Prefix translated in dynamic/PAT rules.
    source_network: SubnetSpec | None = None
    inside_interface_id: str | None = None
    outside_interface_id: str | None = None
    protocol: str | None = None
    inside_port: int | None = Field(default=None, ge=1, le=65535)
    outside_port: int | None = Field(default=None, ge=1, le=65535)
    description: str | None = None
    evidence: Evidence | None = None


class ACLAction(str, Enum):
    PERMIT = "permit"
    DENY = "deny"


class ACLRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int = Field(ge=1)
    action: ACLAction
    protocol: str = "ip"
    source: str = "any"
    source_port: str | None = None
    destination: str = "any"
    destination_port: str | None = None
    description: str | None = None
    log: bool = False


class ACL(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    node_id: str | None = None
    rules: tuple[ACLRule, ...] = ()
    #: ("interface_id", "in"|"out") pairs.
    applied_to: tuple[tuple[str, str], ...] = ()
    implicit_deny: bool = True
    evidence: Evidence | None = None


class RoutingPlan(BaseModel):
    """All routing configuration for an architecture.

    Extension point: BGP, HSRP/VRRP, DHCP, DNS and VPN become additional fields here.
    """

    model_config = ConfigDict(frozen=True)

    mode: RoutingMode = RoutingMode.NONE
    ospf: tuple[OSPFConfiguration, ...] = ()
    static_routes: tuple[StaticRoute, ...] = ()
    #: Reserved for future protocols; carried through untouched so the schema can grow
    #: without a version bump for every protocol added.
    extensions: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_configured(self) -> bool:
        return bool(self.ospf or self.static_routes)

    def ospf_for(self, node_id: str) -> tuple[OSPFConfiguration, ...]:
        return tuple(config for config in self.ospf if config.node_id == node_id)

    def static_routes_for(self, node_id: str) -> tuple[StaticRoute, ...]:
        return tuple(route for route in self.static_routes if route.node_id == node_id)

    def declared_mode(self) -> RoutingMode:
        """The mode implied by the actual contents, used to catch inconsistency."""
        has_static = bool(self.static_routes)
        has_ospf = bool(self.ospf)
        if has_static and has_ospf:
            return RoutingMode.STATIC_AND_OSPF
        if has_static:
            return RoutingMode.STATIC
        if has_ospf:
            return RoutingMode.OSPF
        return RoutingMode.NONE


def normalize_area_id(value: str | int) -> str:
    """Accept ``0``, ``"0"`` or ``"0.0.0.0"`` and always return dotted form."""
    text = str(value).strip()
    if text.isdigit():
        return str(ipaddress.IPv4Address(int(text)))
    return str(ipaddress.IPv4Address(text))


# Re-exported for validators that need to compare a route's next hop to an interface.
def next_hop_in_subnet(route: StaticRoute, address: IPAddressSpec) -> bool | None:
    """Is the route's next hop reachable on ``address``'s subnet? ``None`` if unknowable."""
    if route.next_hop is None or address.subnet is None:
        return None
    return address.subnet.contains(route.next_hop)
