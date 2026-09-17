"""Fixed cloud architectures used by the deterministic generator tests.

Built in code rather than loaded from JSON so that a schema change breaks the fixture
loudly at construction, instead of silently producing a half-populated document.
``as_json_fixture`` writes the same object out as ``cloud_architecture.json`` for the
end-to-end test.
"""

from __future__ import annotations

from src.domain.cloud import (
    CloudArchitecture,
    CloudArchitectureMetadata,
    ComputeInstance,
    ConfigFile,
    Configuration,
    ConfigurationHost,
    Database,
    DatabaseEngine,
    Gateway,
    GatewayType,
    Infrastructure,
    Listener,
    LoadBalancer,
    LoadBalancerScheme,
    NetworkInterfaceSpec,
    PackageRequirement,
    Relationship,
    RelationshipType,
    Route,
    RouteTable,
    SecurityGroup,
    SecurityRule,
    SecurityRuleDirection,
    ServiceRequirement,
    Subnet,
    SubnetPurpose,
    SystemRole,
    VirtualNetwork,
)
from src.domain.network import SubnetSpec

__all__ = ["minimal_cloud", "full_cloud", "two_vpc_peered_cloud"]


def minimal_cloud() -> CloudArchitecture:
    """One VPC, one public subnet, one instance. The smallest generatable thing."""
    return CloudArchitecture(
        metadata=CloudArchitectureMetadata(
            cloud_architecture_id="cloud_fixture_minimal",
            name="minimal",
            region="eu-west-1",
            network_architecture_id="arch_fixture",
            network_architecture_revision=1,
            deployment_pattern="single_vpc",
        ),
        infrastructure=Infrastructure(
            networks=(
                VirtualNetwork(
                    id="vpc-r1",
                    logical_name="r1-vpc",
                    cidr=SubnetSpec(cidr="10.0.0.0/16"),
                    source_node_ids=("R1",),
                ),
            ),
            subnets=(
                Subnet(
                    id="sn-lan",
                    logical_name="sw1-lan",
                    network_id="vpc-r1",
                    cidr=SubnetSpec(cidr="10.0.1.0/24"),
                    public=True,
                    source_node_ids=("SW1",),
                ),
            ),
            compute=(
                ComputeInstance(
                    id="c-s1",
                    logical_name="s1",
                    subnet_id="sn-lan",
                    instance_type="t3.micro",
                    source_node_ids=("S1",),
                ),
            ),
        ),
    )


def full_cloud() -> CloudArchitecture:
    """Public/private split with NAT, bastion, a router appliance, RDS and an ALB."""
    vpc = VirtualNetwork(
        id="vpc-r1",
        logical_name="r1-vpc",
        cidr=SubnetSpec(cidr="10.0.0.0/16"),
        source_node_ids=("R1",),
    )
    public = Subnet(
        id="sn-public",
        logical_name="r1-public",
        network_id="vpc-r1",
        cidr=SubnetSpec(cidr="10.0.1.0/24"),
        public=True,
        purpose=SubnetPurpose.PUBLIC_EDGE,
        availability_zone="eu-west-1a",
    )
    private = Subnet(
        id="sn-private",
        logical_name="r1-private",
        network_id="vpc-r1",
        cidr=SubnetSpec(cidr="10.0.2.0/24"),
        source_node_ids=("SW1",),
        availability_zone="eu-west-1b",
    )
    db_subnet = Subnet(
        id="sn-db",
        logical_name="r1-db",
        network_id="vpc-r1",
        cidr=SubnetSpec(cidr="10.0.3.0/24"),
        purpose=SubnetPurpose.DATABASE,
        availability_zone="eu-west-1c",
    )
    igw = Gateway(
        id="gw-igw", logical_name="r1-igw", type=GatewayType.INTERNET, network_id="vpc-r1"
    )
    nat = Gateway(
        id="gw-nat",
        logical_name="r1-nat",
        type=GatewayType.NAT,
        network_id="vpc-r1",
        subnet_id="sn-public",
    )
    public_rt = RouteTable(
        id="rt-public",
        logical_name="r1-public-rt",
        network_id="vpc-r1",
        routes=(Route(destination=SubnetSpec(cidr="0.0.0.0/0"), target_ref="gw-igw"),),
        associated_subnet_ids=("sn-public",),
    )
    private_rt = RouteTable(
        id="rt-private",
        logical_name="r1-private-rt",
        network_id="vpc-r1",
        routes=(Route(destination=SubnetSpec(cidr="0.0.0.0/0"), target_ref="gw-nat"),),
        associated_subnet_ids=("sn-private", "sn-db"),
    )
    bastion_sg = SecurityGroup(
        id="sg-bastion",
        logical_name="bastion",
        network_id="vpc-r1",
        rules=(
            SecurityRule(
                direction=SecurityRuleDirection.INGRESS,
                protocol="tcp",
                from_port=22,
                to_port=22,
                cidr_blocks=("0.0.0.0/0",),
                description="SSH from the admin network",
            ),
            SecurityRule(
                direction=SecurityRuleDirection.EGRESS,
                protocol="-1",
                from_port=0,
                to_port=0,
                cidr_blocks=("0.0.0.0/0",),
            ),
        ),
    )
    app_sg = SecurityGroup(
        id="sg-app",
        logical_name="app",
        network_id="vpc-r1",
        source_firewall_node_id="FW1",
        rules=(
            SecurityRule(
                direction=SecurityRuleDirection.INGRESS,
                protocol="tcp",
                from_port=22,
                to_port=22,
                source_security_group_id="sg-bastion",
                description="SSH only from the bastion",
            ),
            SecurityRule(
                direction=SecurityRuleDirection.INGRESS,
                protocol="tcp",
                from_port=80,
                to_port=80,
                cidr_blocks=("10.0.0.0/16",),
            ),
        ),
    )
    db_sg = SecurityGroup(
        id="sg-db",
        logical_name="db",
        network_id="vpc-r1",
        rules=(
            SecurityRule(
                direction=SecurityRuleDirection.INGRESS,
                protocol="tcp",
                from_port=5432,
                to_port=5432,
                source_security_group_id="sg-app",
            ),
        ),
    )
    bastion = ComputeInstance(
        id="c-bastion",
        logical_name="bastion",
        subnet_id="sn-public",
        security_group_ids=("sg-bastion",),
        assign_public_ip=True,
        is_bastion=True,
        roles=("bastion",),
        source_node_ids=("PC1",),
    )
    router = ComputeInstance(
        id="c-r1",
        logical_name="r1-appliance",
        subnet_id="sn-public",
        interfaces=(
            NetworkInterfaceSpec(id="nic-r1-0", subnet_id="sn-public", primary=True),
            NetworkInterfaceSpec(
                id="nic-r1-1", subnet_id="sn-private", source_dest_check=False
            ),
        ),
        security_group_ids=("sg-app",),
        instance_type="t3.small",
        is_forwarder=True,
        roles=("router",),
        source_node_ids=("R1",),
    )
    app = ComputeInstance(
        id="c-app1",
        logical_name="app1",
        subnet_id="sn-private",
        security_group_ids=("sg-app",),
        roles=("web",),
        root_volume_gb=30,
        source_node_ids=("S1",),
    )
    database = Database(
        id="db-main",
        logical_name="maindb",
        engine=DatabaseEngine.POSTGRES,
        engine_version="16",
        subnet_ids=("sn-private", "sn-db"),
        security_group_ids=("sg-db",),
        source_node_ids=("DB1",),
    )
    balancer = LoadBalancer(
        id="lb-app",
        logical_name="app-lb",
        network_id="vpc-r1",
        scheme=LoadBalancerScheme.INTERNET_FACING,
        subnet_ids=("sn-public",),
        security_group_ids=("sg-app",),
        listeners=(Listener(protocol="HTTP", port=80, target_port=80),),
        target_compute_ids=("c-app1",),
        source_node_ids=("LB1",),
    )

    return CloudArchitecture(
        metadata=CloudArchitectureMetadata(
            cloud_architecture_id="cloud_fixture_full",
            name="full",
            region="eu-west-1",
            network_architecture_id="arch_fixture_full",
            network_architecture_revision=4,
            deployment_pattern="single_vpc_public_private",
            confidence="high",
        ),
        infrastructure=Infrastructure(
            networks=(vpc,),
            subnets=(public, private, db_subnet),
            gateways=(igw, nat),
            route_tables=(public_rt, private_rt),
            security_groups=(bastion_sg, app_sg, db_sg),
            compute=(bastion, router, app),
            databases=(database,),
            load_balancers=(balancer,),
        ),
        configuration=Configuration(
            hosts=(
                ConfigurationHost(
                    id="h-bastion",
                    name="bastion",
                    compute_id="c-bastion",
                    groups=("bastion",),
                    roles=("base",),
                    remote_user="ec2-user",
                ),
                ConfigurationHost(
                    id="h-app1",
                    name="app1",
                    compute_id="c-app1",
                    groups=("web",),
                    roles=("nginx",),
                    requires_bastion=True,
                    bastion_host_id="h-bastion",
                    remote_user="ec2-user",
                    variables={"app_port": 8080},
                ),
                ConfigurationHost(
                    id="h-r1",
                    name="r1",
                    compute_id="c-r1",
                    groups=("routers",),
                    roles=("frr",),
                    requires_bastion=True,
                ),
            ),
            roles=(
                SystemRole(
                    id="role-base",
                    name="base",
                    packages=(PackageRequirement(name="curl"), PackageRequirement(name="jq")),
                ),
                SystemRole(
                    id="role-nginx",
                    name="nginx",
                    requires=("base",),
                    packages=(PackageRequirement(name="nginx"),),
                    services=(ServiceRequirement(name="nginx"),),
                    files=(
                        ConfigFile(
                            path="/etc/nginx/conf.d/app.conf",
                            content="server { listen 80; }",
                            owner="root",
                            mode="0644",
                            notifies=("nginx",),
                        ),
                    ),
                    variables={"worker_processes": "auto"},
                ),
                SystemRole(
                    id="role-frr",
                    name="frr",
                    requires=("base",),
                    packages=(PackageRequirement(name="frr"),),
                    services=(ServiceRequirement(name="frr"),),
                    kind="router_appliance",
                ),
            ),
            group_variables={"web": {"http_port": 80}},
            global_variables={"ansible_python_interpreter": "/usr/bin/python3"},
        ),
        relationships=(
            Relationship(
                id="rel-vpc-public",
                type=RelationshipType.CONTAINS,
                source_id="vpc-r1",
                target_id="sn-public",
            ),
        ),
    )


def two_vpc_peered_cloud() -> CloudArchitecture:
    """Two routed domains joined by a peering connection."""
    networks = tuple(
        VirtualNetwork(
            id=f"vpc-r{index}",
            logical_name=f"r{index}-vpc",
            cidr=SubnetSpec(cidr=f"10.{index}.0.0/16"),
            source_node_ids=(f"R{index}",),
        )
        for index in (1, 2)
    )
    subnets = tuple(
        Subnet(
            id=f"sn-r{index}",
            logical_name=f"r{index}-lan",
            network_id=f"vpc-r{index}",
            cidr=SubnetSpec(cidr=f"10.{index}.1.0/24"),
        )
        for index in (1, 2)
    )
    peering = Gateway(
        id="gw-peer",
        logical_name="r1-r2-peering",
        type=GatewayType.PEERING,
        peer_network_ids=("vpc-r1", "vpc-r2"),
        source_node_ids=("R1", "R2"),
    )
    tables = tuple(
        RouteTable(
            id=f"rt-r{index}",
            logical_name=f"r{index}-rt",
            network_id=f"vpc-r{index}",
            routes=(
                Route(
                    destination=SubnetSpec(cidr=f"10.{3 - index}.0.0/16"),
                    target_ref="gw-peer",
                ),
            ),
            associated_subnet_ids=(f"sn-r{index}",),
        )
        for index in (1, 2)
    )
    return CloudArchitecture(
        metadata=CloudArchitectureMetadata(
            cloud_architecture_id="cloud_fixture_peered",
            name="two-vpc-peered",
            deployment_pattern="multi_vpc_peering",
        ),
        infrastructure=Infrastructure(
            networks=networks, subnets=subnets, gateways=(peering,), route_tables=tables
        ),
    )
