# `cloud_architecture.json`

The RAG's output and the generators' input. Defined in `src/domain/cloud/`, versioned at
`schema_version: "1.0"`.

## The two halves

```
CloudArchitecture
├── infrastructure   ← Terraform provisions this
└── configuration    ← Ansible configures this
```

The split is an ownership boundary, not a presentation choice. A concern belongs to
exactly one half. Terraform decides that a machine exists, with this NIC, in this subnet,
behind this security group. Ansible decides that the machine has docker installed and
nginx running. Neither manages the other's concern, so there is no drift between two
systems that both believe they own a resource.

Enforced by test: `tests/unit/generators/test_ansible_generator.py` fails if an instance
type or a CIDR appears anywhere in generated Ansible, and if a cloud provisioning module
appears in it at all.

## Top level

| Field | Meaning |
| --- | --- |
| `schema_version` | `"1.0"`. An unknown version raises `SchemaVersionError` rather than being read optimistically. |
| `metadata` | Identity, provider, region, and the **exact** source: `network_architecture_id` + `network_architecture_revision`. Also `deployment_pattern`, `confidence` and `producers` (RAG revision, model, adapter version). |
| `infrastructure` | Networks, subnets, route tables, gateways, compute, databases, load balancers, security groups. |
| `configuration` | Hosts, roles, services, packages, group and host variables. |
| `relationships` | Typed edges between resources (`contains`, `attached_to`, `routes_via`, `peered_with`, …). Architecture, not ordering: Terraform dependency order is derived from real references, never transcribed from here. |
| `iam` | Identities and capabilities the design needs. Never a credential. |
| `assumptions` | What the RAG decided in the absence of instruction, with rationale and supporting rule ids. |
| `warnings` | Non-blocking concerns, each with a severity and a remediation. |
| `unresolved` | Fields the translation could not determine. |
| `references` | Knowledge-base chunks the RAG used, so a decision can be explained. Never raw model reasoning. |
| `unmapped` | Network concepts with no cloud representation. Reported, never dropped silently. |
| `validation` | The `ValidationReport` from `CloudArchitectureValidatorSuite`. |

## Infrastructure resources

Every resource carries `id`, `logical_name`, `provider_type` (a *cloud* type name such as
`aws_vpc`, not a Terraform address), `source_node_ids` back into the network architecture,
`tags`, and `Evidence` provenance.

| Resource | Notable fields |
| --- | --- |
| `VirtualNetwork` | `cidr`, `secondary_cidrs`, DNS flags, `region` |
| `Subnet` | `network_id`, `cidr`, `availability_zone`, `public`, `purpose`, `source_vlan_id` |
| `RouteTable` | `routes[]` (`destination` + `target_ref`), `associated_subnet_ids` |
| `Gateway` | `type` ∈ internet / nat / transit / vpn / peering, `subnet_id`, `peer_network_ids` |
| `ComputeInstance` | `instance_type`, `image`, `interfaces[]`, `roles`, `is_forwarder`, `is_bastion`, `assign_public_ip` |
| `Database` | `engine`, `instance_class`, `subnet_ids`, `credentials_secret_ref` (a reference, never a password) |
| `LoadBalancer` | `type`, `scheme`, `listeners[]`, `target_compute_ids` |
| `SecurityGroup` | `rules[]`, `source_firewall_node_id` |

`ComputeInstance.roles` is the seam between the halves: Terraform tags the instance with
them, and Ansible groups hosts by them.

## Configuration

| Object | Notable fields |
| --- | --- |
| `ConfigurationHost` | `compute_id`, `groups`, `roles`, `connection`, `requires_bastion`, `address_output_ref` |
| `SystemRole` | `packages[]`, `services[]`, `files[]`, `variables`, `requires[]` |

**There are no addresses here.** A host names a compute resource; its real address exists
only after apply and is supplied by `InventoryGenerator` from deployment outputs. A test
asserts that no address-shaped field ever appears on `ConfigurationHost`.

`SystemRole.requires` is deliberately not called `depends_on`: borrowing Terraform's
ordering vocabulary into the cloud schema is the kind of leak this boundary exists to
prevent, and a test greps the serialised document for exactly that.

## What is not in here

No HCL. No YAML. No file paths, module sources, Terraform resource addresses, `depends_on`,
`count`, `for_each`, or provider blocks. `tests/unit/cloud/test_cloud_architecture.py`
serialises a full document and fails on any of: `terraform`, `hcl`, `tfvars`, `ansible`,
`playbook`, `jinja`, `depends_on`.

## Gates

```python
cloud.can_generate                    # valid, and has infrastructure to provision
cloud.requires_configuration_stage    # false for a pure-infrastructure translation
```

The graph consults both: the first before any generator runs, the second to skip the
Ansible node entirely rather than emit an empty playbook.

## Validation

`CloudArchitectureValidatorSuite` runs five independent validators. Errors block
generation; warnings and recommendations do not.

- `SubnetValidator` — subnets inside their VPC, no overlaps, usable size.
- `ComputePlacementValidator` — placement, address-in-subnet, duplicates, AWS's five
  reserved addresses per subnet, and forwarders with source/destination checking left on.
- `RouteTableValidator` — route targets exist, no duplicate destinations, NAT gateways are
  placed, peering names exactly two networks.
- `PublicExposureValidator` — anything open to `0.0.0.0/0`, with sensitive ports named.
- `ConfigurationCoherenceValidator` — roles resolve, bastions exist when hosts need them.
