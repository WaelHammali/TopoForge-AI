"""Cloud architecture validation.

The gate between the RAG and the generators. The RAG performs no validation of its own —
its contract says so explicitly — so everything that would otherwise reach a generator as
a malformed or dangerous cloud design is caught here.

Each validator is small and independent. Errors block generation; warnings and
recommendations do not.
"""

from __future__ import annotations

import ipaddress
from abc import ABC, abstractmethod

from src.domain.architecture.issues import IssueSeverity, ValidationIssue, ValidationReport
from src.domain.cloud.models import CloudArchitecture
from src.domain.cloud.resources import GatewayType, SecurityRuleDirection

__all__ = [
    "CloudArchitectureValidatorSuite",
    "CloudValidator",
    "ComputePlacementValidator",
    "ConfigurationCoherenceValidator",
    "PublicExposureValidator",
    "RouteTableValidator",
    "SubnetValidator",
    "default_cloud_validators",
]

#: Ports that should never be reachable from the whole internet without a deliberate
#: decision. Reported, never silently rewritten.
_SENSITIVE_PORTS: dict[int, str] = {
    22: "SSH",
    3389: "RDP",
    3306: "MySQL",
    5432: "PostgreSQL",
    6379: "Redis",
    27017: "MongoDB",
    9200: "Elasticsearch",
    1433: "SQL Server",
}
_WORLD = frozenset({"0.0.0.0/0", "::/0"})


class CloudValidator(ABC):
    """One independent check over a cloud architecture."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def validate(self, architecture: CloudArchitecture) -> list[ValidationIssue]: ...


class SubnetValidator(CloudValidator):
    """Subnets must fit inside their network and must not overlap one another."""

    @property
    def name(self) -> str:
        return "SubnetValidator"

    def validate(self, architecture: CloudArchitecture) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        infrastructure = architecture.infrastructure
        networks = {network.id: network for network in infrastructure.networks}

        for subnet in infrastructure.subnets:
            network = networks.get(subnet.network_id)
            if network is None:
                continue
            if not subnet.cidr.is_subnet_of(network.cidr) and not any(
                subnet.cidr.is_subnet_of(secondary) for secondary in network.secondary_cidrs
            ):
                issues.append(
                    ValidationIssue(
                        code="subnet_outside_network",
                        severity=IssueSeverity.ERROR,
                        message=(
                            f"subnet {subnet.logical_name} ({subnet.cidr}) is not inside "
                            f"{network.logical_name} ({network.cidr})"
                        ),
                        subject_ids=(subnet.id, network.id),
                        validator=self.name,
                        remediation="widen the VPC CIDR or re-address the subnet",
                    )
                )
            if subnet.cidr.usable_host_count < 2:
                issues.append(
                    ValidationIssue(
                        code="subnet_too_small",
                        severity=IssueSeverity.WARNING,
                        message=(
                            f"subnet {subnet.logical_name} ({subnet.cidr}) has "
                            f"{subnet.cidr.usable_host_count} usable address(es); AWS also "
                            "reserves five addresses per subnet"
                        ),
                        subject_ids=(subnet.id,),
                        validator=self.name,
                    )
                )

        by_network: dict[str, list] = {}
        for subnet in infrastructure.subnets:
            by_network.setdefault(subnet.network_id, []).append(subnet)

        for network_id, subnets in by_network.items():
            for index, first in enumerate(subnets):
                for second in subnets[index + 1 :]:
                    if first.cidr.overlaps(second.cidr):
                        issues.append(
                            ValidationIssue(
                                code="overlapping_subnets",
                                severity=IssueSeverity.ERROR,
                                message=(
                                    f"subnets {first.logical_name} ({first.cidr}) and "
                                    f"{second.logical_name} ({second.cidr}) overlap inside "
                                    f"{network_id}"
                                ),
                                subject_ids=(first.id, second.id),
                                validator=self.name,
                            )
                        )
        return issues


class ComputePlacementValidator(CloudValidator):
    """Instances must be placed, and their addresses must belong to their subnet."""

    @property
    def name(self) -> str:
        return "ComputePlacementValidator"

    def validate(self, architecture: CloudArchitecture) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        subnets = {subnet.id: subnet for subnet in architecture.infrastructure.subnets}
        seen: dict[str, str] = {}

        for instance in architecture.infrastructure.compute:
            if instance.subnet_id is None and not instance.interfaces:
                issues.append(
                    ValidationIssue(
                        code="compute_not_placed",
                        severity=IssueSeverity.ERROR,
                        message=f"{instance.logical_name} is not placed in any subnet",
                        subject_ids=(instance.id,),
                        validator=self.name,
                    )
                )
                continue

            for nic in instance.interfaces:
                subnet = subnets.get(nic.subnet_id)
                if subnet is None or nic.private_ip is None:
                    continue
                if not subnet.cidr.contains(nic.private_ip):
                    issues.append(
                        ValidationIssue(
                            code="address_outside_subnet",
                            severity=IssueSeverity.ERROR,
                            message=(
                                f"{instance.logical_name} interface {nic.id} has address "
                                f"{nic.private_ip}, which is not in {subnet.cidr}"
                            ),
                            subject_ids=(instance.id, subnet.id),
                            validator=self.name,
                        )
                    )
                    continue

                previous = seen.get(nic.private_ip)
                if previous is not None and previous != instance.id:
                    issues.append(
                        ValidationIssue(
                            code="duplicate_address",
                            severity=IssueSeverity.ERROR,
                            message=(
                                f"{nic.private_ip} is assigned to both {previous} and "
                                f"{instance.id}"
                            ),
                            subject_ids=(previous, instance.id),
                            validator=self.name,
                        )
                    )
                seen[nic.private_ip] = instance.id

                if _is_aws_reserved(nic.private_ip, subnet.cidr.cidr):
                    issues.append(
                        ValidationIssue(
                            code="aws_reserved_address",
                            severity=IssueSeverity.ERROR,
                            message=(
                                f"{nic.private_ip} is one of the five addresses AWS reserves "
                                f"in {subnet.cidr} and cannot be assigned"
                            ),
                            subject_ids=(instance.id,),
                            validator=self.name,
                            remediation="re-address the interface or widen the subnet",
                        )
                    )

            if instance.is_forwarder and any(nic.source_dest_check for nic in instance.interfaces):
                issues.append(
                    ValidationIssue(
                        code="forwarder_with_source_dest_check",
                        severity=IssueSeverity.ERROR,
                        message=(
                            f"{instance.logical_name} forwards traffic but has an interface "
                            "with source/destination checking enabled, which AWS will drop"
                        ),
                        subject_ids=(instance.id,),
                        validator=self.name,
                    )
                )
        return issues


class RouteTableValidator(CloudValidator):
    """Routes must target something that exists, and subnets should be routable."""

    @property
    def name(self) -> str:
        return "RouteTableValidator"

    def validate(self, architecture: CloudArchitecture) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        infrastructure = architecture.infrastructure
        resource_ids = {resource.id for resource in infrastructure.all_resources()}
        associated: set[str] = set()

        for table in infrastructure.route_tables:
            associated.update(table.associated_subnet_ids)
            destinations: set[str] = set()
            for route in table.routes:
                if route.target_ref not in resource_ids:
                    issues.append(
                        ValidationIssue(
                            code="route_target_missing",
                            severity=IssueSeverity.ERROR,
                            message=(
                                f"route to {route.destination} in {table.logical_name} targets "
                                f"{route.target_ref}, which is not in this architecture"
                            ),
                            subject_ids=(table.id,),
                            validator=self.name,
                        )
                    )
                if route.destination.cidr in destinations:
                    issues.append(
                        ValidationIssue(
                            code="duplicate_route_destination",
                            severity=IssueSeverity.ERROR,
                            message=(
                                f"{table.logical_name} has two routes for "
                                f"{route.destination}"
                            ),
                            subject_ids=(table.id,),
                            validator=self.name,
                        )
                    )
                destinations.add(route.destination.cidr)

        for subnet in infrastructure.subnets:
            if subnet.id not in associated:
                issues.append(
                    ValidationIssue(
                        code="subnet_without_route_table",
                        severity=IssueSeverity.WARNING,
                        message=(
                            f"subnet {subnet.logical_name} is associated with no route table "
                            "and will fall back to the VPC's main table"
                        ),
                        subject_ids=(subnet.id,),
                        validator=self.name,
                    )
                )

        for gateway in infrastructure.gateways:
            if gateway.type is GatewayType.NAT and gateway.subnet_id is None:
                issues.append(
                    ValidationIssue(
                        code="nat_gateway_without_subnet",
                        severity=IssueSeverity.ERROR,
                        message=(
                            f"NAT gateway {gateway.logical_name} is not placed in a subnet; "
                            "it must live in a public one"
                        ),
                        subject_ids=(gateway.id,),
                        validator=self.name,
                    )
                )
            if gateway.type is GatewayType.PEERING and len(gateway.peer_network_ids) != 2:
                issues.append(
                    ValidationIssue(
                        code="peering_arity",
                        severity=IssueSeverity.ERROR,
                        message=(
                            f"peering {gateway.logical_name} names "
                            f"{len(gateway.peer_network_ids)} networks; it must name two"
                        ),
                        subject_ids=(gateway.id,),
                        validator=self.name,
                        remediation="use a transit gateway for three or more domains",
                    )
                )
        return issues


class PublicExposureValidator(CloudValidator):
    """Exposure to the whole internet is reported, never quietly accepted."""

    @property
    def name(self) -> str:
        return "PublicExposureValidator"

    def validate(self, architecture: CloudArchitecture) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for group in architecture.infrastructure.security_groups:
            for rule in group.rules:
                if rule.direction is not SecurityRuleDirection.INGRESS:
                    continue
                if not (set(rule.cidr_blocks) & _WORLD):
                    continue

                if rule.protocol in {"-1", "all"} or rule.from_port is None:
                    issues.append(
                        ValidationIssue(
                            code="world_open_all_ports",
                            severity=IssueSeverity.ERROR,
                            message=(
                                f"security group {group.logical_name} allows every port from "
                                "the whole internet"
                            ),
                            subject_ids=(group.id,),
                            validator=self.name,
                            remediation="restrict the port range and the source CIDR",
                        )
                    )
                    continue

                for port in range(rule.from_port, (rule.to_port or rule.from_port) + 1):
                    service = _SENSITIVE_PORTS.get(port)
                    if service is None:
                        continue
                    issues.append(
                        ValidationIssue(
                            code="sensitive_port_open_to_world",
                            severity=IssueSeverity.WARNING,
                            message=(
                                f"security group {group.logical_name} exposes {service} "
                                f"(port {port}) to 0.0.0.0/0"
                            ),
                            subject_ids=(group.id,),
                            validator=self.name,
                            remediation=(
                                "restrict the source to an administrative CIDR, or reach the "
                                "host through a bastion"
                            ),
                        )
                    )

        for database in architecture.infrastructure.databases:
            if database.publicly_accessible:
                issues.append(
                    ValidationIssue(
                        code="publicly_accessible_database",
                        severity=IssueSeverity.WARNING,
                        message=f"database {database.logical_name} is publicly accessible",
                        subject_ids=(database.id,),
                        validator=self.name,
                    )
                )
        return issues


class ConfigurationCoherenceValidator(CloudValidator):
    """The configuration half must be consistent with the infrastructure half."""

    @property
    def name(self) -> str:
        return "ConfigurationCoherenceValidator"

    def validate(self, architecture: CloudArchitecture) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        configuration = architecture.configuration

        for name in configuration.unresolved_role_names():
            issues.append(
                ValidationIssue(
                    code="undefined_role",
                    severity=IssueSeverity.ERROR,
                    message=f"a host references role {name!r}, which is not defined",
                    validator=self.name,
                    remediation="define the role or remove the reference",
                )
            )

        compute_by_id = {
            instance.id: instance for instance in architecture.infrastructure.compute
        }
        for host in configuration.hosts:
            instance = compute_by_id.get(host.compute_id)
            if instance is None:
                continue
            if host.requires_bastion and not any(
                candidate.is_bastion for candidate in architecture.infrastructure.compute
            ):
                issues.append(
                    ValidationIssue(
                        code="bastion_required_but_absent",
                        severity=IssueSeverity.WARNING,
                        message=(
                            f"host {host.name} is only reachable through a bastion, but the "
                            "architecture defines none; configuration will not be able to "
                            "connect after deployment"
                        ),
                        subject_ids=(host.id,),
                        validator=self.name,
                    )
                )

        for instance in architecture.infrastructure.compute:
            if not instance.roles:
                continue
            if not any(host.compute_id == instance.id for host in configuration.hosts):
                issues.append(
                    ValidationIssue(
                        code="compute_role_without_configuration",
                        severity=IssueSeverity.RECOMMENDATION,
                        message=(
                            f"{instance.logical_name} declares roles "
                            f"{list(instance.roles)} but no configuration host targets it"
                        ),
                        subject_ids=(instance.id,),
                        validator=self.name,
                    )
                )
        return issues


def default_cloud_validators() -> tuple[CloudValidator, ...]:
    return (
        SubnetValidator(),
        ComputePlacementValidator(),
        RouteTableValidator(),
        PublicExposureValidator(),
        ConfigurationCoherenceValidator(),
    )


class CloudArchitectureValidatorSuite:
    """Runs every validator and produces one report."""

    def __init__(self, validators: tuple[CloudValidator, ...] | None = None):
        self._validators = validators if validators is not None else default_cloud_validators()

    def validate(self, architecture: CloudArchitecture) -> ValidationReport:
        issues: list[ValidationIssue] = []
        for validator in self._validators:
            issues.extend(validator.validate(architecture))
        return ValidationReport.from_issues(
            issues, tuple(validator.name for validator in self._validators)
        )


def _is_aws_reserved(address: str, cidr: str) -> bool:
    """AWS reserves the first four addresses and the last in every subnet."""
    try:
        network = ipaddress.ip_network(cidr, strict=True)
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    if candidate not in network or network.version != 4:
        return False
    offset = int(candidate) - int(network.network_address)
    return offset <= 3 or candidate == network.broadcast_address
