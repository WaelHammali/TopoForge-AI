"""cloud_architecture.json — the RAG's output and the generators' input."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.domain.architecture.issues import IssueSeverity, ValidationIssue, ValidationReport
from src.domain.cloud import (
    CLOUD_SCHEMA_VERSION,
    CloudArchitecture,
    CloudArchitectureMetadata,
    CloudProvider,
    ComputeInstance,
    Configuration,
    ConfigurationHost,
    Gateway,
    GatewayType,
    Infrastructure,
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
    SystemRole,
    VirtualNetwork,
)
from src.domain.network import SubnetSpec
from src.shared.errors import SchemaVersionError


@pytest.fixture
def cloud() -> CloudArchitecture:
    vpc = VirtualNetwork(
        id="vpc-r1",
        logical_name="r1",
        cidr=SubnetSpec(cidr="10.0.0.0/16"),
        source_node_ids=("R1",),
    )
    public = Subnet(
        id="sn-public",
        logical_name="r1-public",
        network_id="vpc-r1",
        cidr=SubnetSpec(cidr="10.0.1.0/24"),
        public=True,
    )
    private = Subnet(
        id="sn-private",
        logical_name="r1-private",
        network_id="vpc-r1",
        cidr=SubnetSpec(cidr="10.0.2.0/24"),
    )
    igw = Gateway(id="gw-igw", logical_name="r1-igw", type=GatewayType.INTERNET, network_id="vpc-r1")
    table = RouteTable(
        id="rt-public",
        logical_name="r1-public-rt",
        network_id="vpc-r1",
        routes=(Route(destination=SubnetSpec(cidr="0.0.0.0/0"), target_ref="gw-igw"),),
        associated_subnet_ids=("sn-public",),
    )
    sg = SecurityGroup(
        id="sg-web",
        logical_name="web",
        network_id="vpc-r1",
        rules=(
            SecurityRule(
                direction=SecurityRuleDirection.INGRESS,
                protocol="tcp",
                from_port=443,
                to_port=443,
                cidr_blocks=("0.0.0.0/0",),
            ),
        ),
    )
    web = ComputeInstance(
        id="c-web1",
        logical_name="web1",
        subnet_id="sn-public",
        security_group_ids=("sg-web",),
        roles=("web",),
        source_node_ids=("S1",),
    )
    host = ConfigurationHost(
        id="h-web1", name="web1", compute_id="c-web1", groups=("web",), roles=("nginx",)
    )
    role = SystemRole(
        id="role-nginx",
        name="nginx",
        packages=(PackageRequirement(name="nginx"),),
        services=(ServiceRequirement(name="nginx"),),
    )
    return CloudArchitecture(
        metadata=CloudArchitectureMetadata(
            name="demo",
            network_architecture_id="arch-1",
            network_architecture_revision=3,
            deployment_pattern="single_vpc_public_private",
        ),
        infrastructure=Infrastructure(
            networks=(vpc,),
            subnets=(public, private),
            route_tables=(table,),
            gateways=(igw,),
            security_groups=(sg,),
            compute=(web,),
        ),
        configuration=Configuration(hosts=(host,), roles=(role,)),
        relationships=(
            Relationship(
                id="rel-1", type=RelationshipType.CONTAINS, source_id="vpc-r1", target_id="sn-public"
            ),
        ),
    )


class TestSchema:
    def test_round_trip(self, cloud: CloudArchitecture) -> None:
        restored = CloudArchitecture.from_json(cloud.to_json())
        assert restored.infrastructure.counts() == cloud.infrastructure.counts()
        assert restored.configuration.counts() == cloud.configuration.counts()

    def test_version_recorded_and_enforced(self, cloud: CloudArchitecture) -> None:
        assert cloud.to_json_dict()["schema_version"] == CLOUD_SCHEMA_VERSION
        with pytest.raises(SchemaVersionError):
            CloudArchitecture.from_json_dict({"schema_version": "0.1"})

    def test_provider_defaults_to_aws_but_is_not_hard_coded(self) -> None:
        assert CloudArchitecture().provider is CloudProvider.AWS
        other = CloudArchitecture(
            metadata=CloudArchitectureMetadata(provider=CloudProvider.AZURE, region="westeurope")
        )
        assert other.provider is CloudProvider.AZURE


class TestInfrastructureConfigurationSplit:
    """Terraform owns infrastructure, Ansible owns configuration. No overlap."""

    def test_configuration_hosts_carry_no_addresses(self, cloud: CloudArchitecture) -> None:
        # Real addresses only exist after apply; the inventory generator supplies them.
        host_fields = set(ConfigurationHost.model_fields)
        assert not {"private_ip", "public_ip", "ansible_host", "address"} & host_fields

    def test_infrastructure_carries_no_package_or_service_concepts(self) -> None:
        infra_fields = set(ComputeInstance.model_fields)
        assert not {"packages", "services", "playbook"} & infra_fields

    def test_roles_link_the_two_halves(self, cloud: CloudArchitecture) -> None:
        compute = cloud.infrastructure.compute[0]
        host = cloud.configuration.hosts[0]
        assert host.compute_id == compute.id
        assert set(host.groups) <= set(compute.roles) | set(host.groups)


class TestReferentialIntegrity:
    def test_subnet_needs_a_real_network(self) -> None:
        with pytest.raises(PydanticValidationError, match="unknown network"):
            CloudArchitecture(
                infrastructure=Infrastructure(
                    subnets=(
                        Subnet(
                            id="s",
                            logical_name="s",
                            network_id="ghost",
                            cidr=SubnetSpec(cidr="10.0.0.0/24"),
                        ),
                    )
                )
            )

    def test_compute_needs_a_real_subnet(self) -> None:
        with pytest.raises(PydanticValidationError, match="unknown subnet"):
            CloudArchitecture(
                infrastructure=Infrastructure(
                    compute=(ComputeInstance(id="c", logical_name="c", subnet_id="ghost"),)
                )
            )

    def test_nic_needs_a_real_subnet(self) -> None:
        with pytest.raises(PydanticValidationError, match="unknown subnet"):
            CloudArchitecture(
                infrastructure=Infrastructure(
                    compute=(
                        ComputeInstance(
                            id="c",
                            logical_name="c",
                            interfaces=(NetworkInterfaceSpec(id="nic", subnet_id="ghost"),),
                        ),
                    )
                )
            )

    def test_configuration_host_needs_real_compute(self) -> None:
        with pytest.raises(PydanticValidationError, match="unknown compute"):
            CloudArchitecture(
                configuration=Configuration(
                    hosts=(ConfigurationHost(id="h", name="h", compute_id="ghost"),)
                )
            )

    def test_relationship_endpoints_must_exist(self, cloud: CloudArchitecture) -> None:
        with pytest.raises(PydanticValidationError, match="unknown resource"):
            cloud.model_copy(
                update={
                    "relationships": (
                        Relationship(
                            id="r",
                            type=RelationshipType.ATTACHED_TO,
                            source_id="vpc-r1",
                            target_id="ghost",
                        ),
                    )
                }
            ).model_validate(
                {
                    **cloud.model_dump(),
                    "relationships": [
                        {
                            "id": "r",
                            "type": "attached_to",
                            "source_id": "vpc-r1",
                            "target_id": "ghost",
                        }
                    ],
                }
            )


class TestGenerationGate:
    def test_valid_and_non_empty_may_generate(self, cloud: CloudArchitecture) -> None:
        assert cloud.can_generate is True

    def test_validation_errors_block_generation(self, cloud: CloudArchitecture) -> None:
        report = ValidationReport.from_issues(
            [
                ValidationIssue(
                    code="subnet_outside_vpc", severity=IssueSeverity.ERROR, message="bad"
                )
            ]
        )
        assert cloud.with_validation(report).can_generate is False

    def test_empty_infrastructure_blocks_generation(self) -> None:
        assert CloudArchitecture().can_generate is False

    def test_ansible_stage_is_skipped_when_there_is_no_configuration(
        self, cloud: CloudArchitecture
    ) -> None:
        assert cloud.requires_configuration_stage is True
        infra_only = cloud.model_copy(update={"configuration": Configuration()})
        assert infra_only.requires_configuration_stage is False


class TestTraceability:
    def test_resources_point_back_at_network_nodes(self, cloud: CloudArchitecture) -> None:
        assert cloud.infrastructure.networks[0].source_node_ids == ("R1",)
        assert cloud.infrastructure.compute[0].source_node_ids == ("S1",)

    def test_metadata_pins_the_exact_source_revision(self, cloud: CloudArchitecture) -> None:
        assert cloud.metadata.network_architecture_id == "arch-1"
        assert cloud.metadata.network_architecture_revision == 3

    def test_summary_is_serialisable(self, cloud: CloudArchitecture) -> None:
        summary = cloud.summary()
        assert summary["networks"] == 1
        assert summary["subnets"] == 2
        assert summary["config_hosts"] == 1


class TestNoIaCConceptsLeak:
    """The RAG's output must not contain implementation syntax concepts."""

    def test_no_terraform_or_ansible_fields_anywhere(self, cloud: CloudArchitecture) -> None:
        banned = ("terraform", "hcl", "tfvars", "ansible", "playbook", "jinja", "depends_on")
        payload = cloud.to_json().lower()
        offenders = [word for word in banned if word in payload]
        assert offenders == [], f"IaC concepts leaked into cloud_architecture.json: {offenders}"


class TestConfigurationHelpers:
    def test_unresolved_roles_are_reported(self) -> None:
        config = Configuration(
            hosts=(ConfigurationHost(id="h", name="h", compute_id="c", roles=("ghost",)),)
        )
        assert config.unresolved_role_names() == ("ghost",)

    def test_role_dependencies_are_checked_too(self) -> None:
        config = Configuration(
            roles=(SystemRole(id="r", name="app", requires=("base",)),)
        )
        assert config.unresolved_role_names() == ("base",)

    def test_groups_preserve_declaration_order(self) -> None:
        config = Configuration(
            hosts=(
                ConfigurationHost(id="h1", name="h1", compute_id="c", groups=("web", "all_app")),
                ConfigurationHost(id="h2", name="h2", compute_id="c", groups=("db", "web")),
            )
        )
        assert config.groups() == ("web", "all_app", "db")

    def test_unsafe_role_name_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="unsafe role name"):
            SystemRole(id="r", name="../escape")
