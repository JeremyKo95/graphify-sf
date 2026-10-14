# graphify-sf

> ⚡ A **Salesforce-native** knowledge-graph library — turn any Enterprise (CPQ)
> Salesforce org's metadata into a token-minimized graph for onboarding, analysis,
> impact review, and development.
> Inspired by [graphify](https://github.com/safishamsi/graphify); rebuilt in Python
> for Apex, Flow, LWC, CPQ data, and the realities of Governor Limits.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](#)

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

- **Parsers** (`graphify/salesforce/`): Apex classes & triggers, Flow (typed record
  tags), LWC (`@wire` + imperative Apex), Aura bundles, Custom Object/Field,
  Validation Rule, Record Type, Workflow, Custom Metadata records, Sharing Rules,
  Custom Labels, Custom Settings, **Lightning pages (FlexiPage) and Experience Cloud
  views**, and — opt-in — Profile / Permission Set / Permission Set Group.
- **Field-level lineage**: which Apex, Flow, formula field, Validation Rule or page
  **reads** or **writes** each field (`reads_field` / `writes_field`), through
  relationship paths (`Account.Owner.Email` → `User.Email`) and child subqueries;
  lookups as `lookup_to`.
- **UI placement**: which FlexiPage / Experience page `hosts` each LWC or Aura
  component, so upstream impact reaches the screen.
- **CPQ rule data ingest** (`cpq_data.py`): SBQQ Price/Product Rules, Conditions,
  Actions from **SFDX JSON or Gearset `*.gs.json`**, plus **JavaScript QCP** custom
  scripts — the logic that lives as data records, not metadata.
- **`__mdt` field mappings** (`mdt_mapping.py`): turns mapping records (e.g.
  `Opportunity_To_Quote_Mapping__mdt`) into traversable `maps_to` field→field edges so
  impact follows the Opp→Quote boundary.
- **Analysis passes** (whole-graph): Order of Execution, Governor Limits, recursive
  triggers, Profile/FLS permission impact, Flow↔CPQ infinite loops, CPQ↔Validation
  Rule conflicts.
- **Token-minimized consumption** (`pipeline.py`/`query.py`/`cli.py`/`viz.py`):
  deterministic community clustering, `token_budget`-bounded queries, an MCP server,
  and a focused self-contained HTML visualization.
- **A stable base for ontologies and diffs**: API-name-based node IDs, a
  byte-identical `graph.json` for the same input, a small `summary.json` with
  integrity counters, and no source code inside the graph. See
  [Graph model](#graph-model).

Every edge carries a `confidence` (EXTRACTED / INFERRED / AMBIGUOUS) so you know
what is certain vs. heuristic.

## Install

Published on PyPI as **[`graphify-sfdx`](https://pypi.org/project/graphify-sfdx/)**:

```bash
pip install graphify-sfdx           # into the current environment
pipx install graphify-sfdx          # isolated CLI on PATH
uv tool install graphify-sfdx       # same, via uv
```

Run it without installing anything:

```bash
uvx --from graphify-sfdx graphify-sfdx --help
```

Two console scripts are provided: **`graphify-sfdx`** (the SF CLI) and
**`graphify-sfdx-mcp`** (the MCP server). Requires Python 3.10+. The only required
dependency is `networkx`. Extras: `graphify-sfdx[mcp]` for the MCP server,
`graphify-sfdx[neo4j]` for live Neo4j push, `graphify-sfdx[code]` for the upstream
multi-language code-graph features (tree-sitter grammars), which the Salesforce
extractor does not use.
For local development, `pip install -e .` from a clone still works.

## Quick start

```bash
# 1. Extract + enrich a graph (optionally merge CPQ rule data)
graphify-sfdx extract path/to/sf-repo \
  --output-dir graphify-out --cpq-data path/to/cpq-records
#    -> graphify-out/graph.json    (full graph, deterministic)
#    -> graphify-out/summary.json  (counts + integrity report, a few KB)
#    --include-permissions  also parse Profiles / Permission Sets / PSGs
#    --strict               exit 1 if summary.json reports an integrity error

# 2. Ask SF-aware questions (token-bounded)
graphify-sfdx impact <node-id> --direction downstream
graphify-sfdx violations --severity HIGH
graphify-sfdx cpq-chain SBQQ__Quote__c
graphify-sfdx ooe Opportunity

# 3. Visualize a focused subgraph (self-contained HTML)
graphify-sfdx viz graphify-out/graph.json --focus <node-id>

# 4. Serve over MCP for an LLM client
graphify-sfdx-mcp graphify-out/graph.json
```

> Every `graphify-sfdx <cmd>` is equivalent to `python -m graphify.salesforce <cmd>`,
> and `graphify-sfdx-mcp` to `python -m graphify.serve` — use whichever your setup
> prefers.

Run the test suite with `pytest tests/test_salesforce.py -v`.

## Use it from an AI agent (Claude, Copilot, Cursor, …)

The point of `graphify-sf` is that an agent answers Salesforce questions from the
**graph** instead of reading raw metadata — so it stays accurate and token-cheap. The
flow is always: **extract once → expose the graph → the agent queries it.**

### Step 1 — build the graph (once per org snapshot)

```bash
graphify-sfdx extract path/to/sf-repo \
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
graphify-sfdx-mcp graphify-out/graph.json
# shared HTTP transport (team / remote)
graphify-sfdx-mcp graphify-out/graph.json --transport http --port 8080
```

**MCP is a standard protocol, so this one server works with every MCP client** —
Claude, Codex, Cursor, Copilot, Windsurf, … all launch the same `graphify-sfdx-mcp`
command. Only the config file location and format differ. Use an **absolute path** to
`graph.json`. If `graphify-sfdx` isn't installed on PATH, swap the command for
`uvx` + `--from "graphify-sfdx[mcp]"` (shown below) — no clone, no install step.

**Claude Code** — one command:

```bash
# installed on PATH
claude mcp add graphify-sfdx -- graphify-sfdx-mcp /abs/path/to/graphify-out/graph.json
# or zero-install via uvx
claude mcp add graphify-sfdx -- uvx --from "graphify-sfdx[mcp]" graphify-sfdx-mcp /abs/path/to/graphify-out/graph.json
```

**Claude Desktop / Cursor / Windsurf** — JSON config with an `mcpServers` key
(`claude_desktop_config.json`, `.cursor/mcp.json`, …); they share this shape:

```json
{
  "mcpServers": {
    "graphify-sfdx": {
      "command": "uvx",
      "args": ["--from", "graphify-sfdx[mcp]", "graphify-sfdx-mcp", "/abs/path/to/graphify-out/graph.json"]
    }
  }
}
```

**OpenAI Codex CLI** — TOML config at `~/.codex/config.toml`:

```toml
[mcp_servers.graphify-sfdx]
command = "uvx"
args = ["--from", "graphify-sfdx[mcp]", "graphify-sfdx-mcp", "/abs/path/to/graphify-out/graph.json"]
```

**VS Code / GitHub Copilot** — `.vscode/mcp.json` uses the `servers` key:

```json
{
  "servers": {
    "graphify-sfdx": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "graphify-sfdx[mcp]", "graphify-sfdx-mcp", "/abs/path/to/graphify-out/graph.json"]
    }
  }
}
```

> In any of these, replace the `uvx --from "graphify-sfdx[mcp]" graphify-sfdx-mcp` command
> with a bare `graphify-sfdx-mcp` if you installed the package on PATH.

Then just ask, e.g. *"What breaks if I change `Opportunity.Amount`? Use graphify-sfdx."*

### Option B — no MCP (CLI the agent shells out to)

For agents that run shell commands but have no MCP, point them at the CLI — each
command prints a token-bounded answer to stdout:

```bash
graphify-sfdx impact <node-id> --direction downstream
graphify-sfdx violations --severity HIGH
graphify-sfdx cpq-chain SBQQ__Quote__c
graphify-sfdx ooe Opportunity
```

### Tell the agent to use it on every task

Drop a short rule into the agent's project-instructions file — `CLAUDE.md` (Claude
Code), `.github/copilot-instructions.md` (Copilot), `.cursorrules` (Cursor), or
`AGENTS.md`:

```markdown
## Salesforce knowledge graph
Before answering Salesforce impact / CPQ / Order-of-Execution / Governor-Limit
questions, consult the graphify-sf graph instead of reading raw metadata:
- If the `graphify-sfdx` MCP server is connected, call `sf_impact` / `sf_violations` /
  `sf_cpq_chain` / `sf_ooe`.
- Otherwise run `graphify-sfdx <impact|violations|cpq-chain|ooe> …`.
Rebuild it with `graphify-sfdx extract <repo> --cpq-data <dir>` after pulling fresh
metadata.
```

## Graph model

What downstream consumers (an ontology mapping, a daily diff, an agent) can rely on.

**Node IDs** are derived from Salesforce API names, never from display labels or
parse order, so a mapping that points at an ID keeps pointing at the same thing:

| Node (`file_type`) | ID | `sf_qualified_name` / label |
|---|---|---|
| SObject (`sobject`) | `sobject_account` | `Account` |
| Field (`field`) | `field_account.region__c` | `Account.Region__c` |
| Apex class / trigger (`code`) | `apex_accountservice` | `AccountService` |
| Apex method (`code`) | `apex_accountservice.getaccounts` | `AccountService.getAccounts` |
| Flow (`flow`) | `flow_<name>` | Flow API name |
| LWC / Aura (`lwc_component` / `aura_component`) | `lwc_<bundle>` / `aura_<bundle>` | bundle folder name |
| Validation Rule (`validation_rule`) | `validation_account.need_region` | `Account.Need_Region` |
| FlexiPage / Experience view | `flexipage_<name>` / `experience_<site>.<view>` | `<name>` / `<site>/<view>` |

IDs are lower-cased; `sf_qualified_name` keeps the original casing and is the
recommended join key. Display labels live in `sf_label`.

**Main relations**

| Relation | From → To |
|---|---|
| `field_of`, `lookup_to` | field → its object, lookup field → referenced object |
| `reads_field`, `writes_field` | Apex / Flow / formula field / Validation Rule / page → field |
| `queries`, `dml_operates_on` | Apex / Flow → SObject (`sf_dynamic` for string-built SOQL) |
| `triggers_on` | Apex trigger / record-triggered Flow → SObject |
| `member_of` | Apex method → class |
| `wire_to`, `lwc_calls`, `aura_controller`, `flow_invokes` | UI / Flow → Apex |
| `hosts`, `page_for` | page → component, record page → SObject |
| `cpq_*`, `maps_to`, `validates`, `order_of_execution`, diagnostics | CPQ rules, `__mdt` mappings, analysis passes |

**Guarantees**

- The graph is a directed multigraph keyed by relation: a class that both reads
  and updates `Account` keeps both edges; repeated sites fold into one edge with
  `sf_occurrences` and `source_locations`.
- Same input → byte-identical `graph.json` (sorted output, deterministic clustering).
- `source_file` is relative to the extracted path; no source code is stored
  (Apex nodes keep `sf_loc` and `sf_source_hash`).
- `sf_stub: true` marks something referenced but not defined in the repo (standard
  objects/fields, managed classes); `sf_unresolved` marks a field reference whose
  object could not be determined.
- `summary.json` reports node/edge counts plus integrity counters that must stay 0;
  `--strict` turns a non-zero counter into a failing exit code.

## Coverage

| Area | Module | Status |
|---|---|---|
| Core parsers (Apex/Flow/LWC/Aura/Objects) | `apex_enhanced`, `flow`, `lwc`, `pages`, `objects` | ✅ |
| Field-level reads / writes (Apex, Flow, formulas, Validation Rules, pages) | `field_refs` + parsers | ✅ |
| UI placement (FlexiPage, Experience Cloud views) | `pages` | ✅ |
| Metadata coverage (RecordType/Workflow/Custom Metadata/Sharing/Label/Setting) | `metadata`, `objects` | ✅ |
| Permissions (Profile/Permission Set/PSG) — opt-in | `profiles`, `metadata` | ✅ |
| CPQ rule data (SFDX + Gearset + JS QCP) | `cpq_data` | ✅ |
| `__mdt` field mapping (Opp→Quote) | `mdt_mapping` | ✅ |
| Token-minimized consumption (cluster/query/CLI/viz/MCP) | `pipeline`, `query`, `cli`, `viz` | ✅ |
| Integrity report (`summary.json`, `--strict`) | `quality`, `validate_sf` | ✅ |
| Neo4j export | `neo4j_sf` | ✅ |
| Apex cross-class call resolution | symbol table needed | ⏸ deferred |

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
