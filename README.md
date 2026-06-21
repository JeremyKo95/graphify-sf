# graphify-sf

> ⚡ A **Salesforce-native** knowledge-graph library — turn any Enterprise (CPQ)
> Salesforce org's metadata into a token-minimized graph for onboarding, analysis,
> impact review, and development.
> Inspired by [graphify](https://github.com/safishamsi/graphify); rebuilt in Python
> for Apex, Flow, LWC, CPQ data, and the realities of Governor Limits.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](#)

---

## What it is

`graphify-sf` is a **Python library** (not an on-platform package). It statically
parses a Salesforce metadata repository — and CPQ rule **data** — into a NetworkX
knowledge graph, runs Salesforce-aware analysis passes, then serves a
**token-minimized** view so an LLM (or a person) can answer "what breaks if I change
X?", "how is a Quote created from an Opportunity?", or "where are the Governor-Limit
risks?" without reading the raw org.

The North Star: **any Enterprise CPQ org → SF-specific KB graph → token-minimized
consumption → onboarding, analysis, impact review, and development.**

## Capabilities

- **Parsers** (`graphify/salesforce/`): Apex (regex, ADR-019), Flow (typed record
  tags), LWC (`@wire` + imperative Apex), Custom Object/Field, Profile/Permission Set,
  Validation Rule, Record Type, Permission Set Group, Workflow, **Custom Metadata
  records, Sharing Rules, Custom Labels, Custom Settings**.
- **CPQ rule data ingest** (`cpq_data.py`): SBQQ Price/Product Rules, Conditions,
  Actions from **SFDX JSON or Gearset `*.gs.json`**, plus **JavaScript QCP** custom
  scripts — the logic that lives as data records, not metadata.
- **`__mdt` field mappings** (`mdt_mapping.py`): turns mapping records (e.g.
  `Opportunity_To_Quote_Mapping__mdt`) into traversable `maps_to` field→field edges so
  impact follows the Opp→Quote boundary.
- **Analysis passes** (whole-graph): Order of Execution, Governor Limits, recursive
  triggers, Profile/FLS permission impact, Flow↔CPQ infinite loops, CPQ↔Validation
  Rule conflicts.
- **Token-minimized consumption** (`pipeline.py`/`query.py`/`cli.py`/`viz.py`): Leiden
  community clustering, `token_budget`-bounded queries, an MCP server, and a focused
  self-contained HTML visualization.

Every node/edge carries a `confidence` (EXTRACTED / INFERRED / AMBIGUOUS) so you know
what is certain vs. heuristic. See [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md),
[`docs/ADR.md`](./docs/ADR.md), and [`docs/PRD.md`](./docs/PRD.md).

## Install

```bash
pip install -e .
```

Requires Python 3.11+. `networkx` is required; `graspologic` (Leiden), `mcp` (serve),
and `tree-sitter` are optional and degrade gracefully when absent.

## Quick start

```bash
# 1. Extract + enrich a graph (optionally merge CPQ rule data)
python -m graphify.salesforce extract path/to/sf-repo \
  --output-dir graphify-out --cpq-data path/to/cpq-records

# 2. Ask SF-aware questions (token-bounded)
python -m graphify.salesforce impact <node-id> --direction downstream
python -m graphify.salesforce violations --severity HIGH
python -m graphify.salesforce cpq-chain SBQQ__Quote__c
python -m graphify.salesforce ooe Opportunity

# 3. Visualize a focused subgraph (self-contained HTML)
python -m graphify.salesforce viz graphify-out/graph.json --focus <node-id>

# 4. Serve over MCP for an LLM client
python -m graphify.serve graphify-out/graph.json
```

Run the test suite with `pytest tests/test_salesforce.py -v`.

## Coverage

| Area | Module | Status |
|---|---|---|
| Core parsers (Apex/Flow/LWC/Objects/Profiles) | `apex_enhanced`, `flow`, `lwc`, `objects`, `profiles` | ✅ |
| Metadata coverage (RecordType/PSG/Workflow/Custom Metadata/Sharing/Label/Setting) | `metadata`, `objects` | ✅ |
| CPQ rule data (SFDX + Gearset + JS QCP) | `cpq_data` | ✅ |
| `__mdt` field mapping (Opp→Quote) | `mdt_mapping` | ✅ |
| Token-minimized consumption (cluster/query/CLI/viz/MCP) | `pipeline`, `query`, `cli`, `viz` | ✅ |
| Neo4j export | `neo4j_sf` | ✅ |
| Apex cross-class call resolution | tree-sitter promotion | ⏸ deferred (ADR-019) |

---

## Inspiration & Credits

This project stands on the shoulders of [**graphify**](https://github.com/safishamsi/graphify)
by **[Safi Shamsi](https://github.com/safishamsi)**. The core idea, architecture, and
API ergonomics are theirs — `graphify-sf` is an independent, Salesforce-tailored
adaptation built with deep respect for the original work. If you need a
platform-agnostic solution, please use and support the upstream project.

> 본 프로젝트는 [graphify](https://github.com/safishamsi/graphify)의 아이디어와 아키텍처에서
> 출발했습니다. 핵심 개념에 대한 모든 공로는 원작자에게 있으며, `graphify-sf`는 이를 Salesforce
> 플랫폼(Apex/Flow/LWC/CPQ) 환경에 맞게 재구현한 독립적인 파생 프로젝트입니다. 원작자의 노고에
> 깊이 감사드립니다. 범용 환경이 필요하다면 원본 프로젝트를 사용하시길 권장합니다.

> The upstream project's original README is preserved in this repo as [`README.upstream.md`](./README.upstream.md).

## License

MIT © Safi Shamsi (graphify) · MIT © JeremyKo95 (graphify-sf). See [LICENSE](./LICENSE).
