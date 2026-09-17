"""Shared fixtures.

The project root is on ``sys.path`` via ``pyproject.toml``'s ``pythonpath`` setting, so
``src.*`` imports resolve without an editable install.

This module also carries a minimal async-test shim. ``pytest-asyncio`` is the normal way
to run coroutine tests and is listed in the dev dependencies, but the suite must also run
on an interpreter that has only pytest — several of this project's target environments are
externally managed. When ``pytest-asyncio`` is installed the shim stands aside completely.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

try:  # pragma: no cover - depends on the environment
    import pytest_asyncio  # noqa: F401

    _HAS_PYTEST_ASYNCIO = True
except ImportError:  # pragma: no cover
    _HAS_PYTEST_ASYNCIO = False


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: run this coroutine test in an event loop")


def pytest_pyfunc_call(pyfuncitem: pytest.Function):
    """Run coroutine tests when no async plugin is installed."""
    if _HAS_PYTEST_ASYNCIO or not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    arguments = {
        name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames
    }
    asyncio.run(pyfuncitem.obj(**arguments))
    return True


from src.domain.architecture import ArchitectureMetadata, ArchitectureSource, NetworkArchitecture
from src.domain.common.provenance import Evidence, ProvenanceSource, Sourced
from src.domain.network import IPAddressSpec, Interface, InterfaceRole
from src.domain.topology import Edge, LinkEndpoint, Node, NodeType


def sourced_address(address: str, prefix: int | None = None, confidence: float = 0.95):
    return Sourced(
        value=IPAddressSpec(address=address, prefix_length=prefix),
        evidence=Evidence(source=ProvenanceSource.OCR, confidence=confidence),
    )


@pytest.fixture
def simple_network() -> NetworkArchitecture:
    """R1 --- SW1 --- S1, with addressing on R1 and S1.

    The shape used by most tests: one router, one switch, one server.
    """
    nodes = (
        Node(id="R1", type=NodeType.ROUTER, name=Sourced.from_user("R1")),
        Node(id="SW1", type=NodeType.SWITCH, name=Sourced.from_user("SW1")),
        Node(id="S1", type=NodeType.SERVER, name=Sourced.from_user("S1")),
    )
    interfaces = (
        Interface(
            id="R1:Gi0/0",
            node_id="R1",
            name="GigabitEthernet0/0",
            role=InterfaceRole.LAN,
            addresses=(sourced_address("10.0.1.1", 24),),
        ),
        Interface(
            id="S1:eth0",
            node_id="S1",
            name="eth0",
            role=InterfaceRole.LAN,
            addresses=(sourced_address("10.0.1.10", 24),),
        ),
    )
    edges = (
        Edge(
            id="e1",
            source=LinkEndpoint(node_id="R1", interface_id="R1:Gi0/0"),
            target=LinkEndpoint(node_id="SW1"),
        ),
        Edge(
            id="e2",
            source=LinkEndpoint(node_id="SW1"),
            target=LinkEndpoint(node_id="S1", interface_id="S1:eth0"),
        ),
    )
    return NetworkArchitecture(
        metadata=ArchitectureMetadata(name="simple", source=ArchitectureSource.IMAGE),
        nodes=nodes,
        interfaces=interfaces,
        edges=edges,
    )
