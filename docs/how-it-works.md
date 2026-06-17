# How graphify-sf works

## The three passes

graphify processes your files in three passes:

**Pass 1 — Code structure (free, no API calls)**
Tree-sitter parses your code files and extracts classes, functions, imports, call graphs, and inline comments. This runs locally with no LLM involved. 25+ languages supported. SQL files get special treatment: tables, views, foreign keys, and JOIN relationships are extracted deterministically.

Code files are not sent to the LLM semantic extractor in the normal pipeline. If a corpus contains only code files, Pass 3 is skipped entirely; semantic extraction is reserved for docs, papers, images, and transcripts.

**Pass 2 — Video and audio (local, no API calls)**
Video and audio files are transcribed with faster-whisper. To focus the transcript on your domain, the transcription prompt is seeded with your top god nodes (the most-connected concepts in your code graph so far). Transcripts are cached — re-runs skip already-processed files.

**Pass 3 — Docs, papers, images (Claude subagents, costs tokens)**
Claude runs in parallel over markdown, PDFs, images, and transcripts. Each subagent reads a batch of files and outputs a JSON fragment: nodes, edges, and any group relationships. The fragments are merged into a single graph.

Before Pass 3, optional converters turn supported pointer/binary formats into
Markdown sidecars under `graphify-out/converted/`. Office files (`.docx`,
`.xlsx`) use the `[office]` extra. Google Workspace shortcuts (`.gdoc`,
`.gsheet`, `.gslides`) are opt-in with `--google-workspace` or
`GRAPHIFY_GOOGLE_WORKSPACE=1` and require an authenticated `gws` CLI.

---

## Salesforce-specific passes (graphify-sf)

When you run `graphify.salesforce.extract_sf()`, four additional analysis passes run after Pass 1 completes:

**Pass SF-1 — CPQ enrichment**
Scans the merged graph for nodes with `SBQQ__` prefixed labels and reclassifies them as `cpq_rule` type. Detects classes that implement `SBQQ.QuoteCalculatorPlugin` (the Quote Calculator Plugin interface) and adds `cpq_applies_to` edges from the QCP class to its target Quote/QuoteLine objects.

**Pass SF-2 — LWC merge**
Lightning Web Components have their logic split across `<name>.html` and `<name>.js`. Each file is parsed separately (for parallel processing). This pass merges pairs of results from the same component directory into a single `lwc_component` node, then adds `wire_to` edges from the component to any Apex methods referenced via `@wire`.

**Pass SF-3 — Order of Execution modeling**
For each SObject that has at least one Apex trigger, Before Save Flow, or Validation Rule attached, this pass generates 18 concept nodes representing the Salesforce Order of Execution steps (System Validation → Before Triggers → Custom Validation → ... → Commit). Steps are chained with `order_of_execution` edges. SObjects referenced only via SOQL are excluded — SOQL does not trigger the execution order.

**Pass SF-4 — Governor Limit diagnosis**
Scans the graph for `queries` edges tagged with `sf_in_loop: true` (set by the Apex parser when a SOQL query is detected inside a for/while block). Emits `governor_violation` diagnostic edges from the offending method to a sentinel node representing the violation type (`gov_limit_soql_in_loop`, `gov_limit_dml_in_loop`, etc.). One sentinel node per violation type prevents node explosion.

---

## How Salesforce metadata files are parsed

`.flow-meta.xml` files are parsed with `xml.etree.ElementTree` (no external dependency). The parser walks the Flow XML tree and emits:
- A `flow` type node for the Flow file itself
- Child nodes for each Flow element (Decision, Assignment, Loop, Screen, ApexAction, SubFlow)
- `flow_invokes` edges for Apex Action elements
- `queries` / `dml_operates_on` edges for Record Lookup / Create / Update / Delete elements

`.object-meta.xml` and `.field-meta.xml` files produce `sobject` and `field` nodes, with `field_of` edges connecting fields to their parent objects.

`.profile-meta.xml` and `.permissionset-meta.xml` files produce `profile` and `permission_set` nodes, with `grants_access_to` edges to the objects and fields they permit.

---

## How cross-file resolution works for Salesforce

graphify-sf uses a shared node ID convention to link Salesforce concepts across file types. The `sobject_nid(api_name)` function produces `"sobject_account"` for `"Account"` regardless of which file discovered it. This means:

- An Apex trigger's `triggers_on` edge
- A Flow's `queries` edge
- A Custom Object XML's SObject node

...all resolve to the **same node** in `build_graph()`. The graph merges them automatically.

The same principle applies to Apex method nodes: LWC `@wire` imports and Apex class definitions both use the same method node ID (`{class_stem}_{method_name}`), so the `wire_to` edge resolves correctly without a dedicated cross-file pass.

---

## How community detection works

Communities are found using the [Leiden algorithm](https://www.nature.com/articles/s41598-019-41695-z) — a graph-clustering method that groups nodes by edge density. Nodes with many connections between them end up in the same community.

In Salesforce graphs, communities naturally form around business domains: the `Account` SObject, its triggers, flows, related LWC components, and CPQ price rules will typically cluster together. This makes the community report a useful first-pass architecture map.

**No embeddings needed.** The semantic similarity edges that Claude extracts (`semantically_similar_to`) are already in the graph, so they influence community shape directly. The graph structure is the similarity signal — there's no separate embedding step or vector database.

---

## Confidence tagging

Every relationship is tagged with one of three labels:

| Tag | Meaning |
|-----|---------|
| `EXTRACTED` | Found directly in the source (e.g. a function call, an import, a Flow XML element) |
| `INFERRED` | A reasonable inference (e.g. cross-file call resolution, CPQ rule target) |
| `AMBIGUOUS` | Uncertain — flagged in the report for human review |

EXTRACTED edges always have confidence 1.0. INFERRED edges use a discrete rubric:
- **0.95** — near-certain (explicit cross-file reference, one plausible target)
- **0.85** — strong evidence (naming + context align)
- **0.75** — reasonable (contextual but not explicit)
- **0.65** — weak (naming similarity only)
- **0.55** — speculative

Salesforce-specific confidence notes:
- `triggers_on` from explicit trigger declaration → `EXTRACTED`
- `queries` from inline SOQL `FROM Account` → `EXTRACTED`
- `cpq_applies_to` from QCP interface detection → `INFERRED` (0.85)
- `flow_invokes` from XML `<apexClass>` element → `EXTRACTED`

---

## Neo4j export

When running `to_cypher_sf()` or `push_to_neo4j_sf()`, graphify-sf maps its node types to Salesforce-aware Neo4j labels:

| graphify `file_type` | Neo4j label |
|---|---|
| `code` (Apex class) | `:ApexClass` |
| `sobject` | `:SObject` |
| `flow` | `:Flow` |
| `lwc_component` | `:LWCComponent` |
| `cpq_rule` | `:CPQRule` |
| `profile` | `:Profile` |
| `permission_set` | `:PermissionSet` |
| `concept` (OoE/Governor) | `:Concept` |

This enables Cypher queries like:

```cypher
// 영향도 분석: Account SObject에 연결된 모든 Apex 트리거 찾기
MATCH (t:ApexClass)-[:TRIGGERS_ON]->(s:SObject {label: 'Account'})
RETURN t.label, t.source_file

// 거버너 한계 위반 목록
MATCH (m:ApexClass)-[v:GOVERNOR_VIOLATION]->(g)
WHERE v.sf_severity = 'HIGH'
RETURN m.label, v.sf_violation_type, v.source_location

// CPQ 의존성 체인
MATCH path = (q:CPQRule)-[:CPQ_APPLIES_TO*1..3]->(t)
RETURN path
```

---

## Token benchmark

The first run extracts and builds the graph — this costs tokens. Every subsequent query reads the compact graph instead of raw files. That's where the savings compound.

On a mixed corpus (Karpathy repos + 5 papers + 4 images, 52 files): **71.5x fewer tokens per query** vs reading the raw files directly.

| Corpus | Files | Reduction |
|--------|-------|-----------|
| Karpathy repos + papers + images | 52 | **71.5x** |
| graphify source + Transformer paper | 4 | **5.4x** |
| httpx (synthetic Python library) | 6 | ~1x |

Token reduction scales with corpus size. Enterprise Salesforce repos (500~2,000 files) see the highest compression — the graph collapses thousands of SOQL references into a handful of SObject nodes.

---

## Parallel extraction

Code files are extracted in parallel using `ProcessPoolExecutor` — bypasses Python's GIL for genuine multiprocessing. Doc/paper/image batches are dispatched as parallel Claude subagents. On a corpus of 84 code files, parallel AST extraction runs in about 1.66x less time than sequential.

Salesforce analysis passes (CPQ, OoE, Governor Limits) run sequentially after all parallel file extraction completes. They are graph-wide O(N) operations with no I/O, so sequential execution adds negligible time.

---

## SHA256 cache

Every extracted file is fingerprinted by content hash. Re-runs skip unchanged files entirely — only new or modified files go through extraction again. The cache lives in `graphify-out/cache/`.

Salesforce analysis pass results are NOT cached — they run on the merged graph every time. This is intentional: the passes consume the aggregate graph and can't be cached per-file.

---

## The graph format

The output `graph.json` uses NetworkX's node-link format. Each node has:
- `id` — stable identifier
- `label` — human-readable name
- `file_type` — `code`, `sobject`, `flow`, `lwc_component`, `cpq_rule`, `concept`, etc.
- `source_file` — where it came from
- Salesforce extras (not validated by base schema): `sf_return_type`, `sf_annotations`, `sf_in_loop`, `sf_ooe_step`, `sf_violation_type`, etc.

Each edge has:
- `source`, `target` — node IDs
- `relation` — `calls`, `triggers_on`, `queries`, `flow_invokes`, `wire_to`, `governor_violation`, etc.
- `confidence` — `EXTRACTED`, `INFERRED`, or `AMBIGUOUS`
- `source_file` — where the relationship was found

See [RFC: file-level node summaries](node-summaries-rfc.md) for two proposed
ways to add compact optional summaries for AI navigation.
