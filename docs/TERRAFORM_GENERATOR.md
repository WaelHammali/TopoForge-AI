# Terraform generator

```
cloud_architecture.json  →  AWSTerraformGenerator  →  TerraformProject  →  validation
```

Deterministic. No LLM, no RAG, no network access, no clock in the output. Two runs over
the same input produce byte-identical files — a test asserts it, and another monkeypatches
`socket.socket` to prove nothing reaches out during generation.

## Layout

```
src/infrastructure/generators/terraform/
├── hcl.py            a small HCL2 writer
├── mappings/aws.py   THE mapping table: cloud resource → Terraform resource
├── generator.py      ordering, file layout, variables, outputs, providers
└── validator.py      fmt / init / validate, plus TFLint and Checkov when present
```

## Why a writer rather than templates

Quoting, escaping and block ordering are handled in one tested place instead of being
repeated in every template. In particular `${` in any string is escaped to `$${`, so a
device label read off a diagram can never become a Terraform interpolation.

## The mapping layer

All mapping decisions live in `mappings/aws.py` and nowhere else. Adding a resource type
means adding a mapper function and registering it; the generator does not change.

| Cloud resource | Terraform |
| --- | --- |
| `VirtualNetwork` | `aws_vpc` (+ `aws_vpc_ipv4_cidr_block_association` for secondaries) |
| `Subnet` | `aws_subnet` |
| `RouteTable` | `aws_route_table` + `aws_route_table_association` |
| `Gateway(internet)` | `aws_internet_gateway` |
| `Gateway(nat)` | `aws_eip` + `aws_nat_gateway` |
| `Gateway(peering)` | `aws_vpc_peering_connection` |
| `Gateway(transit)` | `aws_ec2_transit_gateway` + attachments |
| `SecurityGroup` | `aws_security_group` with inline ingress/egress |
| `ComputeInstance` | `aws_instance` (+ `aws_network_interface` per secondary NIC) |
| `Database` | `aws_db_subnet_group` + `aws_db_instance` |
| `LoadBalancer` | `aws_lb` + target group + listener + attachments |

Addresses are indexed in a pre-pass, so a route table can reference a gateway that has not
been rendered yet.

## Decisions worth knowing

- **Routes pick the argument that matches their target**: `gateway_id` for an internet
  gateway, `nat_gateway_id` for NAT, `vpc_peering_connection_id` for peering. Getting this
  wrong produces Terraform that plans and then fails.
- **Router appliances set `source_dest_check = false`**, or AWS drops the transit traffic
  that makes them routers.
- **Availability zones come from `data.aws_availability_zones`**, indexed by the subnet's
  position within its own VPC. No hard-coded AZ names, and adding a VPC never reshuffles
  an existing one's assignment.
- **Instance sizing comes from the cloud architecture**, not from a generator heuristic.
  The archived upstream implementation sized routers by interface count and its own gap
  report called that invalid; that heuristic is not re-inherited here.
- **Database credentials are `sensitive` variables**, never literals, so they stay out of
  plan and apply output.
- **A security rule naming neither a CIDR nor a peer group is skipped with a warning**,
  never defaulted to `0.0.0.0/0`.

## Output

```
providers.tf   required_version, pinned aws provider, default tags
data.tf        availability zones, default AMI
main.tf        the resources, in dependency-readable order
variables.tf   aws_region plus every required variable, sensitive where appropriate
outputs.tf     ids and addresses; the inventory generator consumes these after apply
terraform.tfvars.example
README.md
```

Every resource derived from a diagram carries a `# From network component(s): R1` comment
and a `topoforge:source-nodes` tag, so provenance is readable from the AWS console.

## Validation

`TerraformProjectValidator` materialises the project into a temporary directory and runs
`fmt -check`, `init -backend=false` (offline: no remote state, no credentials) and
`validate`, then TFLint and Checkov when installed.

A missing binary produces a **skipped** check, never a passing one. Failures come back as
structured `GeneratorError` values and never trigger an automatic model-driven repair:
generated code does not go back into an LLM.

## Adding another generator

`CodeGenerator` is a port. A `CloudFormationGenerator` or `PulumiGenerator` is a new
implementation consuming the same `cloud_architecture.json`; nothing upstream changes and
the RAG is untouched.
