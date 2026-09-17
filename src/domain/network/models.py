"""Networking value objects.

All address arithmetic delegates to the standard library :mod:`ipaddress` module. No LLM
is ever asked to compute a network address, a broadcast address, or a mask — those are
exact operations and are performed exactly.
"""

from __future__ import annotations

import ipaddress
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from src.domain.common.provenance import Evidence, Sourced

__all__ = [
    "IPAddressSpec",
    "Interface",
    "InterfaceRole",
    "MACAddress",
    "Network",
    "NetworkScope",
    "SubnetSpec",
    "VLAN",
]

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}([:-])){5}[0-9A-Fa-f]{2}$|^([0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}$")


class MACAddress(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str

    @field_validator("value")
    @classmethod
    def _valid(cls, value: str) -> str:
        if not _MAC_RE.match(value.strip()):
            raise ValueError(f"invalid MAC address: {value!r}")
        return value.strip().lower()


class SubnetSpec(BaseModel):
    """An IPv4/IPv6 network in canonical form."""

    model_config = ConfigDict(frozen=True)

    cidr: str

    @field_validator("cidr")
    @classmethod
    def _canonical(cls, value: str) -> str:
        # strict=False so that "10.0.0.5/24" is accepted and normalised to "10.0.0.0/24";
        # callers that need to reject host bits use IPAddressSpec instead.
        return str(ipaddress.ip_network(value.strip(), strict=False))

    @property
    def network(self) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
        return ipaddress.ip_network(self.cidr, strict=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def prefix_length(self) -> int:
        return self.network.prefixlen

    @computed_field  # type: ignore[prop-decorator]
    @property
    def netmask(self) -> str:
        return str(self.network.netmask)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def version(self) -> int:
        return self.network.version

    @property
    def network_address(self) -> str:
        return str(self.network.network_address)

    @property
    def broadcast_address(self) -> str:
        return str(self.network.broadcast_address)

    @property
    def usable_host_count(self) -> int:
        """Assignable host addresses.

        /31 point-to-point links (RFC 3021) and /32 host routes are special-cased: they
        have no network/broadcast reservation.
        """
        net = self.network
        total = net.num_addresses
        if net.version == 4 and net.prefixlen >= 31:
            return total
        if net.version == 6 and net.prefixlen >= 127:
            return total
        return max(total - 2, 0)

    def contains(self, address: str) -> bool:
        try:
            return ipaddress.ip_address(address) in self.network
        except ValueError:
            return False

    def overlaps(self, other: SubnetSpec) -> bool:
        if self.version != other.version:
            return False
        return self.network.overlaps(other.network)

    def is_subnet_of(self, other: SubnetSpec) -> bool:
        if self.version != other.version:
            return False
        return self.network.subnet_of(other.network)  # type: ignore[arg-type]

    def __str__(self) -> str:
        return self.cidr


class IPAddressSpec(BaseModel):
    """A host address together with the prefix it lives on.

    This is the canonical result of parsing any of ``10.0.0.1/24``,
    ``10.0.0.1 255.255.255.0`` or ``10.0.0.1 /24``.
    """

    model_config = ConfigDict(frozen=True)

    address: str
    prefix_length: int | None = None

    @field_validator("address")
    @classmethod
    def _valid_address(cls, value: str) -> str:
        return str(ipaddress.ip_address(value.strip()))

    @model_validator(mode="after")
    def _valid_prefix(self) -> IPAddressSpec:
        if self.prefix_length is None:
            return self
        limit = 32 if self.version == 4 else 128
        if not 0 <= self.prefix_length <= limit:
            raise ValueError(f"prefix /{self.prefix_length} is invalid for IPv{self.version}")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def version(self) -> int:
        return ipaddress.ip_address(self.address).version

    @property
    def interface(self) -> ipaddress.IPv4Interface | ipaddress.IPv6Interface | None:
        if self.prefix_length is None:
            return None
        return ipaddress.ip_interface(f"{self.address}/{self.prefix_length}")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def netmask(self) -> str | None:
        iface = self.interface
        return str(iface.netmask) if iface is not None else None

    @property
    def subnet(self) -> SubnetSpec | None:
        iface = self.interface
        return SubnetSpec(cidr=str(iface.network)) if iface is not None else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cidr(self) -> str | None:
        return f"{self.address}/{self.prefix_length}" if self.prefix_length is not None else None

    @property
    def is_network_address(self) -> bool:
        """True when the host part is all zeros on a prefix that reserves it."""
        iface = self.interface
        if iface is None or iface.network.prefixlen >= (31 if self.version == 4 else 127):
            return False
        return iface.ip == iface.network.network_address

    @property
    def is_broadcast_address(self) -> bool:
        iface = self.interface
        if iface is None or self.version != 4 or iface.network.prefixlen >= 31:
            return False
        return iface.ip == iface.network.broadcast_address

    def same_subnet_as(self, other: IPAddressSpec) -> bool | None:
        """Tri-state: ``None`` when either side lacks a prefix, so callers can ask."""
        if self.subnet is None or other.subnet is None:
            return None
        return self.subnet.cidr == other.subnet.cidr

    def __str__(self) -> str:
        return self.cidr or self.address


class InterfaceRole(str, Enum):
    LAN = "lan"
    WAN = "wan"
    TRANSIT = "transit"
    MANAGEMENT = "management"
    LOOPBACK = "loopback"
    TRUNK = "trunk"
    ACCESS = "access"
    UNKNOWN = "unknown"


class Interface(BaseModel):
    """A network interface on a node.

    ``addresses`` carries provenance for every address so the UI can show where each one
    came from and the user can override it.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    node_id: str
    name: str
    role: InterfaceRole = InterfaceRole.UNKNOWN
    addresses: tuple[Sourced[IPAddressSpec], ...] = ()
    mac: MACAddress | None = None
    vlan_id: int | None = None
    #: VLANs carried when the interface is a trunk.
    allowed_vlans: tuple[int, ...] = ()
    enabled: bool = True
    mtu: int | None = None
    description: str | None = None
    #: Edge this interface terminates, when known.
    edge_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("interface name must not be empty")
        return value.strip()

    @property
    def primary_address(self) -> IPAddressSpec | None:
        return self.addresses[0].value if self.addresses else None

    @property
    def has_address(self) -> bool:
        return bool(self.addresses)

    @property
    def has_complete_address(self) -> bool:
        """An address is complete only when it also carries a prefix length."""
        return any(item.value.prefix_length is not None for item in self.addresses)

    @property
    def subnets(self) -> tuple[SubnetSpec, ...]:
        return tuple(
            item.value.subnet for item in self.addresses if item.value.subnet is not None
        )

    def with_address(self, address: IPAddressSpec, evidence: Evidence) -> Interface:
        """Return a copy with one more address. Value objects are never mutated."""
        return self.model_copy(
            update={"addresses": (*self.addresses, Sourced(value=address, evidence=evidence))}
        )


class NetworkScope(str, Enum):
    LAN = "lan"
    TRANSIT = "transit"
    LOOPBACK = "loopback"
    EXTERNAL = "external"


class Network(BaseModel):
    """An L3 network / broadcast domain discovered in or declared for the topology."""

    model_config = ConfigDict(frozen=True)

    id: str
    subnet: SubnetSpec | None = None
    name: str | None = None
    scope: NetworkScope = NetworkScope.LAN
    vlan_id: int | None = None
    #: Interface ids attached to this network.
    member_interface_ids: tuple[str, ...] = ()
    #: Node ids attached to this network (denormalised for convenience).
    member_node_ids: tuple[str, ...] = ()
    gateway_address: str | None = None
    evidence: Evidence | None = None

    @property
    def is_point_to_point(self) -> bool:
        return self.subnet is not None and self.subnet.usable_host_count <= 2


class VLAN(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int = Field(ge=1, le=4094, description="802.1Q VLAN id; 0 and 4095 are reserved")
    name: str | None = None
    #: Nodes participating in this VLAN.
    node_ids: tuple[str, ...] = ()
    #: Interfaces in access mode for this VLAN.
    access_interface_ids: tuple[str, ...] = ()
    #: Interfaces trunking this VLAN.
    trunk_interface_ids: tuple[str, ...] = ()
    subnet: SubnetSpec | None = None
    gateway_address: str | None = None
    evidence: Evidence | None = None
