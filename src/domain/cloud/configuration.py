"""System / application configuration.

The half of :class:`~src.domain.cloud.models.CloudArchitecture` that Ansible consumes.

The split from :mod:`src.domain.cloud.resources` is a strict ownership boundary:
Terraform **provisions** (a VM exists, with this NIC, in this subnet, behind this security
group); Ansible **configures** (that VM has docker installed, nginx running, this file
written). Nothing is owned by both.

Note what is *absent*: there are no IP addresses here. A configuration host references a
compute resource by id; its real address is only known after apply, and is supplied by the
``InventoryGenerator`` from Terraform outputs.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.domain.common.provenance import Evidence

__all__ = [
    "ConfigFile",
    "Configuration",
    "ConfigurationHost",
    "ConnectionMethod",
    "PackageRequirement",
    "ServiceRequirement",
    "ServiceState",
    "SystemRole",
]


class ConnectionMethod(str, Enum):
    SSH = "ssh"
    SSM = "ssm"
    WINRM = "winrm"
    LOCAL = "local"


class ConfigurationHost(BaseModel):
    """A machine Ansible will configure.

    It names a compute resource; it does **not** carry an address. Addresses arrive after
    Terraform apply via the inventory generator.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    #: Inventory hostname, e.g. "web1".
    name: str
    #: The compute resource this host runs on.
    compute_id: str
    #: Inventory groups. Usually mirrors the compute resource's roles.
    groups: tuple[str, ...] = ()
    #: Role names applied to this host, resolved against ``Configuration.roles``.
    roles: tuple[str, ...] = ()
    connection: ConnectionMethod = ConnectionMethod.SSH
    remote_user: str | None = None
    #: True when the host is only reachable through a bastion.
    requires_bastion: bool = False
    bastion_host_id: str | None = None
    #: Which Terraform output supplies this host's address after apply.
    address_output_ref: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence | None = None

    @field_validator("name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("host name must not be empty")
        return value.strip()


class ServiceState(str, Enum):
    STARTED = "started"
    STOPPED = "stopped"
    RESTARTED = "restarted"
    RELOADED = "reloaded"


class PackageRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str | None = None
    state: str = "present"


class ServiceRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    state: ServiceState = ServiceState.STARTED
    enabled: bool = True


class ConfigFile(BaseModel):
    """A file the configuration layer must place on a host."""

    model_config = ConfigDict(frozen=True)

    path: str
    #: Either literal content or a template name resolved by the generator.
    content: str | None = None
    template: str | None = None
    owner: str | None = None
    group: str | None = None
    mode: str | None = None
    #: Services to restart when this file changes.
    notifies: tuple[str, ...] = ()


class SystemRole(BaseModel):
    """A reusable unit of configuration: packages + services + files + variables."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    description: str | None = None
    packages: tuple[PackageRequirement, ...] = ()
    services: tuple[ServiceRequirement, ...] = ()
    files: tuple[ConfigFile, ...] = ()
    variables: dict[str, Any] = Field(default_factory=dict)
    #: Roles that must be applied before this one. Named "requires" rather than
    #: "depends_on" so that no IaC ordering vocabulary leaks into the cloud schema.
    requires: tuple[str, ...] = ()
    #: Set when the role corresponds to a known upstream pattern (e.g. FRR on a router).
    kind: str | None = None
    evidence: Evidence | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("role name must not be empty")
        if "/" in cleaned or cleaned.startswith("."):
            raise ValueError(f"unsafe role name: {value!r}")
        return cleaned


class Configuration(BaseModel):
    """Everything Ansible configures. Terraform never owns anything in here."""

    model_config = ConfigDict(frozen=True)

    hosts: tuple[ConfigurationHost, ...] = ()
    roles: tuple[SystemRole, ...] = ()
    #: Group-level variables, keyed by inventory group name.
    group_variables: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: Global variables applied to every host.
    global_variables: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.hosts or self.roles)

    def role(self, name: str) -> SystemRole | None:
        return next((item for item in self.roles if item.name == name or item.id == name), None)

    def hosts_in_group(self, group: str) -> tuple[ConfigurationHost, ...]:
        return tuple(host for host in self.hosts if group in host.groups)

    def groups(self) -> tuple[str, ...]:
        seen: list[str] = []
        for host in self.hosts:
            for group in host.groups:
                if group not in seen:
                    seen.append(group)
        return tuple(seen)

    def unresolved_role_names(self) -> tuple[str, ...]:
        """Role names referenced by hosts that no role defines."""
        defined = {role.name for role in self.roles} | {role.id for role in self.roles}
        missing: list[str] = []
        for host in self.hosts:
            for name in host.roles:
                if name not in defined and name not in missing:
                    missing.append(name)
        for role in self.roles:
            for dependency in role.requires:
                if dependency not in defined and dependency not in missing:
                    missing.append(dependency)
        return tuple(missing)

    def counts(self) -> dict[str, int]:
        return {
            "hosts": len(self.hosts),
            "roles": len(self.roles),
            "groups": len(self.groups()),
        }
