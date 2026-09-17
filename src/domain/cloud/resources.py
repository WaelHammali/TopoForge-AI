"""Cloud resource vocabulary.

Provider-neutral shapes with an AWS-first vocabulary. These describe *cloud architecture*,
not infrastructure-as-code: there is no HCL, no Terraform resource address, no module path
and no ``depends_on`` anywhere in this module. Mapping a :class:`CloudResource` onto a
Terraform resource is the generator's job and lives in
``src/infrastructure/generators/terraform/mappings``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.domain.common.provenance import Evidence
from src.domain.network.models import SubnetSpec

__all__ = [
    "CloudProvider",
    "CloudResource",
    "ComputeInstance",
    "ComputePlatform",
    "Database",
    "DatabaseEngine",
    "Gateway",
    "GatewayType",
    "Infrastructure",
    "LoadBalancer",
    "LoadBalancerScheme",
    "LoadBalancerType",
    "Listener",
    "NetworkInterfaceSpec",
    "ResourceKind",
    "Route",
    "RouteTable",
    "SecurityGroup",
    "SecurityRule",
    "SecurityRuleDirection",
    "Subnet",
    "SubnetPurpose",
    "VirtualNetwork",
]


class CloudProvider(str, Enum):
    AWS = "aws"
    # Declared so the schema is honest about its extension points. Only AWS is mapped.
    AZURE = "azure"
    GCP = "gcp"


class ResourceKind(str, Enum):
    VIRTUAL_NETWORK = "virtual_network"
    SUBNET = "subnet"
    ROUTE_TABLE = "route_table"
    GATEWAY = "gateway"
    COMPUTE = "compute"
    DATABASE = "database"
    LOAD_BALANCER = "load_balancer"
    SECURITY_GROUP = "security_group"
    NETWORK_FIREWALL = "network_firewall"
    IAM = "iam"


class CloudResource(BaseModel):
    """Fields every cloud resource carries.

    ``source_node_ids`` is the traceability link back into the network architecture: it
    answers "which router became this VPC?" and drives highlighting in the UI.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: ResourceKind
    #: Stable, human-meaningful name used for tagging and for generator identifiers.
    logical_name: str
    #: Provider's own type name, e.g. "aws_vpc". A *cloud* type, not a Terraform address.
    provider_type: str | None = None
    #: Ids of the network-architecture nodes/edges that produced this resource.
    source_node_ids: tuple[str, ...] = ()
    tags: dict[str, str] = Field(default_factory=dict)
    evidence: Evidence | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("logical_name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("logical_name must not be empty")
        return value.strip()


class VirtualNetwork(CloudResource):
    """A VPC (AWS) / VNet (Azure) / VPC network (GCP)."""

    kind: ResourceKind = ResourceKind.VIRTUAL_NETWORK
    provider_type: str | None = "aws_vpc"
    cidr: SubnetSpec
    secondary_cidrs: tuple[SubnetSpec, ...] = ()
    enable_dns_support: bool = True
    enable_dns_hostnames: bool = True
    region: str | None = None


class SubnetPurpose(str, Enum):
    LAN = "lan"
    TRANSIT = "transit"
    MANAGEMENT = "management"
    DATABASE = "database"
    PUBLIC_EDGE = "public_edge"


class Subnet(CloudResource):
    kind: ResourceKind = ResourceKind.SUBNET
    provider_type: str | None = "aws_subnet"
    network_id: str
    cidr: SubnetSpec
    availability_zone: str | None = None
    #: A public subnet routes 0.0.0.0/0 to an internet gateway.
    public: bool = False
    purpose: SubnetPurpose = SubnetPurpose.LAN
    #: VLAN this subnet represents, when it came from an L2 segment.
    source_vlan_id: int | None = None


class Route(BaseModel):
    model_config = ConfigDict(frozen=True)

    destination: SubnetSpec
    #: Id of the gateway / interface / peering resource that carries the traffic.
    target_ref: str
    target_kind: str = "gateway"
    description: str | None = None


class RouteTable(CloudResource):
    kind: ResourceKind = ResourceKind.ROUTE_TABLE
    provider_type: str | None = "aws_route_table"
    network_id: str
    routes: tuple[Route, ...] = ()
    associated_subnet_ids: tuple[str, ...] = ()
    is_main: bool = False


class GatewayType(str, Enum):
    INTERNET = "internet"
    NAT = "nat"
    TRANSIT = "transit"
    VPN = "vpn"
    PEERING = "peering"
    VIRTUAL_PRIVATE = "virtual_private"


class Gateway(CloudResource):
    kind: ResourceKind = ResourceKind.GATEWAY
    type: GatewayType
    network_id: str | None = None
    #: NAT gateways live in a subnet; internet gateways attach to the network.
    subnet_id: str | None = None
    #: For peering/transit: the other networks involved.
    peer_network_ids: tuple[str, ...] = ()
    allocate_public_ip: bool = False


class ComputePlatform(str, Enum):
    VIRTUAL_MACHINE = "virtual_machine"
    CONTAINER = "container"
    SERVERLESS = "serverless"


class NetworkInterfaceSpec(BaseModel):
    """A NIC on a compute instance, tied to a subnet."""

    model_config = ConfigDict(frozen=True)

    id: str
    subnet_id: str
    private_ip: str | None = None
    #: Router appliances forward traffic that is not addressed to them.
    source_dest_check: bool = True
    primary: bool = False
    security_group_ids: tuple[str, ...] = ()
    #: Interface in the source network architecture, for traceability.
    source_interface_id: str | None = None


class ComputeInstance(CloudResource):
    kind: ResourceKind = ResourceKind.COMPUTE
    provider_type: str | None = "aws_instance"
    platform: ComputePlatform = ComputePlatform.VIRTUAL_MACHINE
    #: e.g. "t3.micro". Chosen by the cloud mapping layer, not by the generator.
    instance_type: str = "t3.micro"
    image: str | None = None
    subnet_id: str | None = None
    interfaces: tuple[NetworkInterfaceSpec, ...] = ()
    security_group_ids: tuple[str, ...] = ()
    assign_public_ip: bool = False
    key_name: str | None = None
    #: Roles this host plays, e.g. ("router",) or ("web",). Links infrastructure to
    #: configuration: the Ansible generator groups hosts by these.
    roles: tuple[str, ...] = ()
    is_bastion: bool = False
    root_volume_gb: int | None = None
    #: Whether this instance forwards traffic (a router appliance).
    is_forwarder: bool = False


class DatabaseEngine(str, Enum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    MARIADB = "mariadb"
    SQLSERVER = "sqlserver"
    ORACLE = "oracle"
    UNSPECIFIED = "unspecified"


class Database(CloudResource):
    kind: ResourceKind = ResourceKind.DATABASE
    provider_type: str | None = "aws_db_instance"
    engine: DatabaseEngine = DatabaseEngine.UNSPECIFIED
    engine_version: str | None = None
    instance_class: str = "db.t3.micro"
    allocated_storage_gb: int = 20
    multi_az: bool = False
    subnet_ids: tuple[str, ...] = ()
    security_group_ids: tuple[str, ...] = ()
    publicly_accessible: bool = False
    #: Never a literal password. Names the secret to resolve at deploy time.
    credentials_secret_ref: str | None = None


class LoadBalancerType(str, Enum):
    APPLICATION = "application"
    NETWORK = "network"
    GATEWAY = "gateway"


class LoadBalancerScheme(str, Enum):
    INTERNET_FACING = "internet_facing"
    INTERNAL = "internal"


class Listener(BaseModel):
    model_config = ConfigDict(frozen=True)

    protocol: str = "HTTP"
    port: int = Field(default=80, ge=1, le=65535)
    target_port: int | None = Field(default=None, ge=1, le=65535)
    target_protocol: str | None = None
    certificate_ref: str | None = None


class LoadBalancer(CloudResource):
    kind: ResourceKind = ResourceKind.LOAD_BALANCER
    provider_type: str | None = "aws_lb"
    type: LoadBalancerType = LoadBalancerType.APPLICATION
    scheme: LoadBalancerScheme = LoadBalancerScheme.INTERNAL
    network_id: str | None = None
    subnet_ids: tuple[str, ...] = ()
    security_group_ids: tuple[str, ...] = ()
    listeners: tuple[Listener, ...] = ()
    #: Compute resource ids behind the load balancer.
    target_compute_ids: tuple[str, ...] = ()


class SecurityRuleDirection(str, Enum):
    INGRESS = "ingress"
    EGRESS = "egress"


class SecurityRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    direction: SecurityRuleDirection
    protocol: str = "tcp"
    from_port: int | None = Field(default=None, ge=-1, le=65535)
    to_port: int | None = Field(default=None, ge=-1, le=65535)
    #: Exactly one source form: a CIDR list, or another security group.
    cidr_blocks: tuple[str, ...] = ()
    source_security_group_id: str | None = None
    description: str | None = None


class SecurityGroup(CloudResource):
    kind: ResourceKind = ResourceKind.SECURITY_GROUP
    provider_type: str | None = "aws_security_group"
    network_id: str
    rules: tuple[SecurityRule, ...] = ()
    #: Set when this group is the cloud realisation of a firewall node in the diagram.
    source_firewall_node_id: str | None = None


class Infrastructure(BaseModel):
    """Everything Terraform provisions. Ansible never owns anything in here."""

    model_config = ConfigDict(frozen=True)

    networks: tuple[VirtualNetwork, ...] = ()
    subnets: tuple[Subnet, ...] = ()
    route_tables: tuple[RouteTable, ...] = ()
    gateways: tuple[Gateway, ...] = ()
    compute: tuple[ComputeInstance, ...] = ()
    databases: tuple[Database, ...] = ()
    load_balancers: tuple[LoadBalancer, ...] = ()
    security_groups: tuple[SecurityGroup, ...] = ()

    def all_resources(self) -> tuple[CloudResource, ...]:
        return (
            *self.networks,
            *self.subnets,
            *self.route_tables,
            *self.gateways,
            *self.compute,
            *self.databases,
            *self.load_balancers,
            *self.security_groups,
        )

    def resource(self, resource_id: str) -> CloudResource | None:
        return next((item for item in self.all_resources() if item.id == resource_id), None)

    def subnets_of(self, network_id: str) -> tuple[Subnet, ...]:
        return tuple(subnet for subnet in self.subnets if subnet.network_id == network_id)

    def compute_in_subnet(self, subnet_id: str) -> tuple[ComputeInstance, ...]:
        return tuple(
            instance
            for instance in self.compute
            if instance.subnet_id == subnet_id
            or any(nic.subnet_id == subnet_id for nic in instance.interfaces)
        )

    @property
    def is_empty(self) -> bool:
        return not self.all_resources()

    def counts(self) -> dict[str, int]:
        return {
            "networks": len(self.networks),
            "subnets": len(self.subnets),
            "route_tables": len(self.route_tables),
            "gateways": len(self.gateways),
            "compute": len(self.compute),
            "databases": len(self.databases),
            "load_balancers": len(self.load_balancers),
            "security_groups": len(self.security_groups),
        }
