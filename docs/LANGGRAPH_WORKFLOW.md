# LangGraph workflow

LangGraph coordinates. It does not compute. Every node is a thin adapter that calls an
application service through a port and writes typed results into the state.

The graph is declared as data in `src/infrastructure/orchestration/langgraph/spec.py`, and
the builder compiles exactly that specification — so the diagram below, the invariant test
and the running graph cannot drift apart.

## Shape

```
                         detect_input_mode
                                 │
                 ┌───────image────┴────prompt───────┐
                 ▼                                  ▼
          preprocess_image                      prompt_llm
                 ▼                                  ▼
          detect_components              build_topology_from_prompt
                 ▼                                  │
            extract_text                            │
                 ▼                                  │
          detect_connections                        │
                 ▼                                  │
          spatial_association                       │
                 ▼                                  │
       build_topology_from_image                    │
                 └──────────────┬───────────────────┘
                                ▼
                       analyze_completeness
                                │
                  ┌──clarify────┴────enrich──┐
                  ▼                          │
        await_clarification  ⏸ INTERRUPT     │
                  └──────────────┬───────────┘
                                 ▼
                          enrich_network
                                 ▼
                         validate_network
                                 │
              ┌───invalid───┬────┴─────clarify──┐
              ▼             ▼ translate         ▼
           failed   finalise_network_    await_clarification
                       architecture
                            ▼
                    ►  network_architecture.json  ◄
                            ▼
                       rag_translate            ← the RAG ends here
                            ▼
                    ►  cloud_architecture.json  ◄
                            ▼
                 validate_cloud_architecture
                            │
                ┌──invalid──┴──generate──┐
                ▼                        ▼
             failed            generate_terraform
                                         ▼
                                validate_terraform
                                         │
                            ┌──ansible───┴───skip───┐
                            ▼                       │
                    generate_ansible                │
                            ▼                       │
                    validate_ansible                │
                            └──────────┬────────────┘
                                       ▼
                              deployment_review
                                       ▼
                               terraform_plan
                                       ▼
                             await_approval  ⏸ INTERRUPT
                                       │
                       ┌───reject──────┴──────apply───┐
                       ▼                              ▼
                    rejected                  terraform_apply
                       ▼                              ▼
                      END                     collect_outputs
                                                      ▼
                                             generate_inventory
                                                      ▼
                                              run_configuration
                                                      ▼
                                                   complete
```

## Subgraphs

| Subgraph | Nodes |
| --- | --- |
| `image_extraction` | preprocess → detect → OCR → connectors → association → topology |
| `prompt_architecture` | prompt_llm → topology |
| `network_enrichment` | completeness → clarification → enrichment → validation → finalisation |
| `rag_translation` | rag_translate → validate_cloud_architecture |
| `generation` | terraform + ansible generation and validation |
| `deployment` | review → plan → approval → apply → outputs → inventory → configure |

## State

`NetworkWorkflowState` is a `TypedDict`. It carries **references, never bulk**: `image_uri`
rather than image bytes, `terraform_project_uri` rather than file content. A checkpoint is
written on every transition, so it has to stay cheap.

Domain objects are held in their serialised JSON form, which keeps a checkpoint a plain
document that any backend can store.

## Interrupts and resume

Two nodes pause the workflow:

- `await_clarification` — the completeness analyzer found REQUIRED information missing.
- `await_approval` — a deployment plan is waiting for a human decision.

The thread id is `f"{project_id}:{architecture_id}"`, stable across process restarts, so a
workflow resumes on the same thread after a clarification, a crash, or an approval that
arrives days later.

On resume, `route_after_completeness` subtracts already-answered question ids, so a user is
never asked the same thing twice. A *skipped* answer counts as answered — declining is a
decision.

## Routing

Every branch is a pure function in `routing.py`, unit-tested without running the graph.
The two that carry the most weight:

**`route_after_network_validation`** — the single gate into the RAG. Errors block.
Warnings do not. Unanswered REQUIRED gaps go back to clarification rather than through.

**`route_after_approval`** — the last gate before real infrastructure. It requires both
`deployment_approved is True` *and* a matching approval record. Anything else — a
rejection, a missing record, a truthy-looking `"true"` or `1` from a malformed resume —
routes to `rejected`. Defaulting to apply would let an interrupted resume deploy.

## Checkpointing

| Environment | Backend |
| --- | --- |
| tests | `MemorySaver` |
| development | `SqliteSaver` (`TOPOFORGE_ORCHESTRATION__SQLITE_PATH`) |
| production | `PostgresSaver` |

Selected by configuration. Checkpoints are an orchestration concern and may be pruned;
architecture **revision history** is a separate, durable business record in PostgreSQL.

## What LangGraph does not do

It does not contain business logic. YOLO, OCR, connector detection, spatial association,
topology building, validation, the RAG, the generators and the executors are all
independent components behind ports. LangGraph only decides what runs next.
