"""The AWS mapping layer: cloud resource -> Terraform resource.

Every mapping decision lives here. Nothing elsewhere in the generator knows that a
:class:`~src.domain.cloud.resources.VirtualNetwork` becomes an ``aws_vpc``, or that a NAT
gateway needs an Elastic IP. Adding a resource type means adding one mapper here and
registering it — not editing the generator.

Each mapper is a pure function ``(resource, context) -> list[Block]``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.domain.cloud.models import CloudArchitecture
from src.domain.cloud.resources import (
    ComputeInstance,
    Database,
    Gateway,
    GatewayType,
    LoadBalancer,
    LoadBalancerScheme,
    LoadBalancerType,
    RouteTable,
    SecurityGroup,
    SecurityRuleDirection,
    Subnet,
    VirtualNetwork,
)
from src.domain.generation.models import GeneratorWarning, WarningSeverity
from src.infrastructure.generators.terraform.hcl import Block, Raw

__all__ = ["MappingContext", "ResourceMapper", "TERRAFORM_TYPE_BY_KIND", "build_registry"]

_IDENTIFIER_CLEAN = re.compile(r"[^a-zA-Z0-9_]+")

#: Documentation of the mapping, also used by the UI to explain what will be created.
TERRAFORM_TYPE_BY_KIND: dict[str, str] = {
    "virtual_network": "aws_vpc",
    "subnet": "aws_subnet",
    "route_table": "aws_route_table",
    "gateway.internet": "aws_internet_gateway",
    "gateway.nat": "aws_nat_gateway",
    "gateway.transit": "aws_ec2_transit_gateway",
    "gateway.peering": "aws_vpc_peering_connection",
    "gateway.vpn": "aws_vpn_gateway",
    "gateway.virtual_private": "aws_vpn_gateway",
    "compute": "aws_instance",
    "database": "aws_db_instance",
    "load_balancer": "aws_lb",
    "security_group": "aws_security_group",
}


def terraform_identifier(value: str) -> str:
    """Turn any id into a legal, stable Terraform identifier.

    Deterministic: the same cloud resource id always yields the same identifier, which is
    what keeps regenerated code byte-identical.
    """
    cleaned = _IDENTIFIER_CLEAN.sub("_", value.strip()).strip("_").lower()
    if not cleaned:
        cleaned = "resource"
    if cleaned[0].isdigit():
        cleaned = f"r_{cleaned}"
    return cleaned


@dataclass(slots=True)
class MappingContext:
    """Everything a mapper needs besides the resource itself."""

    architecture: CloudArchitecture
    #: cloud resource id -> ("aws_vpc", "r1_vpc"), for building references.
    addresses: dict[str, tuple[str, str]] = field(default_factory=dict)
    warnings: list[GeneratorWarning] = field(default_factory=list)
    #: Names of Terraform variables the project must declare.
    required_variables: set[str] = field(default_factory=set)
    #: Outputs the project must declare.
    outputs: dict[str, tuple[Any, str]] = field(default_factory=dict)

    def reference(self, resource_id: str, attribute: str = "id") -> Raw | None:
        """``aws_vpc.r1_vpc.id`` for a cloud resource id, or ``None`` if unknown."""
        address = self.addresses.get(resource_id)
        if address is None:
            return None
        return Raw(f"{address[0]}.{address[1]}.{attribute}")

    def warn(
        self,
        code: str,
        message: str,
        *subject_ids: str,
        severity: WarningSeverity = WarningSeverity.MEDIUM,
    ) -> None:
        self.warnings.append(
            GeneratorWarning(
                code=code, message=message, subject_ids=subject_ids, severity=severity
            )
        )

    def base_tags(self, resource: Any) -> dict[str, str]:
        """Tags applied to everything, plus the resource's own.

        ``topoforge:source-nodes`` is the traceability link: it says which diagram
        component this resource came from, readable straight from the AWS console.
        """
        tags: dict[str, str] = {
            "Name": resource.logical_name,
            "ManagedBy": "topoforge",
        }
        if getattr(resource, "source_node_ids", ()):
            tags["topoforge:source-nodes"] = ",".join(resource.source_node_ids)
        tags.update(getattr(resource, "tags", {}) or {})
        return tags


ResourceMapper = Callable[[Any, MappingContext], list[Block]]


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #
def map_virtual_network(network: VirtualNetwork, ctx: MappingContext) -> list[Block]:
    name = terraform_identifier(network.id)
    block = Block("resource", ("aws_vpc", name), comments=_provenance(network))
    block.set("cidr_block", network.cidr.cidr)
    block.set("enable_dns_support", network.enable_dns_support)
    block.set("enable_dns_hostnames", network.enable_dns_hostnames)
    block.set("tags", ctx.base_tags(network))

    blocks = [block]
    for index, secondary in enumerate(network.secondary_cidrs):
        association = Block(
            "resource", ("aws_vpc_ipv4_cidr_block_association", f"{name}_secondary_{index}")
        )
        association.set("vpc_id", Raw(f"aws_vpc.{name}.id"))
        association.set("cidr_block", secondary.cidr)
        blocks.append(association)

    ctx.outputs[f"{name}_id"] = (Raw(f"aws_vpc.{name}.id"), f"VPC id for {network.logical_name}")
    return blocks


def map_subnet(subnet: Subnet, ctx: MappingContext) -> list[Block]:
    name = terraform_identifier(subnet.id)
    vpc_ref = ctx.reference(subnet.network_id)
    if vpc_ref is None:
        ctx.warn(
            "unresolved_network_reference",
            f"subnet {subnet.logical_name!r} references network {subnet.network_id!r}, "
            "which is not in this architecture",
            subnet.id,
            severity=WarningSeverity.HIGH,
        )
        return []

    block = Block("resource", ("aws_subnet", name), comments=_provenance(subnet))
    block.set("vpc_id", vpc_ref)
    block.set("cidr_block", subnet.cidr.cidr)
    if subnet.availability_zone:
        block.set("availability_zone", subnet.availability_zone)
    else:
        # Deterministic AZ selection from the region's own data source: no hard-coded
        # AZ names, and no dependence on which account is being deployed into.
        index = _subnet_index(subnet, ctx)
        block.set(
            "availability_zone",
            Raw(f"data.aws_availability_zones.available.names[{index}]"),
        )
    block.set("map_public_ip_on_launch", subnet.public)
    block.set("tags", {**ctx.base_tags(subnet), "topoforge:purpose": subnet.purpose.value})
    return [block]


def map_route_table(table: RouteTable, ctx: MappingContext) -> list[Block]:
    name = terraform_identifier(table.id)
    vpc_ref = ctx.reference(table.network_id)
    if vpc_ref is None:
        ctx.warn(
            "unresolved_network_reference",
            f"route table {table.logical_name!r} references unknown network "
            f"{table.network_id!r}",
            table.id,
            severity=WarningSeverity.HIGH,
        )
        return []

    block = Block("resource", ("aws_route_table", name), comments=_provenance(table))
    block.set("vpc_id", vpc_ref)
    for route in table.routes:
        target = ctx.reference(route.target_ref)
        if target is None:
            ctx.warn(
                "unresolved_route_target",
                f"route to {route.destination.cidr} in {table.logical_name!r} targets "
                f"{route.target_ref!r}, which is not in this architecture",
                table.id,
                severity=WarningSeverity.HIGH,
            )
            continue
        route_block = Block("route")
        route_block.set("cidr_block", route.destination.cidr)
        route_block.set(_route_target_argument(route.target_ref, ctx), target)
        block.add(route_block)
    block.set("tags", ctx.base_tags(table))

    blocks = [block]
    for subnet_id in table.associated_subnet_ids:
        subnet_ref = ctx.reference(subnet_id)
        if subnet_ref is None:
            ctx.warn(
                "unresolved_subnet_association",
                f"route table {table.logical_name!r} associates unknown subnet {subnet_id!r}",
                table.id,
            )
            continue
        association = Block(
            "resource",
            ("aws_route_table_association", f"{name}_{terraform_identifier(subnet_id)}"),
        )
        association.set("subnet_id", subnet_ref)
        association.set("route_table_id", Raw(f"aws_route_table.{name}.id"))
        blocks.append(association)
    return blocks


def map_gateway(gateway: Gateway, ctx: MappingContext) -> list[Block]:
    name = terraform_identifier(gateway.id)

    if gateway.type is GatewayType.INTERNET:
        block = Block("resource", ("aws_internet_gateway", name), comments=_provenance(gateway))
        vpc_ref = ctx.reference(gateway.network_id) if gateway.network_id else None
        if vpc_ref is None:
            ctx.warn(
                "unresolved_network_reference",
                f"internet gateway {gateway.logical_name!r} is not attached to a known network",
                gateway.id,
                severity=WarningSeverity.HIGH,
            )
            return []
        block.set("vpc_id", vpc_ref)
        block.set("tags", ctx.base_tags(gateway))
        return [block]

    if gateway.type is GatewayType.NAT:
        subnet_ref = ctx.reference(gateway.subnet_id) if gateway.subnet_id else None
        if subnet_ref is None:
            # A NAT gateway with nowhere public to live cannot be generated correctly,
            # and silently placing it somewhere would be worse than reporting it.
            ctx.warn(
                "nat_gateway_without_public_subnet",
                f"NAT gateway {gateway.logical_name!r} has no public subnet to live in; "
                "the cloud architecture must place it in one",
                gateway.id,
                severity=WarningSeverity.HIGH,
            )
            return []
        eip = Block("resource", ("aws_eip", f"{name}_eip"), comments=_provenance(gateway))
        eip.set("domain", "vpc")
        eip.set("tags", {**ctx.base_tags(gateway), "Name": f"{gateway.logical_name}-eip"})

        block = Block("resource", ("aws_nat_gateway", name))
        block.set("allocation_id", Raw(f"aws_eip.{name}_eip.id"))
        block.set("subnet_id", subnet_ref)
        block.set("tags", ctx.base_tags(gateway))
        return [eip, block]

    if gateway.type is GatewayType.PEERING:
        if len(gateway.peer_network_ids) != 2:
            ctx.warn(
                "peering_needs_exactly_two_networks",
                f"peering {gateway.logical_name!r} names "
                f"{len(gateway.peer_network_ids)} networks; exactly two are required",
                gateway.id,
                severity=WarningSeverity.HIGH,
            )
            return []
        left, right = (ctx.reference(item) for item in gateway.peer_network_ids)
        if left is None or right is None:
            ctx.warn(
                "unresolved_peer_network",
                f"peering {gateway.logical_name!r} references a network that is not present",
                gateway.id,
                severity=WarningSeverity.HIGH,
            )
            return []
        block = Block(
            "resource", ("aws_vpc_peering_connection", name), comments=_provenance(gateway)
        )
        block.set("vpc_id", left)
        block.set("peer_vpc_id", right)
        block.set("auto_accept", True)
        block.set("tags", ctx.base_tags(gateway))
        return [block]

    if gateway.type is GatewayType.TRANSIT:
        block = Block(
            "resource", ("aws_ec2_transit_gateway", name), comments=_provenance(gateway)
        )
        block.set("description", gateway.logical_name)
        block.set("tags", ctx.base_tags(gateway))
        blocks = [block]
        for network_id in gateway.peer_network_ids:
            vpc_ref = ctx.reference(network_id)
            subnets = [
                ctx.reference(subnet.id)
                for subnet in ctx.architecture.infrastructure.subnets_of(network_id)
            ]
            subnets = [ref for ref in subnets if ref is not None]
            if vpc_ref is None or not subnets:
                ctx.warn(
                    "transit_attachment_incomplete",
                    f"transit gateway {gateway.logical_name!r} cannot attach network "
                    f"{network_id!r}: it has no known subnets",
                    gateway.id,
                    severity=WarningSeverity.HIGH,
                )
                continue
            attachment = Block(
                "resource",
                (
                    "aws_ec2_transit_gateway_vpc_attachment",
                    f"{name}_{terraform_identifier(network_id)}",
                ),
            )
            attachment.set("transit_gateway_id", Raw(f"aws_ec2_transit_gateway.{name}.id"))
            attachment.set("vpc_id", vpc_ref)
            attachment.set("subnet_ids", subnets)
            attachment.set("tags", ctx.base_tags(gateway))
            blocks.append(attachment)
        return blocks

    ctx.warn(
        "unsupported_gateway_type",
        f"gateway type {gateway.type.value!r} has no AWS mapping yet",
        gateway.id,
        severity=WarningSeverity.HIGH,
    )
    return []


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #
def map_security_group(group: SecurityGroup, ctx: MappingContext) -> list[Block]:
    name = terraform_identifier(group.id)
    vpc_ref = ctx.reference(group.network_id)
    if vpc_ref is None:
        ctx.warn(
            "unresolved_network_reference",
            f"security group {group.logical_name!r} references unknown network "
            f"{group.network_id!r}",
            group.id,
            severity=WarningSeverity.HIGH,
        )
        return []

    block = Block("resource", ("aws_security_group", name), comments=_provenance(group))
    block.set("name", group.logical_name)
    block.set("description", f"TopoForge security group for {group.logical_name}")
    block.set("vpc_id", vpc_ref)

    for rule in group.rules:
        rule_block = Block(
            "ingress" if rule.direction is SecurityRuleDirection.INGRESS else "egress"
        )
        rule_block.set("from_port", rule.from_port if rule.from_port is not None else 0)
        rule_block.set("to_port", rule.to_port if rule.to_port is not None else 0)
        rule_block.set("protocol", rule.protocol)
        if rule.source_security_group_id:
            peer = ctx.reference(rule.source_security_group_id)
            if peer is None:
                ctx.warn(
                    "unresolved_security_group_reference",
                    f"rule in {group.logical_name!r} references unknown security group "
                    f"{rule.source_security_group_id!r}",
                    group.id,
                    severity=WarningSeverity.HIGH,
                )
                continue
            rule_block.set("security_groups", [peer])
        elif rule.cidr_blocks:
            rule_block.set("cidr_blocks", list(rule.cidr_blocks))
        else:
            ctx.warn(
                "rule_without_source",
                f"a rule in {group.logical_name!r} names neither a CIDR nor a peer group "
                "and was skipped rather than defaulted to 0.0.0.0/0",
                group.id,
                severity=WarningSeverity.HIGH,
            )
            continue
        rule_block.set_if("description", rule.description)
        block.add(rule_block)

    block.set("tags", ctx.base_tags(group))
    return [block]


# --------------------------------------------------------------------------- #
# Compute and data
# --------------------------------------------------------------------------- #
def map_compute(instance: ComputeInstance, ctx: MappingContext) -> list[Block]:
    name = terraform_identifier(instance.id)
    block = Block("resource", ("aws_instance", name), comments=_provenance(instance))

    block.set("ami", Raw(instance.image) if _looks_like_reference(instance.image) else (
        instance.image or Raw("data.aws_ami.amazon_linux.id")
    ))
    block.set("instance_type", instance.instance_type)

    primary_subnet = instance.subnet_id or (
        instance.interfaces[0].subnet_id if instance.interfaces else None
    )
    subnet_ref = ctx.reference(primary_subnet) if primary_subnet else None
    if subnet_ref is None:
        ctx.warn(
            "compute_without_subnet",
            f"compute {instance.logical_name!r} has no subnet and cannot be placed",
            instance.id,
            severity=WarningSeverity.HIGH,
        )
        return []
    block.set("subnet_id", subnet_ref)

    security_groups = [
        ref
        for ref in (ctx.reference(group_id) for group_id in instance.security_group_ids)
        if ref is not None
    ]
    if security_groups:
        block.set("vpc_security_group_ids", security_groups)

    if instance.assign_public_ip:
        block.set("associate_public_ip_address", True)

    if instance.is_forwarder:
        # A router appliance forwards traffic that is not addressed to it, which AWS
        # blocks by default.
        block.set("source_dest_check", False)

    if instance.key_name:
        block.set("key_name", instance.key_name)
    else:
        block.set("key_name", Raw("var.ssh_key_name"))
        ctx.required_variables.add("ssh_key_name")

    if instance.root_volume_gb:
        root = Block("root_block_device")
        root.set("volume_size", instance.root_volume_gb)
        block.add(root)

    tags = ctx.base_tags(instance)
    if instance.roles:
        # The Ansible inventory groups hosts by this tag, which is how provisioning and
        # configuration stay linked without either owning the other.
        tags["topoforge:roles"] = ",".join(instance.roles)
    if instance.is_bastion:
        tags["topoforge:bastion"] = "true"
    block.set("tags", tags)

    blocks = [block]
    # Secondary NICs, for multi-homed router appliances.
    for nic in instance.interfaces:
        if nic.primary or nic.subnet_id == primary_subnet:
            continue
        nic_subnet = ctx.reference(nic.subnet_id)
        if nic_subnet is None:
            ctx.warn(
                "unresolved_interface_subnet",
                f"interface {nic.id!r} on {instance.logical_name!r} references unknown "
                f"subnet {nic.subnet_id!r}",
                instance.id,
            )
            continue
        nic_name = f"{name}_{terraform_identifier(nic.id)}"
        nic_block = Block("resource", ("aws_network_interface", nic_name))
        nic_block.set("subnet_id", nic_subnet)
        nic_block.set_if("private_ips", [nic.private_ip] if nic.private_ip else None)
        nic_block.set("source_dest_check", nic.source_dest_check)
        nic_block.set("tags", {**ctx.base_tags(instance), "Name": nic.id})

        attachment = Block("resource", ("aws_network_interface_attachment", nic_name))
        attachment.set("instance_id", Raw(f"aws_instance.{name}.id"))
        attachment.set("network_interface_id", Raw(f"aws_network_interface.{nic_name}.id"))
        attachment.set("device_index", len(blocks))
        blocks.extend([nic_block, attachment])

    ctx.outputs[f"{name}_private_ip"] = (
        Raw(f"aws_instance.{name}.private_ip"),
        f"Private address of {instance.logical_name}",
    )
    if instance.assign_public_ip:
        ctx.outputs[f"{name}_public_ip"] = (
            Raw(f"aws_instance.{name}.public_ip"),
            f"Public address of {instance.logical_name}",
        )
    return blocks


def map_database(database: Database, ctx: MappingContext) -> list[Block]:
    name = terraform_identifier(database.id)
    blocks: list[Block] = []

    subnet_refs = [
        ref
        for ref in (ctx.reference(subnet_id) for subnet_id in database.subnet_ids)
        if ref is not None
    ]
    if len(subnet_refs) < 2:
        ctx.warn(
            "db_subnet_group_needs_two_subnets",
            f"database {database.logical_name!r} needs subnets in at least two "
            f"availability zones; {len(subnet_refs)} were provided",
            database.id,
            severity=WarningSeverity.HIGH,
        )
        return []

    subnet_group = Block("resource", ("aws_db_subnet_group", name), comments=_provenance(database))
    subnet_group.set("name", f"{database.logical_name}-subnets")
    subnet_group.set("subnet_ids", subnet_refs)
    subnet_group.set("tags", ctx.base_tags(database))
    blocks.append(subnet_group)

    block = Block("resource", ("aws_db_instance", name))
    block.set("identifier", database.logical_name)
    block.set("engine", database.engine.value)
    block.set_if("engine_version", database.engine_version)
    block.set("instance_class", database.instance_class)
    block.set("allocated_storage", database.allocated_storage_gb)
    block.set("multi_az", database.multi_az)
    block.set("db_subnet_group_name", Raw(f"aws_db_subnet_group.{name}.name"))
    security_groups = [
        ref
        for ref in (ctx.reference(group_id) for group_id in database.security_group_ids)
        if ref is not None
    ]
    if security_groups:
        block.set("vpc_security_group_ids", security_groups)
    block.set("publicly_accessible", database.publicly_accessible)
    block.set("skip_final_snapshot", True)
    # Credentials are never generated into code. The operator supplies them, and the
    # variable is declared sensitive.
    block.set("username", Raw(f"var.{name}_username"))
    block.set("password", Raw(f"var.{name}_password"))
    ctx.required_variables.add(f"{name}_username")
    ctx.required_variables.add(f"{name}_password")
    block.set("tags", ctx.base_tags(database))
    blocks.append(block)

    ctx.outputs[f"{name}_endpoint"] = (
        Raw(f"aws_db_instance.{name}.endpoint"),
        f"Endpoint of {database.logical_name}",
    )
    return blocks


def map_load_balancer(balancer: LoadBalancer, ctx: MappingContext) -> list[Block]:
    name = terraform_identifier(balancer.id)
    subnet_refs = [
        ref
        for ref in (ctx.reference(subnet_id) for subnet_id in balancer.subnet_ids)
        if ref is not None
    ]
    if not subnet_refs:
        ctx.warn(
            "load_balancer_without_subnets",
            f"load balancer {balancer.logical_name!r} has no subnets",
            balancer.id,
            severity=WarningSeverity.HIGH,
        )
        return []

    block = Block("resource", ("aws_lb", name), comments=_provenance(balancer))
    block.set("name", balancer.logical_name)
    block.set("internal", balancer.scheme is LoadBalancerScheme.INTERNAL)
    block.set(
        "load_balancer_type",
        "application" if balancer.type is LoadBalancerType.APPLICATION else "network",
    )
    block.set("subnets", subnet_refs)
    security_groups = [
        ref
        for ref in (ctx.reference(group_id) for group_id in balancer.security_group_ids)
        if ref is not None
    ]
    if security_groups and balancer.type is LoadBalancerType.APPLICATION:
        block.set("security_groups", security_groups)
    block.set("tags", ctx.base_tags(balancer))
    blocks = [block]

    network_ref = ctx.reference(balancer.network_id) if balancer.network_id else None
    for index, listener in enumerate(balancer.listeners):
        if network_ref is None:
            ctx.warn(
                "listener_without_network",
                f"listener {index} on {balancer.logical_name!r} needs a known network for "
                "its target group",
                balancer.id,
                severity=WarningSeverity.HIGH,
            )
            break
        group_name = f"{name}_tg_{index}"
        target_group = Block("resource", ("aws_lb_target_group", group_name))
        target_group.set("name", f"{balancer.logical_name}-tg-{index}")
        target_group.set("port", listener.target_port or listener.port)
        target_group.set("protocol", listener.target_protocol or listener.protocol)
        target_group.set("vpc_id", network_ref)
        target_group.set("tags", ctx.base_tags(balancer))
        blocks.append(target_group)

        listener_block = Block("resource", ("aws_lb_listener", f"{name}_listener_{index}"))
        listener_block.set("load_balancer_arn", Raw(f"aws_lb.{name}.arn"))
        listener_block.set("port", listener.port)
        listener_block.set("protocol", listener.protocol)
        if listener.certificate_ref:
            listener_block.set("certificate_arn", listener.certificate_ref)
        action = Block("default_action")
        action.set("type", "forward")
        action.set("target_group_arn", Raw(f"aws_lb_target_group.{group_name}.arn"))
        listener_block.add(action)
        blocks.append(listener_block)

        for target_id in balancer.target_compute_ids:
            target_ref = ctx.reference(target_id)
            if target_ref is None:
                continue
            attachment = Block(
                "resource",
                (
                    "aws_lb_target_group_attachment",
                    f"{group_name}_{terraform_identifier(target_id)}",
                ),
            )
            attachment.set("target_group_arn", Raw(f"aws_lb_target_group.{group_name}.arn"))
            attachment.set("target_id", target_ref)
            attachment.set("port", listener.target_port or listener.port)
            blocks.append(attachment)

    ctx.outputs[f"{name}_dns_name"] = (
        Raw(f"aws_lb.{name}.dns_name"),
        f"DNS name of {balancer.logical_name}",
    )
    return blocks


# --------------------------------------------------------------------------- #
def build_registry() -> dict[str, ResourceMapper]:
    """The single mapping table. One entry per cloud resource kind."""
    return {
        "virtual_network": map_virtual_network,
        "subnet": map_subnet,
        "route_table": map_route_table,
        "gateway": map_gateway,
        "security_group": map_security_group,
        "compute": map_compute,
        "database": map_database,
        "load_balancer": map_load_balancer,
    }


# --------------------------------------------------------------------------- #
def _provenance(resource: Any) -> tuple[str, ...]:
    """Comment lines tying a Terraform resource back to the diagram it came from."""
    if not getattr(resource, "source_node_ids", ()):
        return ()
    return (f"From network component(s): {', '.join(resource.source_node_ids)}",)


def _looks_like_reference(value: str | None) -> bool:
    """True for a Terraform expression such as ``data.aws_ami.x.id``, false for an AMI id."""
    return bool(value) and not value.startswith("ami-") and "." in value  # type: ignore[union-attr]


def _route_target_argument(target_ref: str, ctx: MappingContext) -> str:
    """Pick the right route argument for the kind of thing being routed to."""
    address = ctx.addresses.get(target_ref)
    terraform_type = address[0] if address else ""
    return {
        "aws_internet_gateway": "gateway_id",
        "aws_nat_gateway": "nat_gateway_id",
        "aws_ec2_transit_gateway": "transit_gateway_id",
        "aws_vpc_peering_connection": "vpc_peering_connection_id",
        "aws_vpn_gateway": "gateway_id",
        "aws_instance": "instance_id",
        "aws_network_interface": "network_interface_id",
    }.get(terraform_type, "gateway_id")


def _subnet_index(subnet: Subnet, ctx: MappingContext) -> int:
    """Spread subnets across availability zones deterministically.

    Index by position within the subnet's own VPC, so adding a VPC never reshuffles the
    AZ assignment of an existing one.
    """
    siblings = ctx.architecture.infrastructure.subnets_of(subnet.network_id)
    for index, candidate in enumerate(siblings):
        if candidate.id == subnet.id:
            return index % 3
    return 0
