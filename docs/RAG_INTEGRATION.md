# RAG integration

```
network_architecture.json  →  RAGProvider.translate()  →  cloud_architecture.json
```

That is the entire contract. The RAG receives a validated network architecture, retrieves
cloud knowledge, reasons, and returns a typed cloud architecture. It writes no files, no
HCL, no YAML and no shell, and generated code is never sent back into it.

## The upstream project

`Advanced-RAG-For-Net_To_Cloud-Translation` (internally `net2tf_v3`). It was restructured
during this build; see `docs/CURRENT_SYSTEM_ANALYSIS.md` §9 for the before and after.

Its active API:

```python
from app import plan_architecture
plan = plan_architecture(architecture_dict, retriever=KnowledgeRetriever(backend=..., top_k=...))
# -> {"cloud_plan", "ansible_plan", "rule_ids", "limitations", "architecture", "knowledge"}
```

Its own planner prompt now states the same boundary this platform enforces: *"Field
validation, clarification, dialogue, code generation, execution and testing belong to other
applications. Do none of these. Return only a JSON plan."*

`legacy/` holds the archived original, including its Terraform templates and Ansible
builders. It is never imported. `tests/unit/test_layering.py` fails the build if any RAG
module imports a generator, or any generator imports the RAG.

## The adapter

```
src/infrastructure/rag/
├── loader.py           guarded import of the external checkout
├── network_mapper.py   NetworkArchitecture → the RAG's input JSON
├── cloud_assembler.py  the RAG's plan → typed CloudArchitecture
├── provider.py         Net2TFRAGProvider
└── stub.py             StubRAGProvider (offline double)
```

### Loading

The RAG uses flat module names (`app`, `config`, `planner`, `retriever`) that would collide
with almost any host application. The loader imports it under a lock, with its directory on
`sys.path` only for the duration of the import, then unregisters those names — the module
objects stay alive through their own references, so the RAG keeps working while `config`
means nothing in this process again.

Nothing in the checkout is modified. It is configured through the `NET2TF_*` variables it
reads itself. The symbols the adapter calls are asserted at load time, and the checkout's
git revision is recorded in `producers`, so upstream drift surfaces as a clear error rather
than an `AttributeError` inside a workflow.

### Mapping in

`NetworkArchitectureMapper` builds the RAG's documented input shape: `components[]` with
interfaces, routing and services; interface-level `edges[]`; `cloud`; `translation_mode`;
`ansible`.

Two rules govern it:

- **Absence is information.** An interface with an address but no prefix is emitted without
  `ipv4` rather than with a guessed mask. An empty `protocols: []` is sent when the user
  chose no routing, because the RAG's contract distinguishes `[]` from omitted.
- **Provenance is withheld.** Extraction confidence is a clarification-stage concern that
  has already been resolved; sending it would only invite the model to reason about it.

Anything the target shape cannot express is recorded in `unmapped` and surfaced in the UI.

### Assembling out

`CloudArchitectureAssembler` types the free-form plan. The division is deliberate:

| | |
| --- | --- |
| **The RAG decides** | what each component becomes, whether egress is required, the deployment pattern |
| **The architecture supplies** | CIDRs, interface addresses, VLAN ids, attachment — exact, already validated |
| **The assembler invents** | nothing |

Concretely: a gateway exists only because the plan named one; an unrecognised
`cloud_representation` is recorded in `unmapped` rather than guessed at; a malformed plan
raises `RAGError` instead of assembling something plausible.

Two modelling rules it enforces, both found by running it:

- A switch is attached to a segment by matching the **far interface** of its links, not the
  far node. A router sits on several segments at once, so "this switch touches R1" would
  wrongly place a LAN switch on R1's transit link.
- A router-to-router link spanning two routed domains is **transport, not a subnet**. An
  EC2 instance cannot hold a NIC in another VPC. Missing transport is reported as a
  high-severity warning; peering is not invented to paper over it, because choosing peering
  over a transit gateway is a knowledge-base decision belonging to the RAG.

### Running without it

`TOPOFORGE_RAG__BACKEND=stub` selects `StubRAGProvider`: deterministic, offline, no Groq
key and no embedding model. It is a **test and development double**, not a second pipeline
— it implements the same port, is selected only by explicit configuration, and its own
output says plainly that no knowledge base was consulted.

## Failure handling

| Failure | Result |
| --- | --- |
| Checkout missing or incomplete | `RAGUnavailableError` at load |
| Transport failure, invalid JSON, truncated model output | `RAGError` — the boundary translates every upstream exception |
| Timeout | `RAGError` with the configured limit |
| Plan missing `cloud_plan` | `RAGError` — an incomplete plan is never assembled |
| Architecture not validated | `ValidationError` before the RAG is called at all |

The RAG runs on a worker thread: it is synchronous and CPU/network bound, and blocking the
event loop with it would stall every other request in the process.
