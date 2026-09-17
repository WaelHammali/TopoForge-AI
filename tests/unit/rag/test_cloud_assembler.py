"""Assembling a typed cloud architecture from the RAG's free-form plan."""

from __future__ import annotations

import pytest

from src.domain.cloud.resources import GatewayType, SubnetPurpose
from src.infrastructure.rag.cloud_assembler import CloudArchitectureAssembler, classify_representation
from src.shared.errors import RAGError
from tests.fixtures.network_architectures import single_router_lan, two_router_ospf


def plan(**overrides):
    """A minimal well-formed plan, shaped like the RAG's documented output."""
    base = {
        "cloud_plan": {
            "translation_mode": "behavioral_lab",
            "component_mapping": [],
            "networking": {"links": [], "addressing": [], "routing": [], "security": []},
            "dependencies": [],
        },
        "ansible_plan": {"targets": [], "tasks": [], "dependencies": []},
        "rule_ids": [],
        "limitations": [],
        "knowledge": [],
    }
    base.update(overrides)
    return base


def mapping(*entries):
    return plan(
        cloud_plan={
            "translation_mode": "behavioral_lab",
            "component_mapping": list(entries),
            "networking": {"links": [], "addressing": [], "routing": [], "security": []},
            "dependencies": [],
        }
    )


def assemble(plan_dict, architecture=None):
    return CloudArchitectureAssembler().assemble(
        plan_dict, architecture or two_router_ospf()
    )


class TestRepresentationClassification:
    @pytest.mark.parametrize(
        ("representation", "expected"),
        [
            ("AWS VPC", "virtual_network"),
            ("VPC subnet", "subnet"),
            ("EC2 instance", "compute"),
            ("EC2 instance (router appliance)", "compute"),
            ("Amazon RDS for PostgreSQL", "database"),
            ("security group", "security_group"),
            ("NAT gateway", "nat_gateway"),
            ("transit gateway attachment", "transit_gateway"),
        ],
    )
    def test_known_representations(self, representation: str, expected: str) -> None:
        assert classify_representation(representation) == expected

    def test_unknown_representation_is_not_guessed(self) -> None:
        assert classify_representation("a vibe") is None
        assert classify_representation(None) is None

    def test_longest_match_wins(self) -> None:
        # "nat gateway" contains "gateway"; the specific reading must win.
        assert classify_representation("NAT gateway in the public subnet") == "nat_gateway"


class TestNetworkDerivation:
    def test_one_vpc_per_routed_domain(self) -> None:
        cloud = assemble(plan())
        assert len(cloud.infrastructure.networks) == 2
        assert {network.cidr.cidr for network in cloud.infrastructure.networks} == {
            "10.10.10.0/24",
            "10.20.20.0/24",
        }

    def test_vpc_cidrs_come_from_the_architecture_not_the_model(self) -> None:
        cloud = assemble(plan(), single_router_lan())
        assert cloud.infrastructure.networks[0].cidr.cidr == "10.10.10.0/24"

    def test_subnets_carry_the_switch_they_came_from(self) -> None:
        cloud = assemble(plan())
        lan = next(
            subnet
            for subnet in cloud.infrastructure.subnets
            if subnet.cidr.cidr == "10.10.10.0/24"
        )
        assert lan.source_node_ids == ("SW1",)

    def test_a_switch_is_not_placed_on_the_router_to_router_link(self) -> None:
        """SW1 touches R1, but R1 also sits on the transit link. Only the LAN is SW1's."""
        cloud = assemble(plan())
        for subnet in cloud.infrastructure.subnets:
            if subnet.cidr.cidr == "10.255.0.0/30":
                pytest.fail("the transit link was turned into a subnet")
        assert all("SW1" not in subnet.source_node_ids or subnet.cidr.cidr == "10.10.10.0/24"
                   for subnet in cloud.infrastructure.subnets)

    def test_cross_domain_link_becomes_transport_not_a_subnet(self) -> None:
        # An EC2 instance cannot hold a NIC in another VPC, so the /30 cannot be a subnet.
        cloud = assemble(plan())
        assert "10.255.0.0/30" not in {s.cidr.cidr for s in cloud.infrastructure.subnets}
        assert any("10.255.0.0/30" in entry for entry in cloud.unmapped)

    def test_missing_transport_is_reported_not_invented(self) -> None:
        cloud = assemble(plan())
        assert any(
            warning.code == "no_transport_between_routed_domains" for warning in cloud.warnings
        )
        assert not cloud.infrastructure.gateways

    def test_peering_is_created_when_the_plan_asks_for_it(self) -> None:
        cloud = assemble(
            plan(
                cloud_plan={
                    "component_mapping": [],
                    "networking": {
                        "routing": [{"description": "connect the domains with VPC peering"}]
                    },
                    "dependencies": [],
                }
            )
        )
        peering = [g for g in cloud.infrastructure.gateways if g.type is GatewayType.PEERING]
        assert len(peering) == 1
        assert len(peering[0].peer_network_ids) == 2

    def test_route_tables_route_to_the_peer_cidr(self) -> None:
        cloud = assemble(
            plan(
                cloud_plan={
                    "component_mapping": [],
                    "networking": {"routing": [{"description": "vpc peering"}]},
                    "dependencies": [],
                }
            )
        )
        destinations = {
            route.destination.cidr
            for table in cloud.infrastructure.route_tables
            for route in table.routes
        }
        assert {"10.10.10.0/24", "10.20.20.0/24"} == destinations


class TestNoFabrication:
    def test_no_internet_gateway_unless_the_plan_names_one(self) -> None:
        cloud = assemble(plan(), single_router_lan())
        assert not [
            g for g in cloud.infrastructure.gateways if g.type is GatewayType.INTERNET
        ]

    def test_no_default_route_without_a_gateway(self) -> None:
        cloud = assemble(plan(), single_router_lan())
        destinations = {
            route.destination.cidr
            for table in cloud.infrastructure.route_tables
            for route in table.routes
        }
        assert "0.0.0.0/0" not in destinations

    def test_internet_gateway_appears_when_planned(self) -> None:
        cloud = assemble(
            plan(
                cloud_plan={
                    "component_mapping": [],
                    "networking": {"routing": [{"description": "attach an internet gateway"}]},
                    "dependencies": [],
                }
            ),
            single_router_lan(),
        )
        assert [g for g in cloud.infrastructure.gateways if g.type is GatewayType.INTERNET]
        assert "0.0.0.0/0" in {
            route.destination.cidr
            for table in cloud.infrastructure.route_tables
            for route in table.routes
        }

    def test_no_compute_is_marked_public_or_bastion_by_default(self) -> None:
        cloud = assemble(plan())
        assert not any(instance.assign_public_ip for instance in cloud.infrastructure.compute)
        assert not any(instance.is_bastion for instance in cloud.infrastructure.compute)


class TestCompute:
    def test_hosts_land_in_the_subnet_matching_their_address(self) -> None:
        cloud = assemble(plan())
        web = next(c for c in cloud.infrastructure.compute if c.logical_name == "web1")
        subnet = cloud.infrastructure.resource(web.subnet_id)
        assert subnet.cidr.cidr == "10.20.20.0/24"

    def test_interface_addresses_are_preserved_exactly(self) -> None:
        cloud = assemble(plan())
        web = next(c for c in cloud.infrastructure.compute if c.logical_name == "web1")
        assert web.interfaces[0].private_ip == "10.20.20.30"

    def test_routers_forward_and_disable_source_dest_check(self) -> None:
        cloud = assemble(plan())
        router = next(c for c in cloud.infrastructure.compute if c.logical_name == "r1")
        assert router.is_forwarder is True
        assert all(not nic.source_dest_check for nic in router.interfaces)

    def test_the_plans_instance_type_is_honoured(self) -> None:
        cloud = assemble(
            mapping(
                {
                    "component_id": "WEB1",
                    "cloud_representation": "EC2 instance",
                    "configuration": {"instance_type": "t3.large"},
                }
            )
        )
        web = next(c for c in cloud.infrastructure.compute if c.logical_name == "web1")
        assert web.instance_type == "t3.large"

    def test_traceability_back_to_the_source_component(self) -> None:
        cloud = assemble(plan())
        web = next(c for c in cloud.infrastructure.compute if c.logical_name == "web1")
        assert web.source_node_ids == ("WEB1",)


class TestConfigurationHalf:
    def test_services_become_roles_with_packages(self) -> None:
        cloud = assemble(plan())
        nginx = cloud.configuration.role("nginx")
        assert nginx is not None
        assert [package.name for package in nginx.packages] == ["nginx"]
        assert [service.name for service in nginx.services] == ["nginx"]

    def test_routing_daemons_are_added_only_when_routing_is_configured(self) -> None:
        with_ospf = assemble(plan())
        assert with_ospf.configuration.role("router") is not None
        without = assemble(plan(), single_router_lan())
        assert without.configuration.role("router") is None

    def test_plan_task_intentions_become_roles(self) -> None:
        cloud = assemble(
            plan(
                ansible_plan={
                    "targets": [{"component_id": "WEB1", "connection": {"user": "automation"}}],
                    "tasks": [
                        {
                            "id": "web-tools",
                            "target_ids": ["WEB1"],
                            "operation": "ansible.builtin.package",
                            "parameters": {"name": ["curl"], "state": "present"},
                        }
                    ],
                }
            )
        )
        role = cloud.configuration.role("web-tools")
        assert role is not None
        assert [package.name for package in role.packages] == ["curl"]

    def test_configuration_hosts_carry_no_address(self) -> None:
        cloud = assemble(plan())
        for host in cloud.configuration.hosts:
            serialised = host.model_dump_json()
            assert "10.10.10" not in serialised
            assert "10.20.20" not in serialised
            assert host.address_output_ref  # resolved after apply, from outputs

    def test_hosts_reference_real_compute(self) -> None:
        cloud = assemble(plan())
        compute_ids = {instance.id for instance in cloud.infrastructure.compute}
        assert all(host.compute_id in compute_ids for host in cloud.configuration.hosts)


class TestExplanations:
    def test_limitations_become_warnings(self) -> None:
        cloud = assemble(plan(limitations=["RIPv2 is not represented"]))
        assert any("RIPv2" in warning.message for warning in cloud.warnings)

    def test_dependencies_become_assumptions(self) -> None:
        cloud = assemble(
            plan(
                cloud_plan={
                    "component_mapping": [],
                    "networking": {},
                    "dependencies": ["an existing EC2 key pair"],
                }
            )
        )
        assert any("key pair" in assumption.statement for assumption in cloud.assumptions)

    def test_knowledge_citations_become_references(self) -> None:
        cloud = assemble(
            plan(knowledge=[{"rule_id": "CORE-001", "source": "kb/mapping_rules.md", "heading": "Core"}])
        )
        assert cloud.references[0].source == "kb/mapping_rules.md"

    def test_metadata_pins_the_source_revision(self) -> None:
        architecture = two_router_ospf()
        cloud = assemble(plan(), architecture)
        assert cloud.metadata.network_architecture_id == architecture.architecture_id
        assert cloud.metadata.network_architecture_revision == architecture.revision

    def test_the_same_input_assembles_identically(self) -> None:
        first = assemble(plan())
        second = assemble(plan())
        assert first.cloud_architecture_id == second.cloud_architecture_id
        assert first.infrastructure.counts() == second.infrastructure.counts()


class TestMalformedPlans:
    def test_a_plan_without_cloud_plan_is_refused(self) -> None:
        with pytest.raises(RAGError, match="cloud_plan"):
            assemble({"ansible_plan": {}})

    def test_a_non_object_cloud_plan_is_refused(self) -> None:
        with pytest.raises(RAGError, match="cloud_plan"):
            assemble({"cloud_plan": "a paragraph of prose"})

    def test_a_non_object_ansible_plan_is_refused(self) -> None:
        with pytest.raises(RAGError, match="ansible_plan"):
            assemble(plan(ansible_plan=["not", "an", "object"]))

    def test_unrecognised_representation_is_recorded_not_guessed(self) -> None:
        cloud = assemble(
            mapping({"component_id": "WEB1", "cloud_representation": "a magic box"})
        )
        assert any("magic box" in entry for entry in cloud.unmapped)

    def test_missing_optional_sections_are_tolerated(self) -> None:
        cloud = assemble({"cloud_plan": {}})
        assert cloud.infrastructure.networks
