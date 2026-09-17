# TopoForge-AI

Turns a network architecture — a diagram or a description — into deployable cloud
infrastructure, with a human approval gate before anything is created.

```
IMAGE ──► YOLO + OCR + edge detection ──► topology ─┐
                                                    │
PROMPT ─► LLM ─────────────────────────► topology ──┤
                                                    ▼
                                    completeness · enrichment · validation
                                                    ▼
                                    ►  network_architecture.json  ◄
                                                    ▼
                                            R A G  (translation)
                                                    ▼
                                    ►   cloud_architecture.json   ◄
                                                    │
                                    ┌───────────────┴──────────────┐
                                    ▼                              ▼
                            TerraformGenerator             AnsibleGenerator
                              (deterministic)               (deterministic)
                                    ▼                              ▼
                              validate · plan            syntax check · lint
                                    └──────────┬───────────────────┘
                                               ▼
                                        USER APPROVAL
                                               ▼
                                     apply → outputs → inventory → configure
```

## The two rules

1. **Everything converges on `network_architecture.json`.** Both input modes produce the
   same canonical, validated document, and it is the only thing the RAG ever receives.
2. **The RAG produces `cloud_architecture.json` and then stops.** It writes no HCL, no
   YAML and no shell. Terraform and Ansible come from deterministic generators that never
   call a model, and generated code is never sent back into one.

Neither rule is a convention. `tests/contract/test_pipeline_invariant.py` computes graph
bottlenecks to prove the RAG cannot be bypassed and apply cannot be reached without the
approval interrupt, and `tests/unit/test_layering.py` walks the AST of every module to
prove no generator can import a model or the RAG.

## Status

| Phase | State |
| --- | --- |
| 0 Discovery | done |
| 1 Domain foundation | done — network, topology, routing, architecture, cloud, generation, deployment |
| 2 Ports | done |
| 8 Validation | done — network gate and cloud gate |
| 10 Orchestration | graph specification, state and routing done; node bodies pending |
| 11 RAG integration | done — adapter, mapper, assembler, offline stub |
| 11b Generators | done — Terraform, Ansible, inventory, validators |
| 3–7 Vision pipeline | ports defined; adapters pending |
| 9 Prompt mode | port defined; adapter pending |
| 12 Backend API | pending |
| 13 Frontend | pending |
| 14 Deployment executors | ports and approval gate done; executors pending |
| 15 Docker, CI | pending |

510 tests pass on a bare interpreter — no Groq key, no AWS credentials, no GPU.

## Run the tests

```bash
python -m pytest -q
```

Heavy dependencies (torch, faiss, ultralytics, opencv) are optional and lazily imported, so
the domain, generation and orchestration suites run without them.

## Configure

```bash
cp .env.example .env
```

Everything is typed configuration under the `TOPOFORGE_` prefix. Two integration points
are marked explicitly:

- **YOLO weights** — `TOPOFORGE_VISION__YOLO_WEIGHTS_PATH`. No trained class names are
  assumed anywhere; the detector reads them from the weights at load time.
- **The RAG** — `TOPOFORGE_RAG__PATH`, or `scripts/sync_external_rag.sh` to vendor a
  pinned copy into `external/rag/`. `TOPOFORGE_RAG__BACKEND=stub` runs offline.

## Documentation

| | |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | target architecture and the layering rule |
| [CURRENT_SYSTEM_ANALYSIS.md](docs/CURRENT_SYSTEM_ANALYSIS.md) | what existed at discovery, and how the RAG changed mid-build |
| [REFACTOR_RAG_BOUNDARY.md](docs/REFACTOR_RAG_BOUNDARY.md) | why IaC generation moved out of the RAG |
| [ARCHITECTURE_JSON_SCHEMA.md](docs/ARCHITECTURE_JSON_SCHEMA.md) | the network contract |
| [CLOUD_ARCHITECTURE_SCHEMA.md](docs/CLOUD_ARCHITECTURE_SCHEMA.md) | the cloud contract |
| [RAG_INTEGRATION.md](docs/RAG_INTEGRATION.md) | how the external RAG is wrapped |
| [TERRAFORM_GENERATOR.md](docs/TERRAFORM_GENERATOR.md) · [ANSIBLE_GENERATOR.md](docs/ANSIBLE_GENERATOR.md) | the deterministic generators |
| [LANGGRAPH_WORKFLOW.md](docs/LANGGRAPH_WORKFLOW.md) | orchestration, interrupts, resume |
| [DEPLOYMENT_FLOW.md](docs/DEPLOYMENT_FLOW.md) | validation, plan, approval, apply |
| [IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | phase-by-phase file plan |

## Layout

```
src/domain/          pure: network, topology, routing, architecture, cloud, generation, deployment
src/application/     use cases and ports — the interfaces everything else implements
src/infrastructure/  adapters: rag, generators, orchestration, vision, llm, persistence, storage
src/shared/          config, logging, telemetry, errors
apps/                api (FastAPI) and web (Next.js)
workers/             vision, rag, deployment
tests/               unit · integration · contract · e2e
```

The dependency rule points inward. `src/domain` imports no framework and no vendor SDK,
which is why the whole domain suite runs on a bare interpreter — and a test enforces it.
