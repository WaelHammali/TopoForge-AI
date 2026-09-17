"""Network value objects, geometry and provenance."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.domain.common.geometry import BoundingBox, Point, iou
from src.domain.common.identifiers import slugify, stable_id
from src.domain.common.provenance import (
    Evidence,
    ProvenanceSource,
    Sourced,
    merge_evidence,
)
from src.domain.network import IPAddressSpec, Interface, SubnetSpec, VLAN


class TestSubnetSpec:
    def test_host_bits_are_normalised_away(self) -> None:
        assert SubnetSpec(cidr="10.0.0.5/24").cidr == "10.0.0.0/24"

    def test_derived_fields(self) -> None:
        subnet = SubnetSpec(cidr="192.168.1.0/24")
        assert subnet.prefix_length == 24
        assert subnet.netmask == "255.255.255.0"
        assert subnet.usable_host_count == 254
        assert subnet.network_address == "192.168.1.0"
        assert subnet.broadcast_address == "192.168.1.255"

    @pytest.mark.parametrize(
        ("cidr", "expected"),
        [("10.0.0.0/30", 2), ("10.0.0.0/31", 2), ("10.0.0.0/32", 1), ("10.0.0.0/24", 254)],
    )
    def test_usable_host_count_handles_rfc3021(self, cidr: str, expected: int) -> None:
        assert SubnetSpec(cidr=cidr).usable_host_count == expected

    def test_containment_and_overlap(self) -> None:
        outer = SubnetSpec(cidr="10.0.0.0/16")
        inner = SubnetSpec(cidr="10.0.1.0/24")
        other = SubnetSpec(cidr="192.168.0.0/24")
        assert inner.is_subnet_of(outer)
        assert outer.overlaps(inner)
        assert not outer.overlaps(other)
        assert outer.contains("10.0.5.5")

    def test_cross_version_comparison_is_false_not_an_error(self) -> None:
        assert not SubnetSpec(cidr="10.0.0.0/8").overlaps(SubnetSpec(cidr="2001:db8::/32"))

    def test_invalid_cidr_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            SubnetSpec(cidr="10.0.0.0/33")


class TestIPAddressSpec:
    def test_without_prefix_nothing_is_invented(self) -> None:
        spec = IPAddressSpec(address="10.0.0.1")
        assert spec.prefix_length is None
        assert spec.netmask is None
        assert spec.subnet is None
        assert spec.cidr is None

    def test_same_subnet_is_tri_state(self) -> None:
        a = IPAddressSpec(address="10.0.0.1", prefix_length=24)
        b = IPAddressSpec(address="10.0.0.2", prefix_length=24)
        c = IPAddressSpec(address="10.0.1.2", prefix_length=24)
        unknown = IPAddressSpec(address="10.0.0.3")
        assert a.same_subnet_as(b) is True
        assert a.same_subnet_as(c) is False
        assert a.same_subnet_as(unknown) is None

    def test_prefix_must_fit_the_version(self) -> None:
        with pytest.raises(PydanticValidationError):
            IPAddressSpec(address="10.0.0.1", prefix_length=64)
        assert IPAddressSpec(address="2001:db8::1", prefix_length=64).version == 6


class TestInterface:
    def test_address_completeness_distinguishes_missing_mask(self) -> None:
        bare = Interface(
            id="i1",
            node_id="R1",
            name="Gi0/0",
            addresses=(
                Sourced(
                    value=IPAddressSpec(address="10.0.0.1"),
                    evidence=Evidence(source=ProvenanceSource.OCR, confidence=0.9),
                ),
            ),
        )
        assert bare.has_address is True
        assert bare.has_complete_address is False

    def test_with_address_does_not_mutate(self) -> None:
        original = Interface(id="i1", node_id="R1", name="Gi0/0")
        updated = original.with_address(
            IPAddressSpec(address="10.0.0.1", prefix_length=24), Evidence.from_user()
        )
        assert original.addresses == ()
        assert len(updated.addresses) == 1

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            Interface(id="i1", node_id="R1", name="  ")


class TestVLAN:
    @pytest.mark.parametrize("vlan_id", [0, 4095, -1])
    def test_reserved_ids_rejected(self, vlan_id: int) -> None:
        with pytest.raises(PydanticValidationError):
            VLAN(id=vlan_id)

    def test_valid_range(self) -> None:
        assert VLAN(id=4094).id == 4094


class TestGeometry:
    def test_iou(self) -> None:
        a = BoundingBox(x1=0, y1=0, x2=10, y2=10)
        b = BoundingBox(x1=5, y1=5, x2=15, y2=15)
        assert iou(a, b) == pytest.approx(25 / 175)
        assert iou(a, a) == pytest.approx(1.0)

    def test_disjoint_boxes(self) -> None:
        a = BoundingBox(x1=0, y1=0, x2=1, y2=1)
        b = BoundingBox(x1=5, y1=5, x2=6, y2=6)
        assert iou(a, b) == 0.0
        assert not a.overlaps(b)

    def test_distance_to_point_is_zero_inside(self) -> None:
        box = BoundingBox(x1=0, y1=0, x2=10, y2=10)
        assert box.distance_to_point(Point(x=5, y=5)) == 0.0
        assert box.distance_to_point(Point(x=13, y=5)) == pytest.approx(3.0)

    def test_inverted_box_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            BoundingBox(x1=10, y1=0, x2=0, y2=10)


class TestProvenance:
    def test_user_beats_ocr_and_keeps_the_loser(self) -> None:
        ocr = Sourced(
            value="10.0.0.1",
            evidence=Evidence(source=ProvenanceSource.OCR, confidence=0.99),
        )
        user = Sourced(value="10.0.0.2", evidence=Evidence.from_user("corrected"))
        merged = merge_evidence(ocr, user)
        assert merged.value == "10.0.0.2"
        assert merged.source is ProvenanceSource.USER
        assert [e.source for e in merged.superseded] == [ProvenanceSource.OCR]

    def test_default_never_beats_a_machine_inference(self) -> None:
        inferred = Sourced(
            value="a", evidence=Evidence(source=ProvenanceSource.LLM, confidence=0.4)
        )
        default = Sourced(value="b", evidence=Evidence.default_value("fallback"))
        assert merge_evidence(inferred, default).value == "a"

    def test_equal_priority_keeps_the_incumbent_so_replay_is_idempotent(self) -> None:
        first = Sourced(
            value="a", evidence=Evidence(source=ProvenanceSource.OCR, confidence=0.8)
        )
        second = Sourced(
            value="b", evidence=Evidence(source=ProvenanceSource.OCR, confidence=0.8)
        )
        assert merge_evidence(first, second).value == "a"

    def test_merging_into_nothing_returns_the_candidate(self) -> None:
        candidate = Sourced(value="x", evidence=Evidence.from_user())
        assert merge_evidence(None, candidate) is candidate

    def test_provenance_chain_survives_repeated_merges(self) -> None:
        current = Sourced(
            value="a", evidence=Evidence(source=ProvenanceSource.DEFAULT, confidence=0.5)
        )
        current = merge_evidence(
            current,
            Sourced(value="b", evidence=Evidence(source=ProvenanceSource.LLM, confidence=0.6)),
        )
        current = merge_evidence(current, Sourced(value="c", evidence=Evidence.from_user()))
        assert current.value == "c"
        assert {e.source for e in current.superseded} == {
            ProvenanceSource.DEFAULT,
            ProvenanceSource.LLM,
        }


class TestIdentifiers:
    def test_stable_id_is_deterministic(self) -> None:
        assert stable_id("router", 10, 20) == stable_id("router", 10, 20)
        assert stable_id("router", 10, 20) != stable_id("router", 10, 21)

    def test_slugify(self) -> None:
        assert slugify("  My Network!! ") == "my-network"
        assert slugify("///") == "unnamed"
