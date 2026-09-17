# `network_architecture.json`

The first canonical contract, and the only thing the RAG ever receives. Defined in
`src/domain/architecture/`, versioned at `schema_version: "1.0"`.

Both input modes — image extraction and prompt generation — produce exactly this shape. A
consumer cannot tell which one it came from except by reading `metadata.source`.

## Shape

```json
{
  "schema_version": "1.0",
  "metadata": {
    "architecture_id": "arch_...",
    "name": "...",
    "source": "image | prompt | image_prompt | manual | import",
    "project_id": "...",
    "asset_ids": ["..."],
    "prompt": "...",
    "created_at": "...",
    "updated_at": "...",
    "revision": 1,
    "producers": {"detector": "yolo@...", "ocr": "paddle@..."}
  },
  "nodes": [],
  "edges": [],
  "interfaces": [],
  "networks": [],
  "vlans": [],
  "routing": {"mode": "none|static|ospf|static_and_ospf", "ospf": [], "static_routes": []},
  "nat": [],
  "acls": [],
  "unresolved": [],
  "missing_information": [],
  "enrichment_options": [],
  "validation": {"valid": true, "errors": [], "warnings": [], "recommendations": []},
  "status": "draft | needs_input | validated | invalid"
}
```

## Objects

| Object | Notes |
| --- | --- |
| `Node` | `id`, `type`, `name` (sourced), `raw_class` — the detector's own string, kept verbatim — `bbox`, `evidence` |
| `Interface` | `node_id`, `name`, `role`, `addresses[]` (each sourced), `vlan_id`, `allowed_vlans`, `mtu`, `mac` |
| `Edge` | `source` / `target` as `LinkEndpoint` (node + optional interface + pixel coordinates), `kind`, `vlan_id` |
| `Network` | a discovered L3 segment: subnet, scope, members, gateway |
| `VLAN` | 802.1Q id (1–4094 enforced), access and trunk interfaces |
| `RoutingPlan` | explicit `mode`, plus OSPF processes and static routes |
| `NATRule`, `ACL` | carried for translation; extension points for DHCP, DNS, VPN, BGP, HSRP/VRRP |

## Provenance

Every machine-derived fact is a `Sourced[T]`: a value, the `Evidence` that produced it
(source, confidence, bounding box, producer, observation ids), and every claim it
superseded.

```
USER 100 > DERIVED 70 > OCR 60 = YOLO 60 > EDGE_DETECTOR 55 > LLM 50 = RAG 50 > DEFAULT 10
```

Merging keeps the loser. Ties keep the incumbent, so replaying an extraction is idempotent.
This is what lets the UI answer "why does R1 have this address?" and what makes a user
correction authoritative without erasing what the machine saw.

## Structural integrity

Enforced at construction, so an impossible document cannot be built at all: no duplicate
node or interface ids, no edge to a node that does not exist, no interface belonging to an
absent node, no endpoint naming an unknown interface.

Network *correctness* is separate — that is the validators' job, and it produces issues
rather than exceptions.

## Gaps and questions

| Object | Meaning |
| --- | --- |
| `UnresolvedField` | a value deliberately carried as unknown rather than guessed |
| `MissingInformation` | a gap, classed REQUIRED / RECOMMENDED / OPTIONAL / DERIVABLE / ERROR |
| `EnrichmentOption` | something the user *could* add; never an error |
| `ClarificationQuestion` | a renderable question produced from the above by a policy |

`DERIVABLE` is a distinct class on purpose: "the system can compute this and only needs
permission" is a different conversation from "please supply this".

The question *strategy* is deliberately not encoded in the schema — only the structures a
policy needs — so the questioning algorithm can change without a schema or UI change.

## The gate

```python
architecture.can_proceed_to_rag
```

True when validation produced no errors **and** no REQUIRED information is still
unanswered. Warnings and optional gaps never block. This is the only gate between
extraction and translation, and `Net2TFRAGProvider.translate` re-checks it, because the RAG
performs no validation of its own.

## Revisions

Architectures are immutable. Every change produces a new `ArchitectureRevision` with a
parent link, a `change_kind` and a reason:

```
1 extraction → 2 user_correction → 3 enrichment → 4 manual_json_edit
```

A revision cannot claim a number its architecture disagrees with, revision 1 cannot have a
parent, and a successor adopts the chain's architecture id even when handed a foreign one —
so pasting a JSON document into the editor cannot fork the history.

Revision history is a durable business record in PostgreSQL, independent of LangGraph
checkpoints, which are an orchestration detail and may be pruned.
