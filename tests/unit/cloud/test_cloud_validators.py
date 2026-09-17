"""Cloud architecture validation — the gate between the RAG and the generators."""

from __future__ import annotations

import pytest

from src.domain.cloud import (
    CloudArchitecture,
    ComputeInstance,
    Configuration,
    ConfigurationHost,
    Database,
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
    SystemRole,
    VirtualNetwork,
)
from src.domain.cloud.validators import CloudArchitectureValidatorSuite
from src.domain.network import SubnetSpec
from tests.fixtures.cloud_architectures import full_cloud, minimal_cloud


def validate(architecture: CloudArchitecture):
    return CloudArchitectureValidatorSuite().validate(architecture)


def codes(report) -> set[str]:
    return {issue.code for issue in report.all_issues}


def build(**infrastructure_kwargs) -> CloudArchitecture:
    base = {
        "networks": (
            VirtualNetwork(id="v", logical_name="v", cidr=SubnetSpec(cidr="10.0.0.0/16")),
        )
    }
    base.update(infrastructure_kwargs)
    return CloudArchitecture(infrastructure=Infrastructure(**base))


class TestFixturesAreSound:
    def test_the_reference_fixtures_validate(self) -> None:
        for architecture in (minimal_cloud(), full_cloud()):
            report = validate(architecture)
            assert report.valid, [issue.message for issue in report.errors]


class TestSubnets:
    def test_subnet_outside_its_vpc_is_an_error(self) -> None:
        report = validate(
            build(
                subnets=(
                    Subnet(
                        id="s",
                        logical_name="s",
                        network_id="v",
                        cidr=SubnetSpec(cidr="192.168.0.0/24"),
                    ),
                )
            )
        )
        assert "subnet_outside_network" in codes(report)
        assert report.blocks_rag is False or report.errors

    def test_overlapping_subnets_are_an_error(self) -> None:
        report = validate(
            build(
                subnets=(
                    Subnet(id="a", logical_name="a", network_id="v", cidr=SubnetSpec(cidr="10.0.0.0/24")),
                    Subnet(id="b", logical_name="b", network_id="v", cidr=SubnetSpec(cidr="10.0.0.0/25")),
                )
            )
        )
        assert "overlapping_subnets" in codes(report)

    def test_adjacent_subnets_do_not_overlap(self) -> None:
        report = validate(
            build(
                subnets=(
                    Subnet(id="a", logical_name="a", network_id="v", cidr=SubnetSpec(cidr="10.0.1.0/24")),
                    Subnet(id="b", logical_name="b", network_id="v", cidr=SubnetSpec(cidr="10.0.2.0/24")),
                )
            )
        )
        assert "overlapping_subnets" not in codes(report)

    def test_a_tiny_subnet_is_warned_about(self) -> None:
        report = validate(
            build(
                subnets=(
                    Subnet(id="a", logical_name="a", network_id="v", cidr=SubnetSpec(cidr="10.0.0.0/32")),
                )
            )
        )
        assert "subnet_too_small" in codes(report)


class TestComputePlacement:
    def _with_compute(self, **compute_kwargs) -> CloudArchitecture:
        return build(
            subnets=(
                Subnet(id="s", logical_name="s", network_id="v", cidr=SubnetSpec(cidr="10.0.1.0/24")),
            ),
            compute=(ComputeInstance(id="c", logical_name="c", **compute_kwargs),),
        )

    def test_unplaced_compute_is_an_error(self) -> None:
        report = validate(build(compute=(ComputeInstance(id="c", logical_name="c"),)))
        assert "compute_not_placed" in codes(report)

    def test_address_outside_its_subnet_is_an_error(self) -> None:
        report = validate(
            self._with_compute(
                subnet_id="s",
                interfaces=(
                    NetworkInterfaceSpec(id="n", subnet_id="s", private_ip="10.0.9.5"),
                ),
            )
        )
        assert "address_outside_subnet" in codes(report)

    def test_aws_reserved_addresses_are_rejected(self) -> None:
        # AWS reserves .0 through .3 and the broadcast address of every subnet.
        for address in ("10.0.1.0", "10.0.1.1", "10.0.1.2", "10.0.1.3", "10.0.1.255"):
            report = validate(
                self._with_compute(
                    subnet_id="s",
                    interfaces=(
                        NetworkInterfaceSpec(id="n", subnet_id="s", private_ip=address),
                    ),
                )
            )
            assert "aws_reserved_address" in codes(report), address

    def test_an_ordinary_address_is_accepted(self) -> None:
        report = validate(
            self._with_compute(
                subnet_id="s",
                interfaces=(
                    NetworkInterfaceSpec(id="n", subnet_id="s", private_ip="10.0.1.10"),
                ),
            )
        )
        assert "aws_reserved_address" not in codes(report)

    def test_duplicate_addresses_are_an_error(self) -> None:
        architecture = build(
            subnets=(
                Subnet(id="s", logical_name="s", network_id="v", cidr=SubnetSpec(cidr="10.0.1.0/24")),
            ),
            compute=(
                ComputeInstance(
                    id="c1",
                    logical_name="c1",
                    subnet_id="s",
                    interfaces=(NetworkInterfaceSpec(id="n1", subnet_id="s", private_ip="10.0.1.10"),),
                ),
                ComputeInstance(
                    id="c2",
                    logical_name="c2",
                    subnet_id="s",
                    interfaces=(NetworkInterfaceSpec(id="n2", subnet_id="s", private_ip="10.0.1.10"),),
                ),
            ),
        )
        assert "duplicate_address" in codes(validate(architecture))

    def test_a_forwarder_with_source_dest_check_is_an_error(self) -> None:
        report = validate(
            self._with_compute(
                subnet_id="s",
                is_forwarder=True,
                interfaces=(
                    NetworkInterfaceSpec(id="n", subnet_id="s", source_dest_check=True),
                ),
            )
        )
        assert "forwarder_with_source_dest_check" in codes(report)


class TestRouting:
    def test_a_route_to_a_missing_target_is_an_error(self) -> None:
        report = validate(
            build(
                subnets=(
                    Subnet(id="s", logical_name="s", network_id="v", cidr=SubnetSpec(cidr="10.0.1.0/24")),
                ),
                route_tables=(
                    RouteTable(
                        id="rt",
                        logical_name="rt",
                        network_id="v",
                        routes=(Route(destination=SubnetSpec(cidr="0.0.0.0/0"), target_ref="ghost"),),
                        associated_subnet_ids=("s",),
                    ),
                ),
            )
        )
        assert "route_target_missing" in codes(report)

    def test_duplicate_destinations_are_an_error(self) -> None:
        gateway = Gateway(id="gw", logical_name="gw", type=GatewayType.INTERNET, network_id="v")
        report = validate(
            build(
                subnets=(
                    Subnet(id="s", logical_name="s", network_id="v", cidr=SubnetSpec(cidr="10.0.1.0/24")),
                ),
                gateways=(gateway,),
                route_tables=(
                    RouteTable(
                        id="rt",
                        logical_name="rt",
                        network_id="v",
                        routes=(
                            Route(destination=SubnetSpec(cidr="0.0.0.0/0"), target_ref="gw"),
                            Route(destination=SubnetSpec(cidr="0.0.0.0/0"), target_ref="gw"),
                        ),
                        associated_subnet_ids=("s",),
                    ),
                ),
            )
        )
        assert "duplicate_route_destination" in codes(report)

    def test_a_nat_gateway_needs_a_subnet(self) -> None:
        report = validate(
            build(gateways=(Gateway(id="gw", logical_name="nat", type=GatewayType.NAT, network_id="v"),))
        )
        assert "nat_gateway_without_subnet" in codes(report)

    def test_peering_must_name_exactly_two_networks(self) -> None:
        report = validate(
            build(
                gateways=(
                    Gateway(
                        id="gw",
                        logical_name="peer",
                        type=GatewayType.PEERING,
                        peer_network_ids=("v",),
                    ),
                )
            )
        )
        assert "peering_arity" in codes(report)


class TestPublicExposure:
    def _group(self, **rule_kwargs) -> CloudArchitecture:
        return build(
            security_groups=(
                SecurityGroup(
                    id="sg",
                    logical_name="sg",
                    network_id="v",
                    rules=(SecurityRule(direction=SecurityRuleDirection.INGRESS, **rule_kwargs),),
                ),
            )
        )

    def test_everything_open_to_the_world_is_an_error(self) -> None:
        report = validate(self._group(protocol="-1", cidr_blocks=("0.0.0.0/0",)))
        assert "world_open_all_ports" in codes(report)

    @pytest.mark.parametrize("port", [22, 3389, 5432, 27017])
    def test_sensitive_ports_open_to_the_world_are_warned_about(self, port: int) -> None:
        report = validate(
            self._group(protocol="tcp", from_port=port, to_port=port, cidr_blocks=("0.0.0.0/0",))
        )
        assert "sensitive_port_open_to_world" in codes(report)

    def test_a_sensitive_port_inside_a_range_is_still_caught(self) -> None:
        report = validate(
            self._group(protocol="tcp", from_port=20, to_port=25, cidr_blocks=("0.0.0.0/0",))
        )
        assert "sensitive_port_open_to_world" in codes(report)

    def test_https_to_the_world_is_not_flagged(self) -> None:
        report = validate(
            self._group(protocol="tcp", from_port=443, to_port=443, cidr_blocks=("0.0.0.0/0",))
        )
        assert "sensitive_port_open_to_world" not in codes(report)

    def test_ssh_from_a_private_range_is_not_flagged(self) -> None:
        report = validate(
            self._group(protocol="tcp", from_port=22, to_port=22, cidr_blocks=("10.0.0.0/16",))
        )
        assert "sensitive_port_open_to_world" not in codes(report)

    def test_a_publicly_accessible_database_is_warned_about(self) -> None:
        report = validate(
            build(
                subnets=(
                    Subnet(id="a", logical_name="a", network_id="v", cidr=SubnetSpec(cidr="10.0.1.0/24")),
                    Subnet(id="b", logical_name="b", network_id="v", cidr=SubnetSpec(cidr="10.0.2.0/24")),
                ),
                databases=(
                    Database(
                        id="db",
                        logical_name="db",
                        subnet_ids=("a", "b"),
                        publicly_accessible=True,
                    ),
                ),
            )
        )
        assert "publicly_accessible_database" in codes(report)


class TestConfigurationCoherence:
    def test_an_undefined_role_is_an_error(self) -> None:
        architecture = build(
            subnets=(
                Subnet(id="s", logical_name="s", network_id="v", cidr=SubnetSpec(cidr="10.0.1.0/24")),
            ),
            compute=(ComputeInstance(id="c", logical_name="c", subnet_id="s"),),
        ).model_copy(
            update={
                "configuration": Configuration(
                    hosts=(ConfigurationHost(id="h", name="h", compute_id="c", roles=("ghost",)),)
                )
            }
        )
        assert "undefined_role" in codes(validate(architecture))

    def test_a_host_needing_an_absent_bastion_is_warned_about(self) -> None:
        architecture = build(
            subnets=(
                Subnet(id="s", logical_name="s", network_id="v", cidr=SubnetSpec(cidr="10.0.1.0/24")),
            ),
            compute=(ComputeInstance(id="c", logical_name="c", subnet_id="s"),),
        ).model_copy(
            update={
                "configuration": Configuration(
                    hosts=(
                        ConfigurationHost(
                            id="h", name="h", compute_id="c", requires_bastion=True, roles=("base",)
                        ),
                    ),
                    roles=(SystemRole(id="r", name="base"),),
                )
            }
        )
        assert "bastion_required_but_absent" in codes(validate(architecture))


class TestReportShape:
    def test_errors_block_generation_and_warnings_do_not(self) -> None:
        architecture = minimal_cloud()
        report = validate(architecture)
        assert architecture.with_validation(report).can_generate is True

        broken = build(
            subnets=(
                Subnet(id="s", logical_name="s", network_id="v", cidr=SubnetSpec(cidr="192.168.0.0/24")),
            )
        )
        assert broken.with_validation(validate(broken)).can_generate is False

    def test_every_validator_is_recorded(self) -> None:
        report = validate(minimal_cloud())
        assert len(report.validators_run) == 5
        assert "SubnetValidator" in report.validators_run
