"""RAG plan + network architecture -> typed ``CloudArchitecture``.

The RAG returns a free-form JSON plan: which component becomes what, how the networking
should be represented, what Ansible should do, what it could not do. This module turns
that into the strongly typed contract the generators consume.

Division of labour, deliberately:

* **The RAG decides.** What a router becomes, whether egress is required, which
  representation each component gets, what the deployment pattern is. Those decisions are
  read from the plan and followed.
* **The architecture supplies facts.** CIDRs, interface addresses, VLAN ids, which
  component is attached to which. These are exact, already validated, and are never
  re-derived from model prose.
* **This module invents nothing.** A decision the plan does not make is not made here. An
  unrecognised representation is recorded in ``unmapped``, not guessed at. A NAT gateway
  appears only because the plan asked for one.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from src.domain.architecture.models import NetworkArchitecture
from src.domain.cloud.configuration import (
    Configuration,
    ConfigurationHost,
    PackageRequirement,
    ServiceRequirement,
    SystemRole,
)
from src.domain.cloud.models import (
    Assumption,
    CloudArchitecture,
    CloudArchitectureMetadata,
    CloudProvider,
    CloudWarning,
    Relationship,
    RelationshipType,
    RetrievedReference,
    WarningSeverity,
)
from src.domain.cloud.resources import (
    ComputeInstance,
    Gateway,
    GatewayType,
    Infrastructure,
    NetworkInterfaceSpec,
    Route,
    RouteTable,
    SecurityGroup,
    SecurityRule,
    SecurityRuleDirection,
    Subnet,
    SubnetPurpose,
    VirtualNetwork,
)
from src.domain.common.identifiers import slugify, stable_id
from src.domain.common.provenance import Evidence, ProvenanceSource
from src.domain.network.models import SubnetSpec
from src.domain.topology.models import Node, NodeType
from src.shared.errors import RAGError

__all__ = ["CloudArchitectureAssembler", "classify_representation"]

#: Keyword -> resource kind, applied to the model's ``cloud_representation`` string.
#: Longest match wins. Anything unmatched is reported rather than assumed.
_REPRESENTATION_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("transit gateway", "transit_gateway"),
    ("transit_gateway", "transit_gateway"),
    ("nat gateway", "nat_gateway"),
    ("nat_gateway", "nat_gateway"),
    ("internet gateway", "internet_gateway"),
    ("internet_gateway", "internet_gateway"),
    ("vpc peering", "peering"),
    ("peering", "peering"),
    ("security group", "security_group"),
    ("security_group", "security_group"),
    ("network firewall", "security_group"),
    ("load balancer", "load_balancer"),
    ("load_balancer", "load_balancer"),
    ("subnet", "subnet"),
    ("vpc", "virtual_network"),
    ("rds", "database"),
    ("database", "database"),
    ("ec2", "compute"),
    ("instance", "compute"),
    ("compute", "compute"),
    ("container", "compute"),
    ("appliance", "compute"),
    ("host", "compute"),
)

#: Service implementation -> the packages and services a role needs. Deterministic; the
#: RAG names the implementation, this table knows what installing it means.
_SERVICE_ROLES: dict[str, dict[str, Any]] = {
    "nginx": {"packages": ("nginx",), "services": ("nginx",)},
    "apache": {"packages": ("apache2",), "services": ("apache2",)},
    "httpd": {"packages": ("httpd",), "services": ("httpd",)},
    "haproxy": {"packages": ("haproxy",), "services": ("haproxy",)},
    "postgresql": {"packages": ("postgresql",), "services": ("postgresql",)},
    "mysql": {"packages": ("mysql-server",), "services": ("mysql",)},
    "frr": {"packages": ("frr",), "services": ("frr",)},
    "bird": {"packages": ("bird",), "services": ("bird",)},
}

_PACKAGE_OPERATIONS = frozenset({"ansible.builtin.package", "package", "apt", "yum", "dnf"})
_SERVICE_OPERATIONS = frozenset({"ansible.builtin.service", "service", "systemd"})


def classify_representation(representation: str | None) -> str | None:
    """Map a model-authored ``cloud_representation`` string to a resource kind."""
    if not representation:
        return None
    lowered = representation.lower()
    matches = [
        (keyword, kind) for keyword, kind in _REPRESENTATION_KEYWORDS if keyword in lowered
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item[0]))[1]


@dataclass(slots=True)
class _Segment:
    """One L2/L3 segment discovered in the network: a broadcast domain."""

    id: str
    subnet: SubnetSpec | None
    node_ids: set[str] = field(default_factory=set)
    interface_ids: set[str] = field(default_factory=set)
    vlan_id: int | None = None
    #: Switches that form this segment, for naming and traceability.
    switch_ids: list[str] = field(default_factory=list)
    #: Only routers are on this segment: it is a router-to-router link.
    is_transit: bool = False
    #: The link crosses routed domains, so the cloud carries it as transport rather than
    #: as a subnet inside either VPC.
    is_transport: bool = False


class CloudArchitectureAssembler:
    """Builds a typed cloud architecture from the RAG's plan."""

    def __init__(self, *, provider: str = "aws", region: str = "eu-west-1"):
        self._provider = provider
        self._region = region

    # ------------------------------------------------------------------ #
    def assemble(
        self,
        plan: dict[str, Any],
        architecture: NetworkArchitecture,
        *,
        producers: dict[str, str] | None = None,
        extra_unmapped: list[str] | None = None,
    ) -> CloudArchitecture:
        cloud_plan = _require_object(plan, "cloud_plan")
        ansible_plan = plan.get("ansible_plan")
        if ansible_plan is not None and not isinstance(ansible_plan, dict):
            raise RAGError("the RAG plan's 'ansible_plan' is not a JSON object")

        mapping = self._component_mapping(cloud_plan)
        unmapped: list[str] = list(extra_unmapped or [])
        warnings: list[CloudWarning] = []

        segments = self._discover_segments(architecture)
        domains = self._discover_domains(architecture, segments)

        networks, subnets, relationships = self._networks_and_subnets(
            architecture, domains, segments, mapping, unmapped
        )
        subnet_by_segment = {subnet.attributes["segment_id"]: subnet for subnet in subnets}

        gateways, gateway_relationships = self._gateways(
            cloud_plan, architecture, networks, subnets, mapping, warnings
        )
        self._check_transport(segments, gateways, networks, warnings, unmapped)
        route_tables = self._route_tables(networks, subnets, gateways)
        security_groups = self._security_groups(cloud_plan, architecture, networks, mapping)
        compute = self._compute(
            architecture, mapping, subnet_by_segment, segments, security_groups, unmapped
        )

        infrastructure = Infrastructure(
            networks=tuple(networks),
            subnets=tuple(subnets),
            route_tables=tuple(route_tables),
            gateways=tuple(gateways),
            security_groups=tuple(security_groups),
            compute=tuple(compute),
        )
        configuration = self._configuration(ansible_plan or {}, architecture, compute)

        metadata = CloudArchitectureMetadata(
            cloud_architecture_id=stable_id(
                architecture.architecture_id, architecture.revision, prefix="cloud"
            ),
            name=f"{architecture.metadata.name} (cloud)",
            provider=CloudProvider(self._provider),
            region=self._region,
            network_architecture_id=architecture.architecture_id,
            network_architecture_revision=architecture.revision,
            project_id=architecture.metadata.project_id,
            deployment_pattern=self._deployment_pattern(cloud_plan, networks, gateways),
            confidence=cloud_plan.get("confidence"),
            producers=producers or {},
        )

        return CloudArchitecture(
            metadata=metadata,
            infrastructure=infrastructure,
            configuration=configuration,
            relationships=tuple([*relationships, *gateway_relationships]),
            assumptions=self._assumptions(cloud_plan),
            warnings=tuple([*warnings, *self._limitation_warnings(plan)]),
            references=self._references(plan),
            unmapped=tuple(dict.fromkeys(unmapped)),
            evidence=Evidence(
                source=ProvenanceSource.RAG,
                confidence=_confidence_value(cloud_plan.get("confidence")),
                detail="translated from the network architecture by the knowledge-base RAG",
                producer=(producers or {}).get("rag_revision"),
            ),
        )

    # ------------------------------------------------------------------ #
    # Network facts
    # ------------------------------------------------------------------ #
    def _discover_segments(self, architecture: NetworkArchitecture) -> list[_Segment]:
        """Group the network into broadcast domains.

        Deterministic and derived from the validated architecture, never from model text:
        interfaces sharing a subnet are in one segment, and L2 devices join the segments
        of what they connect.
        """
        by_subnet: dict[str, _Segment] = {}
        unaddressed: list[_Segment] = []

        for interface in architecture.interfaces:
            subnets = interface.subnets
            if subnets:
                key = subnets[0].cidr
                segment = by_subnet.get(key)
                if segment is None:
                    segment = _Segment(
                        id=f"seg-{slugify(key)}", subnet=subnets[0], vlan_id=interface.vlan_id
                    )
                    by_subnet[key] = segment
                segment.node_ids.add(interface.node_id)
                segment.interface_ids.add(interface.id)
                if segment.vlan_id is None:
                    segment.vlan_id = interface.vlan_id

        # L2 devices carry no address of their own, so they are attached through the
        # interfaces at the far end of their links. Matching on the far *interface* rather
        # than on the far node matters: a router sits on several segments at once, so
        # "this switch touches R1" would wrongly place it on R1's transit link too.
        for node in architecture.nodes:
            if not node.is_l2:
                continue
            far_interface_ids: set[str] = set()
            far_node_ids: set[str] = set()
            for edge in architecture.edges_of(node.id):
                for endpoint in (edge.source, edge.target):
                    if endpoint.node_id == node.id:
                        continue
                    far_node_ids.add(endpoint.node_id)
                    if endpoint.interface_id:
                        far_interface_ids.add(endpoint.interface_id)

            for segment in by_subnet.values():
                if far_interface_ids & segment.interface_ids:
                    attached = True
                elif far_interface_ids:
                    # Interface-level information exists and did not match this segment.
                    attached = False
                else:
                    # No interface-level edges at all: fall back to node adjacency, which
                    # is all the information there is.
                    attached = bool(far_node_ids & segment.node_ids)
                if attached:
                    segment.node_ids.add(node.id)
                    segment.switch_ids.append(node.id)

        segments = list(by_subnet.values())
        for segment in segments:
            l3_members = [
                node_id
                for node_id in segment.node_ids
                if (node := architecture.node(node_id)) is not None and node.is_l3_forwarder
            ]
            # A segment whose only members are routers is a transit link between them.
            segment.is_transit = len(l3_members) >= 2 and len(segment.node_ids) == len(l3_members)

        return sorted(segments + unaddressed, key=lambda item: item.id)

    def _discover_domains(
        self, architecture: NetworkArchitecture, segments: list[_Segment]
    ) -> dict[str, list[_Segment]]:
        """Assign each segment to a routed domain, one per L3 forwarder.

        A routed domain becomes a VPC. A transit segment is shared, and is assigned to the
        first of its routers by sorted id so that the result is stable across runs.
        """
        routers = sorted(
            node.id for node in architecture.nodes if node.is_l3_forwarder
        )
        domains: dict[str, list[_Segment]] = {router: [] for router in routers}

        if not routers:
            # No L3 device at all: one implicit domain holding everything, which the RAG's
            # knowledge base calls a logical VPC.
            domains["__implicit__"] = list(segments)
            return domains

        for segment in segments:
            owners = sorted(segment.node_ids & set(routers))
            if len(owners) > 1:
                # A link between routers in different routed domains is *transport*, not a
                # subnet: an EC2 instance cannot hold a NIC in another VPC. It becomes the
                # justification for peering or a transit gateway instead.
                segment.is_transport = True
                continue
            domains[owners[0] if owners else routers[0]].append(segment)
        return domains

    def _networks_and_subnets(
        self,
        architecture: NetworkArchitecture,
        domains: dict[str, list[_Segment]],
        segments: list[_Segment],
        mapping: dict[str, dict[str, Any]],
        unmapped: list[str],
    ) -> tuple[list[VirtualNetwork], list[Subnet], list[Relationship]]:
        networks: list[VirtualNetwork] = []
        subnets: list[Subnet] = []
        relationships: list[Relationship] = []

        for router_id, owned in sorted(domains.items()):
            if not owned:
                continue
            cidr = _covering_supernet([segment.subnet for segment in owned if segment.subnet])
            if cidr is None:
                unmapped.append(
                    f"routed domain {router_id!r} has no addressed segment, so no VPC CIDR "
                    "could be derived from the architecture"
                )
                continue

            node = architecture.node(router_id)
            name = slugify(node.display_name if node else router_id)
            network = VirtualNetwork(
                id=f"vpc-{name}",
                logical_name=f"{name}-vpc",
                cidr=cidr,
                region=self._region,
                source_node_ids=() if router_id == "__implicit__" else (router_id,),
                evidence=_rag_evidence(mapping.get(router_id)),
                attributes={"routed_domain": router_id},
            )
            networks.append(network)
            if router_id != "__implicit__":
                relationships.append(
                    Relationship(
                        id=f"rel-{network.id}-source",
                        type=RelationshipType.DERIVED_FROM,
                        source_id=network.id,
                        target_id=network.id,
                        description=f"routed domain of {router_id}",
                    )
                )

            for segment in owned:
                if segment.subnet is None:
                    continue
                label = slugify(
                    segment.switch_ids[0] if segment.switch_ids else segment.subnet.cidr
                )
                subnet = Subnet(
                    id=f"sn-{slugify(segment.subnet.cidr)}",
                    logical_name=f"{name}-{label}",
                    network_id=network.id,
                    cidr=segment.subnet,
                    purpose=SubnetPurpose.TRANSIT if segment.is_transit else SubnetPurpose.LAN,
                    source_vlan_id=segment.vlan_id,
                    source_node_ids=tuple(sorted(segment.switch_ids)),
                    # public is decided by the RAG plan's gateway decisions, below.
                    public=False,
                    attributes={"segment_id": segment.id},
                )
                subnets.append(subnet)
                relationships.append(
                    Relationship(
                        id=f"rel-{network.id}-{subnet.id}",
                        type=RelationshipType.CONTAINS,
                        source_id=network.id,
                        target_id=subnet.id,
                    )
                )
        return networks, subnets, relationships

    # ------------------------------------------------------------------ #
    # RAG decisions
    # ------------------------------------------------------------------ #
    @staticmethod
    def _component_mapping(cloud_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        entries = cloud_plan.get("component_mapping")
        if not isinstance(entries, list):
            return {}
        return {
            str(entry["component_id"]): entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("component_id")
        }

    def _gateways(
        self,
        cloud_plan: dict[str, Any],
        architecture: NetworkArchitecture,
        networks: list[VirtualNetwork],
        subnets: list[Subnet],
        mapping: dict[str, dict[str, Any]],
        warnings: list[CloudWarning],
    ) -> tuple[list[Gateway], list[Relationship]]:
        """Create gateways only where the plan asked for them.

        The RAG is explicitly instructed never to invent public access or a bastion to
        make connectivity work. Honouring that means a gateway appears here only when the
        plan names one.
        """
        gateways: list[Gateway] = []
        relationships: list[Relationship] = []
        plan_text = _plan_text(cloud_plan)

        wants_internet = _mentions(plan_text, "internet gateway", "internet_gateway", "igw")
        wants_nat = _mentions(plan_text, "nat gateway", "nat_gateway")
        wants_peering = _mentions(plan_text, "peering")
        wants_transit = _mentions(plan_text, "transit gateway", "transit_gateway", "tgw")

        for network in networks:
            network_subnets = [s for s in subnets if s.network_id == network.id]
            if not network_subnets:
                continue

            if wants_internet:
                gateway = Gateway(
                    id=f"gw-igw-{network.id}",
                    logical_name=f"{network.logical_name}-igw",
                    type=GatewayType.INTERNET,
                    network_id=network.id,
                    source_node_ids=network.source_node_ids,
                )
                gateways.append(gateway)
                relationships.append(
                    Relationship(
                        id=f"rel-{gateway.id}",
                        type=RelationshipType.ATTACHED_TO,
                        source_id=gateway.id,
                        target_id=network.id,
                    )
                )

            if wants_nat:
                public = next(
                    (s for s in network_subnets if s.public),
                    network_subnets[0],
                )
                gateways.append(
                    Gateway(
                        id=f"gw-nat-{network.id}",
                        logical_name=f"{network.logical_name}-nat",
                        type=GatewayType.NAT,
                        network_id=network.id,
                        subnet_id=public.id,
                        allocate_public_ip=True,
                        source_node_ids=network.source_node_ids,
                    )
                )

        if wants_peering and len(networks) == 2:
            gateways.append(
                Gateway(
                    id="gw-peering",
                    logical_name="vpc-peering",
                    type=GatewayType.PEERING,
                    peer_network_ids=tuple(network.id for network in networks),
                )
            )
        elif wants_peering and len(networks) != 2:
            warnings.append(
                CloudWarning(
                    id="warn-peering-arity",
                    code="peering_requires_two_networks",
                    message=(
                        f"the plan proposes VPC peering but the translation produced "
                        f"{len(networks)} networks; peering connects exactly two"
                    ),
                    severity=WarningSeverity.HIGH,
                    remediation="use a transit gateway for three or more routed domains",
                )
            )

        if wants_transit and len(networks) >= 2:
            gateways.append(
                Gateway(
                    id="gw-transit",
                    logical_name="transit-gateway",
                    type=GatewayType.TRANSIT,
                    peer_network_ids=tuple(network.id for network in networks),
                )
            )

        return gateways, relationships


    @staticmethod
    def _check_transport(
        segments: list[_Segment],
        gateways: list[Gateway],
        networks: list[VirtualNetwork],
        warnings: list[CloudWarning],
        unmapped: list[str],
    ) -> None:
        """Every cross-domain link must have cloud transport, or the lab loses a path.

        Reported rather than repaired: choosing peering over a transit gateway is a
        knowledge-base decision belonging to the RAG, and inventing one here would be
        exactly the fabrication the translation contract forbids.
        """
        transport_segments = [segment for segment in segments if segment.is_transport]
        if not transport_segments:
            return

        has_transport = any(
            gateway.type in {GatewayType.PEERING, GatewayType.TRANSIT} for gateway in gateways
        )
        for segment in transport_segments:
            cidr = segment.subnet.cidr if segment.subnet else segment.id
            unmapped.append(
                f"router-to-router link {cidr} crosses routed domains: in the cloud it is "
                "carried as transport between VPCs, not as a subnet"
            )
        if not has_transport and len(networks) > 1:
            warnings.append(
                CloudWarning(
                    id="warn-missing-transport",
                    code="no_transport_between_routed_domains",
                    message=(
                        f"{len(transport_segments)} router-to-router link(s) connect "
                        f"{len(networks)} routed domains, but the plan proposes no VPC "
                        "peering or transit gateway to carry them"
                    ),
                    severity=WarningSeverity.HIGH,
                    remediation=(
                        "the translation must choose peering (two domains) or a transit "
                        "gateway (three or more) for these paths"
                    ),
                )
            )

    @staticmethod
    def _route_tables(
        networks: list[VirtualNetwork], subnets: list[Subnet], gateways: list[Gateway]
    ) -> list[RouteTable]:
        """One route table per network, carrying only routes the gateways justify."""
        tables: list[RouteTable] = []
        for network in networks:
            network_subnets = [s for s in subnets if s.network_id == network.id]
            if not network_subnets:
                continue

            routes: list[Route] = []
            default = SubnetSpec(cidr="0.0.0.0/0")
            internet = next(
                (
                    g
                    for g in gateways
                    if g.type is GatewayType.INTERNET and g.network_id == network.id
                ),
                None,
            )
            nat = next(
                (g for g in gateways if g.type is GatewayType.NAT and g.network_id == network.id),
                None,
            )
            if internet is not None:
                routes.append(Route(destination=default, target_ref=internet.id))
            elif nat is not None:
                routes.append(Route(destination=default, target_ref=nat.id))

            for gateway in gateways:
                if gateway.type not in {GatewayType.PEERING, GatewayType.TRANSIT}:
                    continue
                if network.id not in gateway.peer_network_ids:
                    continue
                for peer_id in gateway.peer_network_ids:
                    if peer_id == network.id:
                        continue
                    peer = next((n for n in networks if n.id == peer_id), None)
                    if peer is not None:
                        routes.append(Route(destination=peer.cidr, target_ref=gateway.id))

            tables.append(
                RouteTable(
                    id=f"rt-{network.id}",
                    logical_name=f"{network.logical_name}-rt",
                    network_id=network.id,
                    routes=tuple(routes),
                    associated_subnet_ids=tuple(s.id for s in network_subnets),
                    is_main=True,
                    source_node_ids=network.source_node_ids,
                )
            )
        return tables

    def _security_groups(
        self,
        cloud_plan: dict[str, Any],
        architecture: NetworkArchitecture,
        networks: list[VirtualNetwork],
        mapping: dict[str, dict[str, Any]],
    ) -> list[SecurityGroup]:
        """Security groups from the plan's security entries and firewall components.

        Rules come from the plan. Where the plan states no rule, the group is created
        empty: an empty group denies, which is the safe reading of silence.
        """
        if not networks:
            return []

        groups: list[SecurityGroup] = []
        default_network = networks[0]

        for node in architecture.nodes:
            if node.type is not NodeType.FIREWALL:
                continue
            groups.append(
                SecurityGroup(
                    id=f"sg-{slugify(node.id)}",
                    logical_name=slugify(node.display_name),
                    network_id=default_network.id,
                    source_firewall_node_id=node.id,
                    source_node_ids=(node.id,),
                    rules=tuple(self._rules_for(cloud_plan, node.id)),
                    evidence=_rag_evidence(mapping.get(node.id)),
                )
            )
        return groups

    @staticmethod
    def _rules_for(cloud_plan: dict[str, Any], component_id: str) -> list[SecurityRule]:
        entries = cloud_plan.get("networking", {})
        security = entries.get("security") if isinstance(entries, dict) else None
        if not isinstance(security, list):
            return []

        rules: list[SecurityRule] = []
        for entry in security:
            if not isinstance(entry, dict):
                continue
            subjects = entry.get("component_ids") or entry.get("component_id") or []
            subjects = [subjects] if isinstance(subjects, str) else list(subjects)
            if component_id not in subjects:
                continue
            port = entry.get("port")
            sources = entry.get("sources") or entry.get("cidr_blocks") or []
            sources = [sources] if isinstance(sources, str) else list(sources)
            rules.append(
                SecurityRule(
                    direction=(
                        SecurityRuleDirection.EGRESS
                        if str(entry.get("direction", "ingress")).lower() == "egress"
                        else SecurityRuleDirection.INGRESS
                    ),
                    protocol=str(entry.get("protocol", "tcp")),
                    from_port=int(port) if isinstance(port, (int, str)) and str(port).isdigit() else None,
                    to_port=int(port) if isinstance(port, (int, str)) and str(port).isdigit() else None,
                    cidr_blocks=tuple(str(item) for item in sources),
                    description=entry.get("description"),
                )
            )
        return rules

    def _compute(
        self,
        architecture: NetworkArchitecture,
        mapping: dict[str, dict[str, Any]],
        subnet_by_segment: dict[str, Subnet],
        segments: list[_Segment],
        security_groups: list[SecurityGroup],
        unmapped: list[str],
    ) -> list[ComputeInstance]:
        """Compute resources for every component the plan represents as one."""
        segment_by_interface: dict[str, _Segment] = {}
        for segment in segments:
            for interface_id in segment.interface_ids:
                segment_by_interface[interface_id] = segment

        group_ids = tuple(group.id for group in security_groups)
        instances: list[ComputeInstance] = []

        for node in architecture.nodes:
            entry = mapping.get(node.id)
            kind = classify_representation(
                (entry or {}).get("cloud_representation") if entry else None
            )

            if kind is None:
                kind = _default_kind(node)
                if entry is not None:
                    unmapped.append(
                        f"component {node.id!r}: the plan's cloud representation "
                        f"{(entry.get('cloud_representation') or '')!r} was not recognised; "
                        f"treated as {kind or 'nothing'}"
                    )
            if kind != "compute":
                continue

            nics: list[NetworkInterfaceSpec] = []
            for index, interface in enumerate(architecture.interfaces_of(node.id)):
                segment = segment_by_interface.get(interface.id)
                subnet = subnet_by_segment.get(segment.id) if segment else None
                if subnet is None:
                    continue
                address = interface.primary_address
                nics.append(
                    NetworkInterfaceSpec(
                        id=f"nic-{slugify(node.id)}-{slugify(interface.name)}",
                        subnet_id=subnet.id,
                        private_ip=address.address if address else None,
                        # A forwarding node must be allowed to pass traffic that is not
                        # addressed to it.
                        source_dest_check=not node.is_l3_forwarder,
                        primary=index == 0,
                        security_group_ids=group_ids,
                        source_interface_id=interface.id,
                    )
                )

            if not nics:
                unmapped.append(
                    f"component {node.id!r} maps to compute but none of its interfaces "
                    "resolved to a subnet, so it was not placed"
                )
                continue

            configuration = (entry or {}).get("configuration") or {}
            instances.append(
                ComputeInstance(
                    id=f"c-{slugify(node.id)}",
                    logical_name=slugify(node.display_name),
                    subnet_id=nics[0].subnet_id,
                    interfaces=tuple(nics),
                    security_group_ids=group_ids,
                    instance_type=str(configuration.get("instance_type", "t3.micro")),
                    image=configuration.get("image"),
                    roles=self._roles_for(node, configuration),
                    is_forwarder=node.is_l3_forwarder,
                    is_bastion=bool(configuration.get("bastion", False)),
                    assign_public_ip=bool(configuration.get("public", False)),
                    source_node_ids=(node.id,),
                    evidence=_rag_evidence(entry),
                )
            )
        return instances

    @staticmethod
    def _roles_for(node: Node, configuration: dict[str, Any]) -> tuple[str, ...]:
        declared = configuration.get("roles")
        if isinstance(declared, list) and declared:
            return tuple(str(role) for role in declared)

        roles: list[str] = []
        if node.is_l3_forwarder:
            roles.append("router")
        for service in node.attributes.get("services", []) or []:
            if isinstance(service, dict) and service.get("enabled", True):
                implementation = service.get("implementation") or service.get("protocol")
                if implementation:
                    roles.append(slugify(str(implementation)))
        return tuple(dict.fromkeys(roles))

    # ------------------------------------------------------------------ #
    # Configuration half
    # ------------------------------------------------------------------ #
    def _configuration(
        self,
        ansible_plan: dict[str, Any],
        architecture: NetworkArchitecture,
        compute: list[ComputeInstance],
    ) -> Configuration:
        compute_by_source = {
            instance.source_node_ids[0]: instance
            for instance in compute
            if instance.source_node_ids
        }

        roles = self._roles(ansible_plan, architecture)
        role_names = {role.name for role in roles}
        hosts: list[ConfigurationHost] = []

        targets = ansible_plan.get("targets")
        target_ids = [
            str(entry["component_id"])
            for entry in (targets if isinstance(targets, list) else [])
            if isinstance(entry, dict) and entry.get("component_id")
        ]
        # A component with roles but no explicit Ansible target still needs configuring.
        for instance in compute:
            source = instance.source_node_ids[0] if instance.source_node_ids else None
            if source and instance.roles and source not in target_ids:
                target_ids.append(source)

        target_entries = {
            str(entry["component_id"]): entry
            for entry in (targets if isinstance(targets, list) else [])
            if isinstance(entry, dict) and entry.get("component_id")
        }

        for component_id in dict.fromkeys(target_ids):
            instance = compute_by_source.get(component_id)
            if instance is None:
                continue
            entry = target_entries.get(component_id, {})
            connection = entry.get("connection") if isinstance(entry, dict) else {}
            connection = connection if isinstance(connection, dict) else {}
            applicable = tuple(role for role in instance.roles if role in role_names)
            hosts.append(
                ConfigurationHost(
                    id=f"h-{slugify(component_id)}",
                    name=slugify(component_id),
                    compute_id=instance.id,
                    groups=instance.roles or ("all_managed",),
                    roles=applicable,
                    remote_user=connection.get("user"),
                    requires_bastion=not instance.assign_public_ip,
                    # Symbolic: the real address only exists after apply.
                    address_output_ref=f"{instance.id}_private_ip".replace("-", "_"),
                    variables=(entry.get("variables") or {}) if isinstance(entry, dict) else {},
                )
            )

        return Configuration(hosts=tuple(hosts), roles=tuple(roles))

    def _roles(
        self, ansible_plan: dict[str, Any], architecture: NetworkArchitecture
    ) -> tuple[SystemRole, ...]:
        """Roles from the plan's task intentions and the architecture's services.

        The plan's tasks are intentions, not YAML — turning them into packages and
        services is this deterministic step's job, not the model's.
        """
        packages: dict[str, set[str]] = defaultdict(set)
        services: dict[str, set[str]] = defaultdict(set)

        for node in architecture.nodes:
            if node.is_l3_forwarder and architecture.routing.is_configured:
                # A router appliance needs a routing daemon to behave like the source
                # device; which daemon is named by the knowledge base, not guessed here.
                packages["router"].update(_SERVICE_ROLES["frr"]["packages"])
                services["router"].update(_SERVICE_ROLES["frr"]["services"])
            for service in node.attributes.get("services", []) or []:
                if not isinstance(service, dict) or not service.get("enabled", True):
                    continue
                implementation = str(
                    service.get("implementation") or service.get("protocol") or ""
                ).lower()
                known = _SERVICE_ROLES.get(implementation)
                role_name = slugify(implementation)
                if known:
                    packages[role_name].update(known["packages"])
                    services[role_name].update(known["services"])
                elif implementation:
                    packages[role_name].add(implementation)

        tasks = ansible_plan.get("tasks")
        for task in tasks if isinstance(tasks, list) else []:
            if not isinstance(task, dict):
                continue
            operation = str(task.get("operation", "")).lower()
            parameters = task.get("parameters") if isinstance(task.get("parameters"), dict) else {}
            role_name = slugify(str(task.get("id") or operation or "task"))

            if operation in _PACKAGE_OPERATIONS:
                names = parameters.get("name", [])
                names = [names] if isinstance(names, str) else list(names)
                packages[role_name].update(str(name) for name in names)
            elif operation in _SERVICE_OPERATIONS:
                name = parameters.get("name")
                if name:
                    services[role_name].add(str(name))

        role_names = sorted(set(packages) | set(services))
        return tuple(
            SystemRole(
                id=f"role-{name}",
                name=name,
                packages=tuple(
                    PackageRequirement(name=package) for package in sorted(packages[name])
                ),
                services=tuple(
                    ServiceRequirement(name=service) for service in sorted(services[name])
                ),
            )
            for name in role_names
        )

    # ------------------------------------------------------------------ #
    # Explanations
    # ------------------------------------------------------------------ #
    @staticmethod
    def _assumptions(cloud_plan: dict[str, Any]) -> tuple[Assumption, ...]:
        entries = cloud_plan.get("dependencies")
        assumptions: list[Assumption] = []
        for index, entry in enumerate(entries if isinstance(entries, list) else []):
            statement = entry if isinstance(entry, str) else str(entry.get("description", entry))
            assumptions.append(
                Assumption(
                    id=f"assumption-{index}",
                    statement=statement,
                    rationale="declared by the RAG as a dependency of its translation",
                )
            )
        return tuple(assumptions)

    @staticmethod
    def _limitation_warnings(plan: dict[str, Any]) -> tuple[CloudWarning, ...]:
        entries = plan.get("limitations")
        warnings: list[CloudWarning] = []
        for index, entry in enumerate(entries if isinstance(entries, list) else []):
            message = entry if isinstance(entry, str) else str(entry)
            warnings.append(
                CloudWarning(
                    id=f"limitation-{index}",
                    code="rag_limitation",
                    message=message,
                    severity=WarningSeverity.MEDIUM,
                )
            )
        return tuple(warnings)

    @staticmethod
    def _references(plan: dict[str, Any]) -> tuple[RetrievedReference, ...]:
        entries = plan.get("knowledge")
        references: list[RetrievedReference] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            references.append(
                RetrievedReference(
                    source=str(entry.get("source", "unknown")),
                    heading=str(entry.get("heading", entry.get("rule_id", ""))),
                )
            )
        return tuple(references)

    @staticmethod
    def _deployment_pattern(
        cloud_plan: dict[str, Any], networks: list[VirtualNetwork], gateways: list[Gateway]
    ) -> str:
        declared = cloud_plan.get("deployment_pattern")
        if isinstance(declared, str) and declared:
            return declared
        if any(gateway.type is GatewayType.TRANSIT for gateway in gateways):
            return "multi_vpc_tgw"
        if any(gateway.type is GatewayType.PEERING for gateway in gateways):
            return "multi_vpc_peering"
        return "single_vpc" if len(networks) <= 1 else "multi_vpc_isolated"


# --------------------------------------------------------------------------- #
def _require_object(plan: dict[str, Any], key: str) -> dict[str, Any]:
    value = plan.get(key)
    if not isinstance(value, dict):
        raise RAGError(
            f"the RAG plan is missing a usable {key!r} object; refusing to assemble a "
            "cloud architecture from an incomplete plan",
            received_keys=sorted(plan) if isinstance(plan, dict) else None,
        )
    return value


def _default_kind(node: Node) -> str | None:
    """What a component becomes when the plan said nothing about it."""
    if node.type in {NodeType.SERVER, NodeType.PC, NodeType.DATABASE, NodeType.STORAGE}:
        return "compute"
    if node.type in {NodeType.ROUTER, NodeType.GATEWAY}:
        return "compute"
    return None


def _covering_supernet(subnets: list[SubnetSpec]) -> SubnetSpec | None:
    """Smallest prefix covering every subnet. Exact arithmetic, no estimation."""
    networks = [subnet.network for subnet in subnets if subnet is not None]
    if not networks:
        return None
    ipv4 = [network for network in networks if network.version == 4]
    if not ipv4:
        return None

    current = ipv4[0]
    for network in ipv4[1:]:
        while not (network.subnet_of(current) or network == current):
            if current.prefixlen == 0:
                break
            current = current.supernet()
    return SubnetSpec(cidr=str(current))


def _plan_text(cloud_plan: dict[str, Any]) -> str:
    import json

    return json.dumps(cloud_plan, default=str).lower()


def _mentions(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _rag_evidence(entry: dict[str, Any] | None) -> Evidence | None:
    if entry is None:
        return None
    rule_ids = entry.get("rule_ids") or []
    detail = str(entry.get("cloud_representation", "")) or None
    if rule_ids:
        detail = f"{detail or 'mapped'} (rules: {', '.join(str(r) for r in rule_ids)})"
    return Evidence(source=ProvenanceSource.RAG, confidence=0.8, detail=detail)


def _confidence_value(confidence: Any) -> float:
    return {"low": 0.3, "medium": 0.6, "high": 0.9}.get(str(confidence).lower(), 0.6)
