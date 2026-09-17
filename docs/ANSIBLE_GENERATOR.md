# Ansible generator

```
cloud_architecture.json  →  DefaultAnsibleGenerator  →  AnsibleProject  →  syntax check
                                                                 ↓
                         (after terraform apply)  outputs → InventoryGenerator → inventory
```

Deterministic. No LLM, no RAG.

## Ownership

Ansible **configures**; it never provisions. Nothing it generates creates, sizes or
networks a machine — that is Terraform's half of `cloud_architecture.json`. A test fails
the build if an instance type, a CIDR, or a cloud provisioning module (`amazon.aws.*`,
`community.aws`) appears anywhere in the generated project.

## Output

```
ansible.cfg
site.yml                  one play per inventory group
inventory/hosts.yml       PLACEHOLDER: no addresses (see below)
group_vars/all.yml        global variables
group_vars/<group>.yml
host_vars/<host>.yml
roles/<role>/tasks/main.yml
roles/<role>/handlers/main.yml
roles/<role>/defaults/main.yml
roles/<role>/meta/main.yml
roles/<role>/files/...
README.md
```

## Mapping

| Cloud configuration | Ansible |
| --- | --- |
| `ConfigurationHost.groups` | inventory groups, one play each |
| `ConfigurationHost.roles` | roles applied in that play |
| `SystemRole.packages` | one `ansible.builtin.package` task |
| `SystemRole.services` | `ansible.builtin.service` task + a `restart <service>` handler |
| `SystemRole.files` | `copy` (literal content) or `template`, with `notify` |
| `SystemRole.requires` | `meta/main.yml` dependencies, and play ordering |
| `SystemRole.variables` | `defaults/main.yml` |

Roles are ordered by a stable topological sort over `requires`. A cycle is broken by
declaration order rather than raising, so one bad dependency cannot block a whole
generation; the cycle still shows up as unresolved role warnings.

`ansible.builtin.package` is used rather than a distribution-specific module, so a role
works across the images this platform may provision.

## The inventory rule

**A generated inventory has no addresses.** Host addresses do not exist until
`terraform apply` has run, so the placeholder inventory lists the groups and hosts, marks
every host `topoforge_address_pending: true`, and says so in its own header:

```yaml
# PLACEHOLDER INVENTORY: it has no addresses.
# Host addresses are only known after `terraform apply`. TopoForge replaces
# this file from the deployment outputs before any playbook runs.
```

`AnsibleProject.inventory_pending` carries the same fact to the UI, which shows it in the
Ansible tab rather than letting an operator discover it as a connection timeout.

After apply, `TerraformOutputInventoryGenerator` builds the real inventory from
`DeploymentOutputs`:

```yaml
all:
  children:
    web:
      hosts:
        app1:
          topoforge_compute_id: c-app1
          ansible_host: 10.0.2.15
          ansible_connection: ssh
          ansible_user: ec2-user
          ansible_ssh_common_args: "-o ProxyCommand=\"ssh -W %h:%p -q ec2-user@52.1.2.3\""
```

A host the deployment reported no address for is written with
`topoforge_unreachable_reason` and named in the file's header — never given a
plausible-looking guess. Generating an inventory before apply raises `DeploymentError`.

## The YAML writer

Written rather than delegated to PyYAML, which re-sorts keys and chooses quoting
heuristically — neither stable enough to golden-test. It handles the traps that matter for
Ansible specifically:

- YAML 1.1 booleans: unquoted `yes` is `True`, so `"yes"` is quoted.
- Jinja expressions: `{{ x }}` parses as a flow mapping unless quoted.
- Numeric-looking strings: `"123"` stays a string.
- Human phrases stay unquoted (`name: Install nginx`) unless they contain `: ` or a
  leading indicator character.

## Validation

`AnsibleProjectValidator` runs a toolchain-free **structure check** first, so CI always
validates something: the declared playbook exists, every task file has named tasks, every
role with tasks has a `meta/main.yml`. Then `ansible-playbook --syntax-check` and
`ansible-lint` when installed — and a missing binary is a skipped check, never a pass.
