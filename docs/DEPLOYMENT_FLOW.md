# Deployment flow

```
cloud_architecture.json
   ▼
TerraformGenerator ──┐              ┌── AnsibleGenerator
   ▼                 │              │        ▼
terraform fmt/init/validate         │   syntax-check / lint
   └─────────────────┴──────┬───────┘
                            ▼
                   deployment review
                            ▼
                    terraform plan          ← mutates nothing
                            ▼
                  ⏸  USER APPROVAL  ⏸
                    ╱              ╲
               reject              approve
                  ▼                   ▼
                 END            terraform apply
                                      ▼
                             terraform outputs     ← the only source of real addresses
                                      ▼
                            InventoryGenerator
                                      ▼
                              ansible-playbook
                                      ▼
                                 complete
```

## The approval gate

Infrastructure is never mutated without an `ApprovalRecord`, and an approval is bound to
**one specific plan** by checksum.

```python
DeploymentPlan.checksum()   # hash of the planned changes + the code fingerprint
ApprovalRecord.authorises(plan)
    # is an approval, for this plan id, whose checksum still matches
DeploymentState.may_apply   # a plan, an approval, and the two agree
```

The checksum covers the planned changes *and* the fingerprint of the generated project it
came from, while deliberately excluding the plan id and timestamp. So:

- regenerating identical code and re-planning against unchanged state keeps an approval
  valid — a harmless re-plan does not force a second human decision;
- any real change — a new resource, a changed action, different generated code — makes the
  approval stop matching, and apply is refused.

`InfrastructureExecutor.apply` takes the approval as an argument and raises
`ApprovalRequiredError` when it does not authorise the plan, when applying is disabled by
configuration (`TOPOFORGE_DEPLOYMENT__ALLOW_APPLY=false`), or when the plan has moved.

At the graph level, `route_after_approval` requires both `deployment_approved is True` and
a matching record. A truthy-looking value from a malformed resume rejects rather than
deploys, and `tests/contract/test_pipeline_invariant.py` asserts that `terraform_apply` has
exactly one predecessor: the approval interrupt.

## Process separation

The API process holds **no** AWS credentials and has **no** Terraform binary. Only
`workers/deployment` does. Translation — image or prompt to `cloud_architecture.json`, and
generation of Terraform and Ansible — works with no cloud credentials at all.

`terraform apply -auto-approve` is never emitted against live configuration; apply runs
against a saved plan file that a human already approved.

## Stages shown to the user

`DeploymentState.progress()` renders the canonical order, each stage `pending`, `active`,
`complete` or `failed`:

```
cloud_architecture → terraform_generated → terraform_validated
→ ansible_generated → ansible_validated → plan_ready → awaiting_approval
→ infrastructure_applied → inventory_generated → configuration_applied → complete
```

## Validation before the plan

| Tool | When it runs | When it is missing |
| --- | --- | --- |
| `terraform fmt -check` | always | skipped, recorded |
| `terraform init -backend=false` | always (offline) | skipped, recorded |
| `terraform validate` | always | skipped, recorded |
| TFLint, Checkov | when installed | skipped, recorded |
| project structure (no toolchain) | always | — |
| `ansible-playbook --syntax-check` | always | skipped, recorded |
| `ansible-lint` | when installed | skipped, recorded |

A missing binary is never counted as a pass. Failures return as structured
`GeneratorError` values; they never trigger an automatic model-driven repair, because that
would send generated code back into an LLM.

## Configuration after apply

Ansible runs only after a successful apply, against an inventory built from real outputs.
A host the deployment reported no address for is marked unreachable with a reason rather
than given a guess, so a configuration run fails loudly on a real gap instead of silently
skipping a machine.
