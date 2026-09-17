# Current System Analysis

**Date:** 2026-09-17
**Analyst:** TopoForge-AI engineering (Phase 0 — Discovery)
**Scope:** Inventory of everything that existed before implementation started, what can be
reused, and what must be built.

This document records *observed facts only*. Where an asset was expected but not found, that
is stated explicitly rather than assumed.

---

## 1. Repository state at discovery

`/home/themangahacker/Projects/TopoForge-AI` contained exactly one tracked file:

```
README.md          # single line: "# TopoForge-AI"
```

Git history: one commit (`757d2de first commit`). No source code, no dependencies, no CI,
no configuration.

### 1.1 Asset discovery outcome

| Expected asset | Found | Location |
| --- | --- | --- |
| Existing RAG implementation | **Yes** | `/home/themangahacker/Projects/Advanced-RAG-For-Net_To_Cloud-Translation` (sibling repository, *not* inside this repository) |
| YOLO model weights (`.pt`) | **No** | A filesystem search across `~/Projects` and `~` (excluding virtualenvs / site-packages) returned **zero** `.pt` files and zero files or directories matching `*yolo*`. |

**Consequence for YOLO.** The trained classes are unknown and will not be guessed. Section 8
of the build specification forbids fabricating class names. The system therefore:

- defines the `ObjectDetector` port and a `YOLOObjectDetector` adapter that reads the class
  map **from the weights file at load time** (`model.names`), never from a hard-coded list;
- ships a `NullObjectDetector` and a fixture-backed `StubObjectDetector` so every layer above
  vision is buildable and testable today;
- fails loudly with `ModelLoadError` when `TOPOFORGE_YOLO_WEIGHTS_PATH` points at nothing.

The integration point is marked in code with `# INTEGRATION POINT: YOLO WEIGHTS` and in
[`docs/VISION_PIPELINE.md`](VISION_PIPELINE.md).

---

## 2. The existing RAG: `Advanced-RAG-For-Net_To_Cloud-Translation` (internally `net2tf_v3`)

A flat, single-package Python project. 5,616 lines across 21 modules, plus a 28-document
Markdown knowledge base, 5 Jinja2 Terraform templates, and an evaluation set.

### 2.1 Declared dependencies (`requirements.txt`)

```
groq                    # LLM provider — Groq cloud, llama-3.3-70b-versatile
pydantic                # architecture schema
jinja2                  # Terraform + Ansible rendering
sentence-transformers   # embeddings + cross-encoder reranking
transformers
faiss-cpu               # vector index (IndexFlatIP, cosine on normalized vectors)
numpy
torch
scikit-learn
ansible                 # post-deploy configuration layer
```

No `langchain`, no `langgraph`, no managed vector database, no OpenAI/Anthropic. The LLM
provider is **Groq**, not OpenAI or Anthropic.

### 2.2 Module inventory

| Module | LOC | Responsibility |
| --- | --- | --- |
| `app.py` | 386 | Orchestrator. `compile_prompt(prompt, out_dir) -> dict` is the single public entrypoint. Also `compile_intake_session`, `generate_ansible_config`, and an `argparse` CLI (`generate`, `generate-ansible`). |
| `config.py` | 21 | Hard-coded paths rooted at `/kaggle/working/net2tf_v3`; model ids; `TOP_K=6`; `MAX_CHARS_PER_CHUNK=1800`. |
| `models.py` | 82 | Pydantic v2 schema: `Architecture`, `Component`, `Edge`, `Addressing`, `FirewallPolicy`, `UserPolicies`, `DomainPlan`, `RouterDomain`, `RouterSubnet`, `HostPlacement`. |
| `extractor.py` | 284 | Groq LLM: free text → `Architecture`, with heavy deterministic normalization and JSON-from-text salvage. |
| `addressing.py` | 809 | **Deterministic compiler.** Manual-CIDR regex parsing, VPC/subnet allocation, logical-VPC inference, host placement (public / bastion / NAT), router-link derivation → `DomainPlan`. |
| `retriever.py` | 527 | FAISS + `all-MiniLM-L6-v2` embeddings + `ms-marco-MiniLM-L-6-v2` cross-encoder, query expansion, and a large hand-tuned `_metadata_boost` re-ranking layer. |
| `planner.py` | 236 | Groq LLM: prompt + extracted architecture + retrieved chunks → advisory JSON plan (`deployment_pattern`, `connectivity_mode`, `nat_required`, `bastion_required`, assumptions…). |
| `validator.py` | 132 | Structural + manual-addressing validation; returns `List[str]` (empty = valid). |
| `terraform_builder.py` | 169 | Jinja2 `StrictUndefined` rendering of `main.tf`, `variables.tf`, `outputs.tf`, `terraform.tfvars.example`, `README.md`. |
| `plan_guard.py` | 139 | Compares the LLM advisory plan against the deterministic compilation; reports divergence. |
| `spec_guard.py` | 371 | Checks the result against the response specification. |
| `quality_checks.py` | 48 | Shells out to `terraform fmt/init/validate` in the generated directory. |
| `response_renderer.py` | 94 | Builds the user-facing structured response (sections, SSH plan, outputs, assumptions). |
| `deploy_check.py` | 531 | Terraform lifecycle driver: `init`, `fmt`, `validate`, `plan`, `show`, **`apply`**, `output -json`, `destroy`; plus Ansible playbook execution. Paths hard-coded to `/kaggle/working/net2tf_v3`. |
| `interactive_intake.py` | 637 | Deterministic staged questionnaire (`collect_components` → … → `ready_to_compile`) with regex parsers. |
| `intake_models.py` | 68 | `IntakeSession`, `IntakeDecision`, stage enum. |
| `ansible_planner.py` | 439 | Groq LLM + heuristic fallback → Ansible task plan. |
| `ansible_builder.py` | 298 | Renders inventory + playbooks from architecture and Terraform outputs. |
| `ansible_check.py` | 37 | `ansible-playbook --syntax-check`. |
| `pack_project.py` | 246 | Packages the project into a distributable zip. |
| `tools/validate_knowledge_base.py` | — | Corpus structure validation + manifest refresh. |

### 2.3 Actual pipeline (`compile_prompt`)

```
prompt text
  → extract_architecture(prompt, groq)            # LLM  → Architecture
  → enrich_with_manual_addressing(arch, prompt)   # regex over the PROMPT TEXT
  → _apply_firewall_default(arch, prompt)
  → retrieve_context(prompt)                      # FAISS + rerank + metadata boost
  → plan_with_rag(prompt, arch_dict, chunks, groq)# LLM  → advisory plan
  → validate_architecture(arch)                   # deterministic; blocks on failure
  → build_domain_plan(arch, prompt)               # deterministic compiler → DomainPlan
  → render_project(arch, templates_dir, out_dir)  # Jinja2 → Terraform files
  → compare_plan_to_compiled(...)                 # plan_guard
  → run_quality_checks(out_dir)                   # terraform fmt/init/validate
  → evaluate_spec_compliance(result)              # spec_guard
  → build_rendered_response(result)
  → writes generated/last_result.json
```

Result dict keys: `status`, `architecture`, `rag_plan`, `retrieved_context`, `validation`,
`plan_guard`, `quality` / `quality_checks`, `generated_files`, `spec_guard`,
`rendered_response`.

### 2.4 Knowledge base

`kb/` — 16 rule documents (`mapping_rules.md`, `addressing_rules.md`, `aws_network_patterns.md`,
`routing_ospfv2.md`, `routing_static.md`, `routing_ripv2.md`, `security_patterns.md`,
`layer2_patterns.md`, `terraform_patterns.md`, `verification_rules.md`, `access_patterns.md`,
`internet_patterns.md`, `input_contract.md`, `translation_contract.md`,
`real_router_appliance_rules.md`, `routing_rules.md`) plus 14 worked examples under
`kb/examples/`. `kb/manifest.json` pins `corpus_version: 2.0.0`, per-file SHA-256 hashes, and
`mandatory_context_ids: [CORE-001, CORE-002, CORE-003]`.

The index is a FAISS `IndexFlatIP` persisted as `index/kb.index` + a pickled `kb_chunks.pkl`.

### 2.5 Observations that constrain integration

These are recorded because they change how the adapter must be written. They are **not**
reasons to rewrite the RAG.

1. **The RAG's public entrypoint takes prose, not JSON.** `compile_prompt(prompt: str)`.
2. **Deterministic stages also read the prose.** `enrich_with_manual_addressing(arch, user_text)`
   and `build_domain_plan(arch, user_text)` regex the *original prompt* for base CIDRs,
   `SW1 = 10.0.1.0/27` bindings, bastion/public/NAT host intent, and "automatic addressing is
   allowed". Architecture data alone is therefore **not** sufficient input to the existing
   compiler — a faithful textual descriptor must accompany it.
3. **`config.py` paths are hard-coded to `/kaggle/working/net2tf_v3`** and will not resolve on
   any other machine. The project's own `docs/IMPLEMENTATION_GAPS.md` flags this.
4. **Flat top-level module names** (`config`, `models`, `validator`, `planner`, `extractor`)
   collide trivially with any host application. Importing requires a guarded `sys.path` entry.
5. **`torch` + `faiss` + `sentence-transformers` are heavyweight**, GPU-aware, and loaded at
   module import. They must not be imported inside a FastAPI request path.
6. **`app.py` calls `render_ansible_project(...)` with keyword arguments it does not accept**
   (documented in the project's own gap report). The Ansible path is therefore treated as
   **known-broken** and is wrapped but not exercised by default.
7. **`deploy_check.py` can run `terraform apply`.** It must never be reachable without an
   explicit, recorded human approval.
8. **The schema is AWS/VPC-shaped and lossy** relative to what this platform extracts: it has
   no interfaces, no per-interface addressing, no VLANs, no OSPF/static-route objects, no
   provenance. The project's own gap report states this first.
9. `models.Component.type` is restricted to `router | switch | server | pc | firewall`. Any
   canonical node type outside that set must be explicitly mapped or reported as unmapped.

### 2.6 Self-declared maturity

`README.md` and `docs/IMPLEMENTATION_GAPS.md` state plainly that the v2 knowledge base is a
**target specification**, that the compiler's routing decisions are hardcoded, and that the
corpus does not yet drive behavior. The retriever's `_metadata_boost` encodes routing policy
(peering vs. TGW by router count) in Python, not in the KB.

**Integration stance:** treat the RAG as a *black box with a known interface*. Wrap it. Do not
fix it from the outside. Its gaps are its own roadmap.

---

## 3. What is reusable as-is

| Asset | Reuse |
| --- | --- |
| `compile_prompt` pipeline | Wrapped wholesale behind `RAGProvider`. |
| `retrieve_context` | Reused directly for the retrieval view in the UI. |
| `addressing.py` compiler | Reused via `compile_prompt`; never reimplemented. |
| Jinja2 Terraform templates | Reused; the generated `.tf` files are our deployment artifacts. |
| `kb/` corpus + manifest | Reused unchanged; mounted read-only. |
| `deploy_check.py` terraform verbs | Reused as the reference for our own `TerraformDeploymentProvider`, gated behind approval. |
| `evaluations/rag_cases.jsonl` | Reused as contract-test fixtures. |

## 4. What must be built from zero

Everything else. There is no web app, no API, no orchestration, no persistence, no vision
pipeline, no canonical schema, no job system, no tests, no containers, no CI.

## 5. Technical debt inherited

| Debt | Severity | Mitigation in this platform |
| --- | --- | --- |
| Kaggle-absolute paths in `config.py` / `deploy_check.py` / `pack_project.py` | High | Adapter injects paths via environment before import; never edits the file. |
| Flat module namespace | High | Isolated, cached, lock-guarded `sys.path` import shim. |
| Prose-driven deterministic stages | High | Deterministic (non-LLM) `ArchitectureDescriptorRenderer` emits the exact textual form the compiler parses. |
| Broken Ansible keyword arguments | Medium | Ansible generation disabled by default behind a feature flag. |
| `status: "ok"` returned with failed quality checks inside | Medium | Adapter re-derives status from `quality`/`spec_guard`, never trusts the top-level field alone. |
| Unhashed index cache | Medium | Adapter fingerprints `kb/` and forces a rebuild on drift. |
| Groq-only LLM | Low | Out of scope for the RAG; our own `LLMProvider` port is independent. |
| Lossy AWS-shaped schema | High (product) | Canonical `architecture.json` is the source of truth; loss at the boundary is measured and reported as `unmapped` fields, not hidden. |

## 6. Integration risks

1. **Fidelity loss at the boundary (highest).** Canonical architecture is richer than the RAG's
   `Architecture`. Mitigation: the adapter emits an explicit `unmapped[]` report on every call,
   surfaced in the UI RAG tab. Nothing is dropped silently.
2. **Non-determinism.** Two Groq LLM calls sit inside the RAG. Mitigation: full request and
   response persisted per revision; the RAG result is attached to an immutable architecture
   revision.
3. **Cold-start latency.** First call builds the FAISS index and downloads two transformer
   models. Mitigation: RAG runs only in a dedicated worker; index prebuilt in the image.
4. **Dependency conflict.** `torch`/`faiss` vs. `ultralytics`/`opencv`. Mitigation: separate
   optional dependency groups and separate worker images (`worker-vision`, `worker-rag`).
5. **Uncontrolled `terraform apply`.** Mitigation: the API process never has Terraform or AWS
   credentials; only the deployment worker does, and only after a persisted approval record.
6. **Upstream drift.** The RAG is a separate git repository. Mitigation: `external/rag/` is a
   pinned, hash-recorded vendoring target with a documented sync script; the adapter asserts
   the presence of the symbols it calls at startup and raises `RAGError` otherwise.

## 7. Missing components (to build)

Canonical domain model · provenance model · validators · image preprocessing · YOLO adapter ·
OCR adapter · network text parser · connector detector · spatial association · topology builder ·
completeness analyzer · enrichment · LLM provider abstraction · prompt-mode architecture
generation · LangGraph orchestration + checkpointing · RAG adapter · deployment provider ·
FastAPI application · job/queue abstraction · SSE streaming · PostgreSQL persistence · object
storage · model registry · Next.js workspace · Docker · CI · observability · test suite.

## 8. Environment observed

Python 3.14.6 · Node 24.19 · npm 11.16 · git 2.53 · 16 cores · 15 GB RAM · **no** Docker,
**no** Terraform, **no** CUDA, **no** `uv`. Present system-wide: pydantic 2.12, fastapi 0.135,
sqlalchemy 2.0, networkx 3.4, numpy 2.3, httpx 0.28, jinja2 3.1, pytest 9.0. Absent: langgraph,
torch, faiss, ultralytics, opencv, paddleocr, groq, structlog.

**Design consequence:** every heavy dependency is imported lazily inside its adapter. The
domain, application, and orchestration-routing layers import none of them, so the unit suite
runs on a bare interpreter.

---

## 9. Addendum — the RAG was restructured mid-build (2026-09-17)

Re-inspection during Phase 11 found the external RAG substantially rewritten. The analysis
in §2 above describes the **archived** implementation, which now lives under `legacy/` in
that repository and is explicitly marked "unused by the active API". The findings there are
retained because they explain what the archived code did and why it was not carried over;
they no longer describe the integration target.

### 9.1 The active RAG

```
architecture JSON  →  retrieval  →  one model call  →  plan JSON
```

Public API — `app.plan_architecture(architecture: dict, *, client=None, retriever=None) -> dict`.

| Module | Role |
| --- | --- |
| `app.py` (72 lines) | Python API + `plan` / `context` CLI. Deep-copies the input, retrieves, plans, and re-attaches the untouched architecture and the knowledge citations. |
| `retriever.py` (340 lines) | Hybrid retrieval: BM25 + embeddings + cross-encoder, content-aware caching, core rules always pinned. A `lexical` backend runs BM25 only, with no torch/faiss. |
| `planner.py` (126 lines) | One Groq call, `temperature=0`, JSON object mode, with an explicit boundary prompt. Raises on truncation and on non-object output. |
| `config.py` (17 lines) | All paths and model names configurable through `NET2TF_*` environment variables. The Kaggle-absolute paths are gone. |
| `legacy/` | The archived original: intake, validators, `terraform_builder.py`, `templates/*.j2`, `ansible_planner.py`, `ansible_builder.py`, `deploy_check.py`. |

### 9.2 What this changes for the integration

The upstream project has independently arrived at the same boundary this refactor defines.
Its own prompt now states: *"Field validation, clarification, dialogue, code generation,
execution and testing belong to other applications. Do none of these. Return only a JSON
plan."* It also refuses to invent routes, bastions or public access, and forbids inferring
peering or Transit Gateway from device counts — the exact behaviour the archived
implementation hard-coded and its gap report criticised.

Consequences for the adapter:

1. **The prose descriptor is unnecessary and has been dropped.** The RAG now consumes JSON
   directly, so `NetworkArchitecture` maps to its input shape structurally. The risk
   recorded in §2.5(2) and §5 — that a deterministic compiler parsed the original prompt
   text — no longer exists.
2. **Input is JSON with a documented convention** (`docs/JSON_CONTRACT.md`): `components[]`
   with interfaces / routing / services, interface-level `edges[]`, `cloud`,
   `translation_mode`, and `ansible.{connections,tasks}`. It is far richer than the archived
   `Architecture` model — it carries interfaces, per-interface addressing, OSPF areas,
   router ids, static routes, VLANs and services — so the fidelity loss recorded as the
   highest integration risk in §6 is largely gone.
3. **Output is a plan, not files**: `cloud_plan` (component mapping, networking, security,
   dependencies), `ansible_plan` (targets, tasks as structured intentions), `rule_ids`,
   `limitations`, plus the architecture and knowledge citations re-attached deterministically.
   *"Tasks describe intentions; they contain no executable YAML."*
4. **The RAG performs no validation.** `plan_architecture` checks only that input and output
   are JSON objects. Validation is explicitly caller-owned — which is this platform's
   `network_validator` before, and `CloudArchitectureValidator` after.
5. **Paths are configurable**, so the adapter no longer needs to patch module constants.
6. **`legacy/` must never be imported.** The adapter targets the active API only.

### 9.3 Revised integration risks

| Risk | Mitigation |
| --- | --- |
| The plan is free-form JSON: the model may omit fields or return prose where a structure was expected. | `CloudArchitectureAssembler` is defensive and deterministic. It types what it can, records what it cannot in `unmapped[]`, and never treats an absent field as a permission. |
| `cloud_representation` is a model-authored string. | Interpreted through an explicit, tested lookup table. An unrecognised value becomes a warning, not a guess. |
| One Groq call with no schema enforcement upstream. | The adapter validates the plan's *shape* before assembly and raises `RAGError` rather than passing a malformed plan onward. |
| Upstream is under active development. | The loader asserts the symbols it uses at import time and reports the checkout's git revision in `producers`, so a drift shows up in the diagnostics rather than as a mystery failure. |
