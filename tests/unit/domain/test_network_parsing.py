"""Deterministic network-text parsing.

These are the tests the build specification names first, because every downstream stage
trusts this module to be exact. No LLM is involved in any of this behaviour.
"""

from __future__ import annotations

import pytest

from src.domain.network import (
    IPAddressSpec,
    SubnetSpec,
    netmask_to_prefix,
    normalize_interface_name,
    parse_cidr,
    parse_ip_and_mask,
    parse_network_text,
    parse_vlan_id,
)
from src.domain.network.parsing import ParseIssueKind, prefix_to_netmask


class TestMaskConversion:
    @pytest.mark.parametrize(
        ("mask", "expected"),
        [
            ("255.255.255.0", 24),
            ("255.255.255.252", 30),
            ("255.255.255.255", 32),
            ("255.255.0.0", 16),
            ("128.0.0.0", 1),
        ],
    )
    def test_valid_masks(self, mask: str, expected: int) -> None:
        assert netmask_to_prefix(mask) == expected

    def test_zero_mask(self) -> None:
        assert netmask_to_prefix("0.0.0.0") == 0

    @pytest.mark.parametrize("mask", ["255.0.255.0", "255.255.1.0", "not-a-mask", "256.0.0.0"])
    def test_non_contiguous_masks_are_rejected(self, mask: str) -> None:
        assert netmask_to_prefix(mask) is None

    def test_wildcard_mask_is_recognised(self) -> None:
        # Cisco ACL wildcard form; 0.0.0.255 is the inverse of a /24.
        assert netmask_to_prefix("0.0.0.255") == 24

    def test_round_trip(self) -> None:
        for prefix in range(0, 33):
            assert netmask_to_prefix(prefix_to_netmask(prefix)) == prefix


class TestCIDRParsing:
    def test_host_bits_are_preserved(self) -> None:
        spec, issues = parse_cidr("192.168.1.10/24")
        assert issues == []
        assert spec is not None
        assert spec.address == "192.168.1.10"
        assert spec.prefix_length == 24
        assert spec.netmask == "255.255.255.0"
        assert spec.subnet == SubnetSpec(cidr="192.168.1.0/24")

    def test_ipv6(self) -> None:
        spec, issues = parse_cidr("2001:db8::1/64")
        assert issues == []
        assert spec is not None
        assert spec.version == 6

    @pytest.mark.parametrize(
        ("text", "kind"),
        [
            ("10.0.0.1", ParseIssueKind.INVALID_CIDR),
            ("999.1.1.1/24", ParseIssueKind.INVALID_IP),
            ("10.0.0.1/33", ParseIssueKind.INVALID_PREFIX),
            ("10.0.0.1/abc", ParseIssueKind.INVALID_PREFIX),
        ],
    )
    def test_invalid_forms_are_reported(self, text: str, kind: ParseIssueKind) -> None:
        spec, issues = parse_cidr(text)
        assert spec is None
        assert [issue.kind for issue in issues] == [kind]

    def test_network_address_assignment_is_reported_not_corrected(self) -> None:
        spec, issues = parse_cidr("10.0.0.0/24")
        assert spec is not None
        assert spec.address == "10.0.0.0"  # not silently moved to .1
        assert [issue.kind for issue in issues] == [ParseIssueKind.NETWORK_ADDRESS_ASSIGNED]

    def test_broadcast_address_assignment_is_reported(self) -> None:
        _, issues = parse_cidr("10.0.0.255/24")
        assert [issue.kind for issue in issues] == [ParseIssueKind.BROADCAST_ADDRESS_ASSIGNED]

    def test_point_to_point_endpoints_are_not_reserved(self) -> None:
        # RFC 3021: a /31 has no network or broadcast address.
        for address in ("10.0.0.0/31", "10.0.0.1/31"):
            _, issues = parse_cidr(address)
            assert issues == []


class TestAddressAndMask:
    def test_dotted_mask_form(self) -> None:
        spec, issues = parse_ip_and_mask("10.0.0.1", "255.255.255.0")
        assert issues == []
        assert spec == IPAddressSpec(address="10.0.0.1", prefix_length=24)

    def test_invalid_mask_is_reported(self) -> None:
        spec, issues = parse_ip_and_mask("10.0.0.1", "255.0.255.0")
        assert spec is None
        assert issues[0].kind is ParseIssueKind.INVALID_MASK


class TestInterfaceNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Gig0/0", "GigabitEthernet0/0"),
            ("gi 0/0", "GigabitEthernet0/0"),
            ("GigabitEthernet0/0/1", "GigabitEthernet0/0/1"),
            ("Fa0/1", "FastEthernet0/1"),
            ("Te1/0/1", "TenGigabitEthernet1/0/1"),
            ("Lo0", "Loopback0"),
            ("Vlan20", "Vlan20"),
            ("Se0/0/0", "Serial0/0/0"),
            ("eth0", "eth0"),
            ("ens192", "ens192"),
        ],
    )
    def test_canonical_names(self, raw: str, expected: str) -> None:
        assert normalize_interface_name(raw) == expected

    def test_unrecognised_text_returns_none(self) -> None:
        assert normalize_interface_name("the quick brown fox") is None
        assert normalize_interface_name("") is None


class TestVLANParsing:
    @pytest.mark.parametrize("text", ["VLAN 20", "vlan20", "Vlan-20", "vlan id 20"])
    def test_valid(self, text: str) -> None:
        vlan, issues = parse_vlan_id(text)
        assert (vlan, issues) == (20, [])

    @pytest.mark.parametrize("text", ["VLAN 0", "vlan 4095", "vlan 9999"])
    def test_out_of_range_is_reported(self, text: str) -> None:
        vlan, issues = parse_vlan_id(text)
        assert vlan is None
        assert issues[0].kind is ParseIssueKind.INVALID_VLAN_ID


class TestWholeStringParsing:
    def test_typical_ocr_label(self) -> None:
        result = parse_network_text("R1 Gig0/0 192.168.1.10/24")
        assert [a.spec.cidr for a in result.addresses] == ["192.168.1.10/24"]
        assert result.interface_names == ["GigabitEthernet0/0"]
        assert result.device_hints == ["R1"]
        assert not result.has_issues

    def test_address_and_mask_on_one_line(self) -> None:
        result = parse_network_text("10.0.0.1 255.255.255.0 on Fa0/1")
        assert len(result.addresses) == 1
        assert result.addresses[0].from_netmask is True
        assert result.addresses[0].spec.prefix_length == 24

    def test_bare_address_has_no_prefix(self) -> None:
        # This is exactly what the completeness analyzer later reports as a missing mask.
        result = parse_network_text("R1 10.0.0.1")
        assert result.addresses[0].spec.prefix_length is None
        assert result.subnets == []

    def test_cidr_is_not_also_parsed_as_a_bare_address(self) -> None:
        result = parse_network_text("10.0.0.1/24")
        assert len(result.addresses) == 1

    def test_ipv6_literal_does_not_produce_a_device_hint(self) -> None:
        # "db8" inside 2001:db8::1 must not be read as a device called DB8.
        result = parse_network_text("2001:db8::1/64 eth0")
        assert result.device_hints == []
        assert result.interface_names == ["eth0"]

    def test_linux_interface_is_not_double_counted(self) -> None:
        result = parse_network_text("server eth0 is up")
        assert result.interface_names == ["eth0"]

    def test_two_adjacent_addresses_are_not_read_as_address_and_mask(self) -> None:
        result = parse_network_text("hosts 10.0.0.5 10.0.0.6")
        assert {a.spec.address for a in result.addresses} == {"10.0.0.5", "10.0.0.6"}
        assert all(a.spec.prefix_length is None for a in result.addresses)

    def test_subnets_are_derived_from_complete_addresses(self) -> None:
        result = parse_network_text("10.0.1.1/24 and 10.0.2.1/24 and 10.0.1.9/24")
        assert [s.cidr for s in result.subnets] == ["10.0.1.0/24", "10.0.2.0/24"]

    def test_empty_text(self) -> None:
        assert parse_network_text("").is_empty
