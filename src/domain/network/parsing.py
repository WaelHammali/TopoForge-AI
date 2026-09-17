"""Deterministic parsing of networking text.

This module exists because of one rule in the build specification: *never ask an LLM to
perform a deterministic IP calculation that Python can perform exactly*. Everything here
is regex + :mod:`ipaddress`, with no model in the loop.

It consumes raw strings — typically OCR output — and produces canonical
:class:`~src.domain.network.models.IPAddressSpec` / ``SubnetSpec`` values plus structured
:class:`ParseIssue` records for anything malformed.

Recognised forms::

    192.168.1.10/24
    192.168.1.10 255.255.255.0
    10.0.0.1 /30
    10.0.0.1 mask 255.255.255.252
    ip address 10.0.0.1 255.255.255.0
    2001:db8::1/64
    Gi0/0, Gig0/0/1, GigabitEthernet0/1, Fa0/1, Te1/0/1, eth0, ens192, Vlan20, Lo0
    VLAN 20, Vlan20, vlan-id 20
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from enum import Enum

from src.domain.network.models import IPAddressSpec, SubnetSpec

__all__ = [
    "ParseIssue",
    "ParseIssueKind",
    "ParsedAddress",
    "ParsedNetworkText",
    "netmask_to_prefix",
    "normalize_interface_name",
    "parse_cidr",
    "parse_ip_and_mask",
    "parse_network_text",
    "parse_vlan_id",
    "prefix_to_netmask",
]

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = rf"(?:{_OCTET}\.){{3}}{_OCTET}"
# Deliberately permissive so that malformed values are *captured and reported* rather than
# silently skipped. Validation happens in ipaddress, not in the regex.
_IPV4_LOOSE = r"(?:\d{1,3}\.){3}\d{1,3}"
_IPV6_LOOSE = r"(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[0-9A-Za-z]+)?"

_CIDR_RE = re.compile(rf"\b(?P<addr>{_IPV4_LOOSE})\s*/\s*(?P<prefix>\d{{1,3}})\b")
_IPV6_CIDR_RE = re.compile(rf"(?P<addr>{_IPV6_LOOSE})\s*/\s*(?P<prefix>\d{{1,3}})")
_IP_MASK_RE = re.compile(
    rf"\b(?P<addr>{_IPV4_LOOSE})\s+(?:mask\s+|netmask\s+|subnet\s+mask\s+)?(?P<mask>{_IPV4_LOOSE})\b",
    re.IGNORECASE,
)
_BARE_IPV4_RE = re.compile(rf"\b(?P<addr>{_IPV4_LOOSE})\b")
_BARE_IPV6_RE = re.compile(rf"(?<![\w:]){_IPV6_LOOSE}(?![\w:])")

_VLAN_RE = re.compile(r"\bvlan[\s._-]*(?:id[\s._-]*)?(\d{1,4})\b", re.IGNORECASE)

# Interface abbreviations seen on network diagrams, mapped to canonical long names.
_INTERFACE_FAMILIES: tuple[tuple[str, str], ...] = (
    (r"(?:twentyfivegige?|twentyfivegigabitethernet|twe)", "TwentyFiveGigE"),
    (r"(?:hundredgige?|hundredgigabitethernet|hu)", "HundredGigE"),
    (r"(?:fortygige?|fortygigabitethernet|fo)", "FortyGigabitEthernet"),
    (r"(?:tengigabitethernet|tengige?|te|xe)", "TenGigabitEthernet"),
    (r"(?:gigabitethernet|gigabit|gige?|gi|g)", "GigabitEthernet"),
    (r"(?:fastethernet|fast|fa|f)", "FastEthernet"),
    (r"(?:ethernet|eth|et|e)", "Ethernet"),
    (r"(?:serial|ser|se|s)", "Serial"),
    (r"(?:loopback|loop|lo)", "Loopback"),
    (r"(?:port-?channel|portchannel|po)", "Port-channel"),
    (r"(?:tunnel|tun|tu)", "Tunnel"),
    (r"(?:vlan|vl)", "Vlan"),
    (r"(?:management|mgmt|ma)", "Management"),
    (r"(?:bundle-ether|be)", "Bundle-Ether"),
)
_INTERFACE_RE = re.compile(
    r"\b(?P<family>"
    + "|".join(pattern for pattern, _ in _INTERFACE_FAMILIES)
    + r")\s*[-_]?\s*(?P<numbers>\d+(?:[/:.]\d+)*)\b",
    re.IGNORECASE,
)
# Linux-style names are matched separately: they carry no slash-numbering.
_LINUX_IFACE_RE = re.compile(r"\b(?:eth|ens|enp|eno|wlan|bond|br|tun|tap)\d+(?:[a-z]\d+)?\b")

# Device labels such as R1, SW2, FW1. The lookarounds keep it out of the middle of an
# IPv6 literal or a hostname: "2001:db8::1" must not yield a "DB8" device.
_DEVICE_HINT_RE = re.compile(
    r"(?<![\w:.])(?:R|RTR|SW|FW|SRV|PC|LB|AP|GW|DB)[-_]?\d+[A-Za-z]?(?![\w:.])", re.IGNORECASE
)


class ParseIssueKind(str, Enum):
    INVALID_IP = "invalid_ip"
    INVALID_MASK = "invalid_mask"
    INVALID_PREFIX = "invalid_prefix"
    INVALID_CIDR = "invalid_cidr"
    NETWORK_ADDRESS_ASSIGNED = "network_address_assigned"
    BROADCAST_ADDRESS_ASSIGNED = "broadcast_address_assigned"
    INVALID_VLAN_ID = "invalid_vlan_id"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class ParseIssue:
    kind: ParseIssueKind
    raw: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "raw": self.raw, "message": self.message}


@dataclass(frozen=True, slots=True)
class ParsedAddress:
    """One address recovered from text, with the exact substring it came from."""

    spec: IPAddressSpec
    raw: str
    #: Character offsets of ``raw`` inside the source string.
    span: tuple[int, int] = (0, 0)
    #: True when the prefix came from a dotted mask rather than slash notation.
    from_netmask: bool = False


@dataclass(slots=True)
class ParsedNetworkText:
    """Everything deterministically extractable from one piece of text."""

    source_text: str
    addresses: list[ParsedAddress] = field(default_factory=list)
    subnets: list[SubnetSpec] = field(default_factory=list)
    interface_names: list[str] = field(default_factory=list)
    vlan_ids: list[int] = field(default_factory=list)
    device_hints: list[str] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.addresses
            or self.subnets
            or self.interface_names
            or self.vlan_ids
            or self.device_hints
        )

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)


# --------------------------------------------------------------------------- #
# Mask helpers
# --------------------------------------------------------------------------- #
def netmask_to_prefix(mask: str) -> int | None:
    """Convert a dotted mask to a prefix length, or ``None`` if it is not contiguous.

    ``255.255.255.0`` -> 24. ``255.0.255.0`` -> ``None`` (non-contiguous, invalid).
    Wildcard masks such as ``0.0.0.255`` are also recognised and inverted.
    """
    candidate = mask.strip()
    try:
        packed = int(ipaddress.IPv4Address(candidate))
    except ValueError:
        return None

    for value, inverted in ((packed, False), (packed ^ 0xFFFFFFFF, True)):
        bits = f"{value:032b}"
        if "01" in bits:  # not a contiguous run of ones
            continue
        prefix = bits.count("1")
        # A wildcard reading is only accepted when the direct reading failed.
        if inverted and prefix in (0, 32):
            continue
        return prefix
    return None


def prefix_to_netmask(prefix: int, version: int = 4) -> str:
    if version == 4:
        if not 0 <= prefix <= 32:
            raise ValueError(f"invalid IPv4 prefix: {prefix}")
        return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
    if not 0 <= prefix <= 128:
        raise ValueError(f"invalid IPv6 prefix: {prefix}")
    return str(ipaddress.IPv6Network(f"::/{prefix}").netmask)


# --------------------------------------------------------------------------- #
# Single-value parsers
# --------------------------------------------------------------------------- #
def parse_cidr(value: str) -> tuple[IPAddressSpec | None, list[ParseIssue]]:
    """Parse ``address/prefix``. The address keeps its host bits."""
    text = value.strip()
    if "/" not in text:
        return None, [ParseIssue(ParseIssueKind.INVALID_CIDR, text, "missing '/' prefix")]

    addr_part, _, prefix_part = text.partition("/")
    addr_part, prefix_part = addr_part.strip(), prefix_part.strip()

    try:
        address = ipaddress.ip_address(addr_part)
    except ValueError:
        return None, [ParseIssue(ParseIssueKind.INVALID_IP, text, f"{addr_part!r} is not an IP")]

    try:
        prefix = int(prefix_part)
    except ValueError:
        return None, [
            ParseIssue(ParseIssueKind.INVALID_PREFIX, text, f"{prefix_part!r} is not an integer")
        ]

    limit = 32 if address.version == 4 else 128
    if not 0 <= prefix <= limit:
        return None, [
            ParseIssue(
                ParseIssueKind.INVALID_PREFIX, text, f"/{prefix} is out of range for IPv{address.version}"
            )
        ]

    spec = IPAddressSpec(address=str(address), prefix_length=prefix)
    return spec, _host_bit_issues(spec, text)


def parse_ip_and_mask(address: str, mask: str) -> tuple[IPAddressSpec | None, list[ParseIssue]]:
    """Parse the ``10.0.0.1 255.255.255.0`` form."""
    try:
        parsed_address = ipaddress.ip_address(address.strip())
    except ValueError:
        return None, [
            ParseIssue(ParseIssueKind.INVALID_IP, address, f"{address!r} is not an IP address")
        ]

    prefix = netmask_to_prefix(mask)
    if prefix is None:
        return None, [
            ParseIssue(
                ParseIssueKind.INVALID_MASK,
                mask,
                f"{mask!r} is not a valid contiguous subnet mask",
            )
        ]

    spec = IPAddressSpec(address=str(parsed_address), prefix_length=prefix)
    return spec, _host_bit_issues(spec, f"{address} {mask}")


def parse_vlan_id(value: str) -> tuple[int | None, list[ParseIssue]]:
    match = _VLAN_RE.search(value) or re.search(r"\b(\d{1,4})\b", value.strip())
    if match is None:
        return None, []
    vlan = int(match.group(1))
    if not 1 <= vlan <= 4094:
        return None, [
            ParseIssue(
                ParseIssueKind.INVALID_VLAN_ID, value, f"VLAN {vlan} is outside the range 1-4094"
            )
        ]
    return vlan, []


def normalize_interface_name(value: str) -> str | None:
    """Canonicalise an interface label.

    ``Gig0/0`` / ``gi 0/0`` / ``GigabitEthernet0/0`` all normalise to
    ``GigabitEthernet0/0``. Linux names are returned lowercased and unchanged.
    """
    text = value.strip()
    if not text:
        return None

    linux = _LINUX_IFACE_RE.search(text.lower())
    if linux is not None and _INTERFACE_RE.match(text) is None:
        return linux.group(0)

    match = _INTERFACE_RE.search(text)
    if match is None:
        return None

    family_text = match.group("family").lower().replace("-", "").replace("_", "")
    numbers = match.group("numbers")
    for pattern, canonical in _INTERFACE_FAMILIES:
        if re.fullmatch(pattern, family_text, re.IGNORECASE):
            return f"{canonical}{numbers}"
    return None


# --------------------------------------------------------------------------- #
# Whole-string parser
# --------------------------------------------------------------------------- #
def parse_network_text(text: str) -> ParsedNetworkText:
    """Extract every networking fact from one piece of text.

    Overlapping matches are resolved by precedence: CIDR beats address+mask, which beats a
    bare address. A span consumed by a stronger form is not re-parsed by a weaker one.
    """
    result = ParsedNetworkText(source_text=text)
    consumed: list[tuple[int, int]] = []

    def is_free(span: tuple[int, int]) -> bool:
        return not any(span[0] < end and start < span[1] for start, end in consumed)

    # 1. IPv6 CIDR (checked before IPv4 so that "::/0" style text is not mangled).
    for match in _IPV6_CIDR_RE.finditer(text):
        if ":" not in match.group("addr"):
            continue
        span = match.span()
        if not is_free(span):
            continue
        spec, issues = parse_cidr(match.group(0))
        result.issues.extend(issues)
        if spec is not None:
            result.addresses.append(ParsedAddress(spec=spec, raw=match.group(0), span=span))
            consumed.append(span)

    # 2. IPv4 CIDR.
    for match in _CIDR_RE.finditer(text):
        span = match.span()
        if not is_free(span):
            continue
        spec, issues = parse_cidr(match.group(0))
        result.issues.extend(issues)
        consumed.append(span)
        if spec is not None:
            result.addresses.append(ParsedAddress(spec=spec, raw=match.group(0), span=span))

    # 3. address + dotted mask.
    for match in _IP_MASK_RE.finditer(text):
        span = match.span()
        if not is_free(span):
            continue
        spec, issues = parse_ip_and_mask(match.group("addr"), match.group("mask"))
        if spec is None:
            # Two adjacent plain addresses are not an address/mask pair; leave them for
            # the bare-address pass rather than reporting a spurious mask error.
            if any(issue.kind is ParseIssueKind.INVALID_MASK for issue in issues):
                continue
            result.issues.extend(issues)
            continue
        result.issues.extend(issues)
        consumed.append(span)
        result.addresses.append(
            ParsedAddress(spec=spec, raw=match.group(0), span=span, from_netmask=True)
        )

    # 4. Bare addresses (no prefix known — this is exactly what the completeness analyzer
    #    later reports as "missing subnet mask").
    for match in _BARE_IPV4_RE.finditer(text):
        span = match.span()
        if not is_free(span):
            continue
        raw = match.group("addr")
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            result.issues.append(
                ParseIssue(ParseIssueKind.INVALID_IP, raw, f"{raw!r} is not a valid IPv4 address")
            )
            consumed.append(span)
            continue
        consumed.append(span)
        result.addresses.append(
            ParsedAddress(spec=IPAddressSpec(address=str(address)), raw=raw, span=span)
        )

    # 5. Interfaces. Linux-style names win over the abbreviation table wherever the two
    #    overlap, so "eth0" is reported once as "eth0" and not also as "Ethernet0".
    interface_spans: list[tuple[int, int]] = []
    for match in _LINUX_IFACE_RE.finditer(text.lower()):
        interface_spans.append(match.span())
        if match.group(0) not in result.interface_names:
            result.interface_names.append(match.group(0))

    for match in _INTERFACE_RE.finditer(text):
        span = match.span()
        if any(span[0] < end and start < span[1] for start, end in interface_spans):
            continue
        canonical = normalize_interface_name(match.group(0))
        if canonical and canonical not in result.interface_names:
            result.interface_names.append(canonical)
            interface_spans.append(span)

    # 6. VLAN ids.
    for match in _VLAN_RE.finditer(text):
        vlan = int(match.group(1))
        if 1 <= vlan <= 4094:
            if vlan not in result.vlan_ids:
                result.vlan_ids.append(vlan)
        else:
            result.issues.append(
                ParseIssue(
                    ParseIssueKind.INVALID_VLAN_ID,
                    match.group(0),
                    f"VLAN {vlan} is outside the range 1-4094",
                )
            )

    # 7. Device labels.
    for match in _DEVICE_HINT_RE.finditer(text):
        hint = match.group(0).upper().replace("-", "").replace("_", "")
        if hint not in result.device_hints:
            result.device_hints.append(hint)

    # 8. Derive the distinct subnets implied by the complete addresses.
    for parsed in result.addresses:
        subnet = parsed.spec.subnet
        if subnet is not None and all(existing.cidr != subnet.cidr for existing in result.subnets):
            result.subnets.append(subnet)

    return result


def _host_bit_issues(spec: IPAddressSpec, raw: str) -> list[ParseIssue]:
    """Report assignment of a reserved address. Reported, never silently corrected."""
    issues: list[ParseIssue] = []
    if spec.is_network_address:
        issues.append(
            ParseIssue(
                ParseIssueKind.NETWORK_ADDRESS_ASSIGNED,
                raw,
                f"{spec.address} is the network address of {spec.subnet}",
            )
        )
    if spec.is_broadcast_address:
        issues.append(
            ParseIssue(
                ParseIssueKind.BROADCAST_ADDRESS_ASSIGNED,
                raw,
                f"{spec.address} is the broadcast address of {spec.subnet}",
            )
        )
    return issues
