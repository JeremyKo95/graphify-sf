"""
graphify-sf: SF-semantic query core (shared by the CLI and the MCP server).

Pure functions over an enriched SF graph (a NetworkX ``DiGraph`` produced by
``pipeline.build_sf_graph``). Each returns a structured dict plus a
token-bounded ``text`` rendering, so the same logic serves both ``graphify sf``
CLI subcommands and the MCP tools (serve.py) without either importing the
other's heavy deps. Only ``networkx`` + ``graphify.security.sanitize_label`` are
imported here — no ``mcp`` import, so the CLI works without the MCP stack.

The four queries answer the SF onboarding / impact questions the generic graph
tools can't express on their own:
    - ``sf_impact``     — "what breaks if I change X?" (directional, confidence-ranked)
    - ``sf_violations`` — governor / loop / CPQ-validation / FLS risks by severity
    - ``sf_cpq_chain``  — CPQ Calc Engine execution order for a Quote object
    - ``sf_ooe``        — Order-of-Execution chain for an SObject
"""

from __future__ import annotations

import networkx as nx

from graphify.salesforce.constants import sobject_nid
from graphify.security import sanitize_label

#: Diagnostic relations surfaced by ``sf_violations`` (analysis-pass outputs).
_VIOLATION_RELATIONS = {
    "governor_violation",
    "infinite_loop_risk",
    "cpq_validation_risk",
    "gov_permission_violation",
}

#: Severity rank for ordering (higher = surfaced first).
_SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}

#: Fallback confidence when an edge carries only the string label, not a value.
_CONFIDENCE_VALUE = {"EXTRACTED": 1.0, "INFERRED": 0.7, "AMBIGUOUS": 0.4}


def _edge_confidence(data: dict) -> float:
    """Numeric confidence for an edge (explicit ``confidence_value`` wins)."""
    value = data.get("confidence_value")
    if isinstance(value, (int, float)):
        return float(value)
    return _CONFIDENCE_VALUE.get(data.get("confidence", ""), 1.0)


def resolve_node(G: nx.DiGraph, query: str) -> str | None:
    """Resolve a user-supplied token to a node ID.

    Tries, in order: exact ID, ``sobject_nid(query)``, case-insensitive label
    match, case-insensitive substring of ID/label. Returns ``None`` if nothing
    matches.
    """
    if query in G:
        return query
    sid = sobject_nid(query)
    if sid in G:
        return sid
    q = query.lower()
    for nid, data in G.nodes(data=True):
        if str(data.get("label", "")).lower() == q:
            return nid
    for nid, data in G.nodes(data=True):
        if q in nid.lower() or q in str(data.get("label", "")).lower():
            return nid
    return None


def _render(G: nx.DiGraph, node_ids: list[str], edges: list[tuple[str, str]],
            *, seeds: list[str] | None = None, token_budget: int = 2000) -> str:
    """Render nodes + edges as token-bounded text (~3 chars/token), sanitized.

    Seeds render first; remaining nodes are ordered by degree (most connected
    first) so truncation drops the least central context.
    """
    char_budget = token_budget * 3
    seed_set = set(seeds or [])
    node_set = set(node_ids)
    ordered = [n for n in (seeds or []) if n in node_set] + sorted(
        node_set - seed_set, key=lambda n: G.degree(n), reverse=True
    )
    lines: list[str] = []
    for nid in ordered:
        d = G.nodes[nid]
        lines.append(
            f"NODE {sanitize_label(str(d.get('label', nid)))} "
            f"[type={sanitize_label(str(d.get('file_type', '')))} "
            f"src={sanitize_label(str(d.get('source_file', '')))}]"
        )
    for u, v in edges:
        if u in node_set and v in node_set and G.has_edge(u, v):
            d = G[u][v]
            sev = d.get("sf_severity")
            sev_suffix = f" sev={sanitize_label(str(sev))}" if sev else ""
            lines.append(
                f"EDGE {sanitize_label(str(G.nodes[u].get('label', u)))} "
                f"--{sanitize_label(str(d.get('relation', '')))} "
                f"[{sanitize_label(str(d.get('confidence', '')))}{sev_suffix}]--> "
                f"{sanitize_label(str(G.nodes[v].get('label', v)))}"
            )
    output = "\n".join(lines)
    if len(output) > char_budget:
        cut = output[:char_budget].rfind("\n")
        cut = cut if cut > 0 else char_budget
        dropped = len(lines) - output[:cut].count("\n") - 1
        output = output[:cut] + f"\n... (truncated — ~{dropped} more lines cut by {token_budget}-token budget)"
    return output


def sf_impact(
    G: nx.DiGraph,
    node: str,
    *,
    direction: str = "downstream",
    depth: int = 3,
    min_confidence: float = 0.0,
    token_budget: int = 2000,
) -> dict:
    """Impact traversal from *node* — "what breaks if I change this?".

    ``direction``: ``downstream`` (successors — what this affects), ``upstream``
    (predecessors — what affects this), or ``both``. Edges below
    ``min_confidence`` are skipped. Returns ``{node, direction, nodes, edges,
    text}``; ``node`` is ``None`` (with an explanatory ``text``) if unresolved.
    """
    start = resolve_node(G, node)
    if start is None:
        return {"node": None, "direction": direction, "nodes": [], "edges": [],
                "text": f"No matching node for {node!r}."}

    visited = {start}
    edges: list[tuple[str, str]] = []
    frontier = {start}
    for _ in range(max(1, depth)):
        nxt: set[str] = set()
        for n in frontier:
            neighbors: list[tuple[str, str]] = []
            if direction in ("downstream", "both"):
                neighbors += [(n, s) for s in G.successors(n)]
            if direction in ("upstream", "both"):
                neighbors += [(p, n) for p in G.predecessors(n)]
            for u, v in neighbors:
                if _edge_confidence(G[u][v]) < min_confidence:
                    continue
                other = v if u == n else u
                edges.append((u, v))
                if other not in visited:
                    nxt.add(other)
        visited |= nxt
        frontier = nxt
        if not frontier:
            break

    text = _render(G, list(visited), edges, seeds=[start], token_budget=token_budget)
    header = (f"IMPACT {G.nodes[start].get('label', start)} "
              f"({direction}, depth={depth}, min_conf={min_confidence}) | "
              f"{len(visited)} nodes\n\n")
    return {"node": start, "direction": direction,
            "nodes": sorted(visited), "edges": edges, "text": header + text}


def sf_violations(G: nx.DiGraph, *, severity: str | None = None,
                  token_budget: int = 2000) -> dict:
    """Collect diagnostic risk edges, ordered by severity (highest first).

    ``severity`` (optional) filters to a single ``sf_severity`` level. Returns
    ``{violations: [...], text}`` where each violation carries source/target
    labels, relation, severity, and the risk-specific fields.
    """
    sev_filter = severity.upper() if severity else None
    found: list[dict] = []
    for u, v, d in G.edges(data=True):
        if d.get("relation") not in _VIOLATION_RELATIONS:
            continue
        edge_sev = str(d.get("sf_severity", "")).upper()
        if sev_filter and edge_sev != sev_filter:
            continue
        found.append({
            "source": u, "target": v,
            "source_label": G.nodes[u].get("label", u),
            "target_label": G.nodes[v].get("label", v),
            "relation": d.get("relation"),
            "severity": edge_sev or None,
            "violation_type": d.get("sf_violation_type"),
            "reason": d.get("sf_reason") or d.get("sf_note"),
        })
    found.sort(key=lambda x: _SEVERITY_RANK.get(x["severity"] or "", -1), reverse=True)

    lines = [
        f"[{v['severity'] or '-'}] {sanitize_label(str(v['relation']))}: "
        f"{sanitize_label(str(v['source_label']))} -> {sanitize_label(str(v['target_label']))}"
        f"{(' — ' + sanitize_label(str(v['reason']))) if v['reason'] else ''}"
        for v in found
    ]
    text = "\n".join(lines) or "No violations found."
    char_budget = token_budget * 3
    if len(text) > char_budget:
        text = text[:char_budget] + "\n... (truncated by token budget)"
    return {"violations": found, "text": f"{len(found)} violation(s)\n\n{text}"}


def sf_cpq_chain(G: nx.DiGraph, quote_object: str, *, token_budget: int = 2000) -> dict:
    """CPQ Calc Engine execution order for *quote_object*.

    Collects ``cpq_applies_to`` edges targeting the object (ordered by
    ``execution_order``) plus the QCP callback nodes (``cpq_qcp_method``).
    Returns ``{target, steps, text}``.
    """
    target = resolve_node(G, quote_object)
    steps: list[dict] = []
    if target is not None:
        for u, v, d in G.in_edges(target, data=True):
            if d.get("relation") == "cpq_applies_to":
                steps.append({
                    "rule": G.nodes[u].get("label", u),
                    "execution_order": d.get("execution_order"),
                    "rule_type": d.get("sf_cpq_rule_type"),
                })
    for nid, d in G.nodes(data=True):
        if d.get("file_type") == "cpq_qcp_method":
            steps.append({"rule": d.get("label", nid),
                          "execution_order": None, "qcp_method": d.get("sf_qcp_method")})
    steps.sort(key=lambda s: (s.get("execution_order") is None, s.get("execution_order") or 0))

    lines = [
        f"{i + 1}. order={s.get('execution_order')} {sanitize_label(str(s['rule']))}"
        f"{(' [' + sanitize_label(str(s.get('qcp_method'))) + ']') if s.get('qcp_method') else ''}"
        for i, s in enumerate(steps)
    ]
    text = "\n".join(lines) or f"No CPQ chain found for {quote_object!r}."
    char_budget = token_budget * 3
    if len(text) > char_budget:
        text = text[:char_budget] + "\n... (truncated by token budget)"
    return {"target": target, "steps": steps,
            "text": f"CPQ Calc Engine chain ({len(steps)} steps)\n\n{text}"}


def sf_ooe(G: nx.DiGraph, sobject: str, *, token_budget: int = 2000) -> dict:
    """Order-of-Execution chain for *sobject* (ordered ``order_of_execution`` edges)."""
    target = resolve_node(G, sobject)
    chain_edges = [
        (u, v) for u, v, d in G.edges(data=True)
        if d.get("relation") == "order_of_execution"
        and (target is None or sobject.lower() in u.lower() or sobject.lower() in v.lower())
    ]
    nodes = sorted({n for e in chain_edges for n in e})
    text = _render(G, nodes, chain_edges, token_budget=token_budget) if chain_edges \
        else f"No Order-of-Execution chain found for {sobject!r}."
    return {"target": target, "edges": chain_edges,
            "text": f"Order of Execution ({len(chain_edges)} steps)\n\n{text}"}
