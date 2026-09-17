"""Deterministic Terraform generation from fixed cloud architectures."""

from __future__ import annotations

import re

import pytest

from src.domain.architecture.issues import IssueSeverity, ValidationIssue, ValidationReport
from src.domain.cloud import (
    CloudArchitecture,
    CloudArchitectureMetadata,
    CloudProvider,
    ComputeInstance,
    Gateway,
    GatewayType,
    Infrastructure,
    Route,
    RouteTable,
    SecurityGroup,
    SecurityRule,
    SecurityRuleDirection,
    Subnet,
    VirtualNetwork,
)
from src.domain.network import SubnetSpec
from src.infrastructure.generators.terraform.generator import AWSTerraformGenerator
from src.shared.errors import UnsupportedInputError, ValidationError
from tests.fixtures.cloud_architectures import full_cloud, minimal_cloud, two_vpc_peered_cloud


def normalise(content: str) -> str:
    """Collapse HCL's alignment padding.

    `terraform fmt` aligns `=` per contiguous attribute group, so the exact column
    depends on the longest neighbouring key. Tests assert on the attribute, not on how
    wide its block happened to be.
    """
    return re.sub(r"[ \t]+", " ", content)


@pytest.fixture
def generator() -> AWSTerraformGenerator:
    return AWSTerraformGenerator()


class TestDeterminism:
    def test_two_runs_produce_identical_bytes(self, generator: AWSTerraformGenerator) -> None:
        first = generator.generate(full_cloud())
        second = generator.generate(full_cloud())
        assert first.fingerprint() == second.fingerprint()
        for path in first.file_paths:
            assert first.file(path).content == second.file(path).content

    def test_output_contains_no_timestamp(self, generator: AWSTerraformGenerator) -> None:
        # A timestamp in the output would break the determinism contract.
        content = "\n".join(f.content for f in generator.generate(full_cloud()).files)
        assert "generated_at" not in content
        assert "20" + "26-" not in content

    def test_a_fresh_generator_instance_gives_the_same_result(self) -> None:
        assert (
            AWSTerraformGenerator().generate(minimal_cloud()).fingerprint()
            == AWSTerraformGenerator().generate(minimal_cloud()).fingerprint()
        )


class TestProjectShape:
    def test_expected_files(self, generator: AWSTerraformGenerator) -> None:
        project = generator.generate(minimal_cloud())
        assert set(project.file_paths) == {
            "README.md",
            "data.tf",
            "main.tf",
            "outputs.tf",
            "providers.tf",
            "terraform.tfvars.example",
            "variables.tf",
        }

    def test_provider_is_pinned(self, generator: AWSTerraformGenerator) -> None:
        providers = generator.generate(minimal_cloud()).file("providers.tf").content
        assert 'source  = "hashicorp/aws"' in providers or '"hashicorp/aws"' in providers
        assert "required_version" in providers
        assert "var.aws_region" in providers

    def test_region_comes_from_the_architecture(self, generator: AWSTerraformGenerator) -> None:
        variables = generator.generate(minimal_cloud()).file("variables.tf").content
        assert '"eu-west-1"' in variables


class TestVPCGeneration:
    def test_vpc_resource(self, generator: AWSTerraformGenerator) -> None:
        main = generator.generate(minimal_cloud()).file("main.tf").content
        assert 'resource "aws_vpc" "vpc_r1"' in main
        assert 'cidr_block = "10.0.0.0/16"' in normalise(main)

    def test_vpc_carries_traceability_back_to_the_diagram(
        self, generator: AWSTerraformGenerator
    ) -> None:
        main = generator.generate(minimal_cloud()).file("main.tf").content
        assert "# From network component(s): R1" in main
        assert '"topoforge:source-nodes" = "R1"' in main

    def test_vpc_id_is_output(self, generator: AWSTerraformGenerator) -> None:
        project = generator.generate(minimal_cloud())
        assert "vpc_r1_id" in project.declared_outputs
        assert "aws_vpc.vpc_r1.id" in project.file("outputs.tf").content


class TestSubnetGeneration:
    def test_subnet_references_its_vpc(self, generator: AWSTerraformGenerator) -> None:
        main = generator.generate(minimal_cloud()).file("main.tf").content
        assert 'resource "aws_subnet" "sn_lan"' in main
        assert "vpc_id = aws_vpc.vpc_r1.id" in normalise(main)

    def test_public_subnet_maps_public_ips(self, generator: AWSTerraformGenerator) -> None:
        assert (
            "map_public_ip_on_launch = true"
            in normalise(generator.generate(minimal_cloud()).file("main.tf").content)
        )

    def test_explicit_availability_zone_is_honoured(
        self, generator: AWSTerraformGenerator
    ) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert 'availability_zone = "eu-west-1a"' in normalise(main)

    def test_missing_az_uses_the_region_data_source_not_a_hard_coded_name(
        self, generator: AWSTerraformGenerator
    ) -> None:
        main = generator.generate(minimal_cloud()).file("main.tf").content
        assert "data.aws_availability_zones.available.names[0]" in main


class TestGatewayAndRouting:
    def test_internet_gateway(self, generator: AWSTerraformGenerator) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert 'resource "aws_internet_gateway" "gw_igw"' in main

    def test_nat_gateway_gets_an_elastic_ip(self, generator: AWSTerraformGenerator) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert 'resource "aws_eip" "gw_nat_eip"' in main
        assert "allocation_id = aws_eip.gw_nat_eip.id" in normalise(main)

    def test_route_uses_the_right_argument_for_its_target(
        self, generator: AWSTerraformGenerator
    ) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert "gateway_id = aws_internet_gateway.gw_igw.id" in normalise(main)
        assert "nat_gateway_id = aws_nat_gateway.gw_nat.id" in normalise(main)

    def test_route_table_associations(self, generator: AWSTerraformGenerator) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert 'resource "aws_route_table_association" "rt_private_sn_private"' in main
        assert 'resource "aws_route_table_association" "rt_private_sn_db"' in main

    def test_peering_between_two_vpcs(self, generator: AWSTerraformGenerator) -> None:
        main = generator.generate(two_vpc_peered_cloud()).file("main.tf").content
        assert 'resource "aws_vpc_peering_connection" "gw_peer"' in main
        assert "vpc_id = aws_vpc.vpc_r1.id" in normalise(main)
        assert "peer_vpc_id = aws_vpc.vpc_r2.id" in normalise(main)
        assert (
            "vpc_peering_connection_id = aws_vpc_peering_connection.gw_peer.id"
            in normalise(main)
        )


class TestSecurityGroups:
    def test_cidr_rule(self, generator: AWSTerraformGenerator) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert 'resource "aws_security_group" "sg_bastion"' in main
        assert "from_port = 22" in normalise(main)

    def test_peer_group_rule_becomes_a_reference_not_a_cidr(
        self, generator: AWSTerraformGenerator
    ) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert "security_groups = [" in main
        assert "aws_security_group.sg_bastion.id" in main

    def test_a_rule_with_no_source_is_skipped_rather_than_opened_to_the_world(self) -> None:
        architecture = CloudArchitecture(
            infrastructure=Infrastructure(
                networks=(
                    VirtualNetwork(
                        id="v", logical_name="v", cidr=SubnetSpec(cidr="10.0.0.0/16")
                    ),
                ),
                subnets=(
                    Subnet(
                        id="s",
                        logical_name="s",
                        network_id="v",
                        cidr=SubnetSpec(cidr="10.0.1.0/24"),
                    ),
                ),
                security_groups=(
                    SecurityGroup(
                        id="sg",
                        logical_name="sg",
                        network_id="v",
                        rules=(
                            SecurityRule(
                                direction=SecurityRuleDirection.INGRESS,
                                protocol="tcp",
                                from_port=22,
                                to_port=22,
                            ),
                        ),
                    ),
                ),
                compute=(ComputeInstance(id="c", logical_name="c", subnet_id="s"),),
            )
        )
        project = AWSTerraformGenerator().generate(architecture)
        assert "0.0.0.0/0" not in project.file("main.tf").content
        assert any(warning.code == "rule_without_source" for warning in project.warnings)


class TestCompute:
    def test_instance_type_comes_from_the_cloud_architecture(
        self, generator: AWSTerraformGenerator
    ) -> None:
        # Sizing is an architecture decision made upstream, not a generator heuristic.
        main = generator.generate(full_cloud()).file("main.tf").content
        assert 'instance_type = "t3.small"' in normalise(main)

    def test_router_appliance_disables_source_dest_check(
        self, generator: AWSTerraformGenerator
    ) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert "source_dest_check = false" in normalise(main)

    def test_secondary_interfaces_are_attached(self, generator: AWSTerraformGenerator) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert 'resource "aws_network_interface" "c_r1_nic_r1_1"' in main
        assert 'resource "aws_network_interface_attachment" "c_r1_nic_r1_1"' in main

    def test_roles_are_tagged_so_ansible_can_group_by_them(
        self, generator: AWSTerraformGenerator
    ) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert '"topoforge:roles" = "web"' in normalise(main)

    def test_ssh_key_becomes_a_required_variable(self, generator: AWSTerraformGenerator) -> None:
        project = generator.generate(full_cloud())
        assert "ssh_key_name" in project.required_variables
        assert "var.ssh_key_name" in project.file("main.tf").content

    def test_private_ip_is_output_for_every_instance(
        self, generator: AWSTerraformGenerator
    ) -> None:
        project = generator.generate(full_cloud())
        assert "c_app1_private_ip" in project.declared_outputs
        assert "c_bastion_public_ip" in project.declared_outputs


class TestDatabase:
    def test_rds_with_subnet_group(self, generator: AWSTerraformGenerator) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert 'resource "aws_db_subnet_group" "db_main"' in main
        assert 'resource "aws_db_instance" "db_main"' in main
        assert 'engine = "postgres"' in normalise(main)

    def test_credentials_are_variables_never_literals(
        self, generator: AWSTerraformGenerator
    ) -> None:
        project = generator.generate(full_cloud())
        assert "db_main_password" in project.required_variables
        assert "var.db_main_password" in project.file("main.tf").content
        # The password variable must be declared sensitive so it stays out of plan output.
        variables = project.file("variables.tf").content
        assert "sensitive" in variables

    def test_single_subnet_database_is_refused_with_a_warning(self) -> None:
        architecture = full_cloud()
        database = architecture.infrastructure.databases[0].model_copy(
            update={"subnet_ids": ("sn-private",)}
        )
        reduced = architecture.model_copy(
            update={
                "infrastructure": architecture.infrastructure.model_copy(
                    update={"databases": (database,)}
                )
            }
        )
        project = AWSTerraformGenerator().generate(reduced)
        assert 'resource "aws_db_instance"' not in project.file("main.tf").content
        assert any(
            warning.code == "db_subnet_group_needs_two_subnets" for warning in project.warnings
        )


class TestLoadBalancer:
    def test_alb_with_listener_and_target_group(self, generator: AWSTerraformGenerator) -> None:
        main = generator.generate(full_cloud()).file("main.tf").content
        assert 'resource "aws_lb" "lb_app"' in main
        assert 'resource "aws_lb_target_group" "lb_app_tg_0"' in main
        assert 'resource "aws_lb_listener" "lb_app_listener_0"' in main
        assert 'resource "aws_lb_target_group_attachment" "lb_app_tg_0_c_app1"' in main

    def test_internet_facing_scheme(self, generator: AWSTerraformGenerator) -> None:
        assert (
            "internal = false"
            in normalise(generator.generate(full_cloud()).file("main.tf").content)
        )


class TestRejections:
    def test_invalid_architecture_is_refused(self, generator: AWSTerraformGenerator) -> None:
        invalid = minimal_cloud().with_validation(
            ValidationReport.from_issues(
                [ValidationIssue(code="bad", severity=IssueSeverity.ERROR, message="bad")]
            )
        )
        with pytest.raises(ValidationError, match="failed validation"):
            generator.generate(invalid)

    def test_empty_infrastructure_is_refused(self, generator: AWSTerraformGenerator) -> None:
        with pytest.raises(ValidationError, match="no infrastructure"):
            generator.generate(CloudArchitecture())

    def test_non_aws_provider_is_refused_rather_than_mis_generated(
        self, generator: AWSTerraformGenerator
    ) -> None:
        azure = minimal_cloud().model_copy(
            update={
                "metadata": CloudArchitectureMetadata(
                    provider=CloudProvider.AZURE, region="westeurope"
                )
            }
        )
        with pytest.raises(UnsupportedInputError, match="generates AWS only"):
            generator.generate(azure)


class TestNoAIInTheLoop:
    def test_generation_needs_no_credentials_and_no_network(
        self, generator: AWSTerraformGenerator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing in generation may reach out. If it did, this would fail.
        import socket

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("the generator attempted network access")

        monkeypatch.setattr(socket, "socket", refuse)
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "AWS_ACCESS_KEY_ID"):
            monkeypatch.delenv(key, raising=False)
        assert generator.generate(full_cloud()).files
