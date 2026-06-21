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

## Use it from an AI agent (Claude, Copilot, Cursor, …)

The point of `graphify-sf` is that an agent answers Salesforce questions from the
**graph** instead of reading raw metadata — so it stays accurate and token-cheap. The
flow is always: **extract once → expose the graph → the agent queries it.**

### Step 1 — build the graph (once per org snapshot)

```bash
python -m graphify.salesforce extract path/to/sf-repo \
  --output-dir graphify-out --cpq-data path/to/cpq-records
# → graphify-out/graph.json
```

Re-run after a metadata pull to refresh it.

### Option A — MCP server (recommended)

Serve the graph over MCP; the agent gets token-bounded Salesforce tools
(`sf_impact`, `sf_violations`, `sf_cpq_chain`, `sf_ooe`) plus the base graph tools
(`query_graph`, `graph_stats`, `god_nodes`, `shortest_path`, `get_neighbors`, …).

```bash
# stdio transport (default — what desktop agents launch)
python -m graphify.serve graphify-out/graph.json
# or the console script after `pip install -e .`
graphify-mcp graphify-out/graph.json
# shared HTTP transport (team / remote)
python -m graphify.serve graphify-out/graph.json --transport http --port 8080
```

**Claude Code** — one command (use an absolute path to `graph.json`):

```bash
claude mcp add graphify-sf -- python -m graphify.serve /abs/path/to/graphify-out/graph.json
```

**Claude Desktop / Cursor / Windsurf** — add to the MCP config
(`claude_desktop_config.json`, `.cursor/mcp.json`, …); they share this shape:

```json
{
  "mcpServers": {
    "graphify-sf": {
      "command": "python",
      "args": ["-m", "graphify.serve", "/abs/path/to/graphify-out/graph.json"]
    }
  }
}
```

**VS Code / GitHub Copilot** — `.vscode/mcp.json` uses the `servers` key:

```json
{
  "servers": {
    "graphify-sf": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "graphify.serve", "/abs/path/to/graphify-out/graph.json"]
    }
  }
}
```

Then just ask, e.g. *"What breaks if I change `Opportunity.Amount`? Use graphify-sf."*

### Option B — no MCP (CLI the agent shells out to)

For agents that run shell commands but have no MCP, point them at the CLI — each
command prints a token-bounded answer to stdout:

```bash
python -m graphify.salesforce impact <node-id> --direction downstream
python -m graphify.salesforce violations --severity HIGH
python -m graphify.salesforce cpq-chain SBQQ__Quote__c
python -m graphify.salesforce ooe Opportunity
```

### Tell the agent to use it on every task

Drop a short rule into the agent's project-instructions file — `CLAUDE.md` (Claude
Code), `.github/copilot-instructions.md` (Copilot), `.cursorrules` (Cursor), or
`AGENTS.md`:

```markdown
## Salesforce knowledge graph
Before answering Salesforce impact / CPQ / Order-of-Execution / Governor-Limit
questions, consult the graphify-sf graph instead of reading raw metadata:
- If the `graphify-sf` MCP server is connected, call `sf_impact` / `sf_violations` /
  `sf_cpq_chain` / `sf_ooe`.
- Otherwise run `python -m graphify.salesforce <impact|violations|cpq-chain|ooe> …`.
Rebuild it with `python -m graphify.salesforce extract <repo> --cpq-data <dir>`
after pulling fresh metadata.
```

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
