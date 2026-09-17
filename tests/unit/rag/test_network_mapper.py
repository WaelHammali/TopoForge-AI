"""Mapping the canonical network architecture onto the RAG's documented input shape."""

from __future__ import annotations

from src.domain.common.provenance import Evidence, ProvenanceSource, Sourced
from src.domain.network import IPAddressSpec, Interface
from src.infrastructure.rag.network_mapper import NetworkArchitectureMapper
from tests.fixtures.network_architectures import single_router_lan, two_router_ospf, two_router_static


def payload_for(architecture, **kwargs):
    return NetworkArchitectureMapper(**kwargs).map(architecture).payload


def component(payload, component_id):
    return next(c for c in payload["components"] if c["id"] == component_id)


class TestShape:
    def test_matches_the_documented_contract(self) -> None:
        payload = payload_for(two_router_ospf())
        assert payload["schema_version"] == "1.0"
        assert payload["translation_mode"] == "behavioral_lab"
        assert payload["cloud"] == {"provider": "aws", "region": "eu-west-1"}
        assert {"components", "edges"} <= set(payload)

    def test_provider_and_region_are_explicit_not_assumed(self) -> None:
        payload = payload_for(single_router_lan(), provider="aws", region="us-west-2")
        assert payload["cloud"]["region"] == "us-west-2"

    def test_every_node_and_edge_is_represented(self) -> None:
        architecture = two_router_ospf()
        payload = payload_for(architecture)
        assert len(payload["components"]) == len(architecture.nodes)
        assert len(payload["edges"]) == len(architecture.edges)

    def test_edges_are_interface_level(self) -> None:
        edge = payload_for(two_router_ospf())["edges"][0]
        assert edge["source"] == {"component": "PC1", "interface": "eth0"}
        assert edge["target"] == {"component": "SW1", "interface": "port1"}


class TestInterfaces:
    def test_addresses_are_sent_as_cidr(self) -> None:
        interfaces = component(payload_for(two_router_ospf()), "R1")["interfaces"]
        assert {"id": "lan0", "enabled": True, "ipv4": "10.10.10.1/24", "role": "lan"} in interfaces

    def test_an_address_without_a_prefix_is_omitted_not_guessed(self) -> None:
        """A bare address is not an interface address, and a mask is never invented."""
        architecture = single_router_lan()
        bare = Interface(
            id="R1:lan1",
            node_id="R1",
            name="lan1",
            addresses=(
                Sourced(
                    value=IPAddressSpec(address="192.168.1.1"),
                    evidence=Evidence(source=ProvenanceSource.OCR, confidence=0.8),
                ),
            ),
        )
        modified = architecture.model_copy(
            update={"interfaces": (*architecture.interfaces, bare)}
        )
        entry = next(
            i for i in component(payload_for(modified), "R1")["interfaces"] if i["id"] == "lan1"
        )
        assert "ipv4" not in entry
        assert "192.168" not in str(entry)

    def test_vlan_membership_is_carried(self) -> None:
        interfaces = component(payload_for(two_router_ospf()), "SW1")["interfaces"]
        assert all(entry["access_vlan"] == 10 for entry in interfaces)


class TestRouting:
    def test_ospf_process_is_expressed_per_interface_with_areas(self) -> None:
        routing = component(payload_for(two_router_ospf()), "R1")["routing"]
        ospf = routing["protocols"][0]
        assert ospf["name"] == "ospf"
        assert ospf["router_id"] == "1.1.1.1"
        assert {"id": "lan0", "area": "0.0.0.0", "passive": True} in ospf["interfaces"]
        assert {"id": "wan0", "area": "0.0.0.0"} in ospf["interfaces"]

    def test_routers_forward(self) -> None:
        assert component(payload_for(two_router_ospf()), "R1")["routing"]["ipv4_forwarding"] is True

    def test_static_routes_carry_destination_via_interface_and_metric(self) -> None:
        routing = component(payload_for(two_router_static()), "R1")["routing"]
        assert routing["static_routes"] == [
            {
                "destination": "10.20.20.0/24",
                "via": "10.255.0.2",
                "interface": "wan0",
                "metric": 1,
            }
        ]

    def test_an_empty_protocol_list_is_sent_when_no_routing_was_chosen(self) -> None:
        """Absence is information: the RAG's contract distinguishes [] from omitted."""
        routing = component(payload_for(single_router_lan()), "R1")["routing"]
        assert routing["protocols"] == []


class TestServicesAndAutomation:
    def test_services_are_forwarded_verbatim(self) -> None:
        services = component(payload_for(two_router_ospf()), "WEB1")["services"]
        assert services[0]["implementation"] == "nginx"
        assert services[0]["listen"]["port"] == 80

    def test_os_information_is_forwarded(self) -> None:
        assert component(payload_for(two_router_ospf()), "WEB1")["os"]["distribution"] == "debian"

    def test_ansible_connections_use_symbolic_references_not_addresses(self) -> None:
        ansible = payload_for(two_router_ospf())["ansible"]
        connection = ansible["connections"][0]
        assert connection["host_ref"] == "deployment.management.WEB1"
        assert "10." not in connection["host_ref"]


class TestFidelityReporting:
    def test_nothing_is_dropped_silently(self) -> None:
        architecture = two_router_ospf()
        # Give one edge no interfaces, so it cannot be expressed at interface level.
        edges = (
            architecture.edges[0].model_copy(
                update={
                    "source": architecture.edges[0].source.model_copy(
                        update={"interface_id": None}
                    )
                }
            ),
            *architecture.edges[1:],
        )
        mapped = NetworkArchitectureMapper().map(architecture.model_copy(update={"edges": edges}))
        assert any("not interface-level" in entry for entry in mapped.unmapped)

    def test_a_clean_architecture_maps_without_loss(self) -> None:
        assert NetworkArchitectureMapper().map(two_router_ospf()).unmapped == []

    def test_provenance_is_not_sent_to_the_model(self) -> None:
        # Extraction confidence is a clarification-stage concern and has no bearing on
        # cloud translation; sending it would invite the model to reason about it.
        payload = str(payload_for(two_router_ospf()))
        assert "confidence" not in payload
        assert "OCR" not in payload
