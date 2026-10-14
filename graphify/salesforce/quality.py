"""
graphify-sf: graph summary + integrity report (``summary.json``).

``graph_summary`` condenses an enriched SF graph into a small, deterministic
dict: node / edge counts by type, stub and unresolved-reference inventories, and
integrity counters that must stay at zero for the graph to be a trustworthy base
for an ontology (stable IDs, one owner per field, no source text, portable
paths). It is cheap to commit and diff daily, unlike the full ``graph.json``.

Pure function over the graph — no file system access, no mcp import.
"""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath

import networkx as nx

#: Integrity counters that must be zero (see ``integrity_errors``).
_INTEGRITY_KEYS = (
    "absolute_source_files",
    "nodes_with_source_text",
    "fields_with_multiple_objects",
    "apex_label_mismatches",
    "cpq_rules_without_rule_data",
    "nodes_missing_qualified_name",
)

#: Node types an ontology maps onto; each must carry ``sf_qualified_name``.
_QUALIFIED_TYPES = frozenset(
    {"sobject", "field", "flow", "lwc_component", "aura_component", "validation_rule",
     "flexipage", "experience_page", "experience_site", "record_type", "layout",
     "assignment_rule", "escalation_rule", "auto_response_rule", "duplicate_rule",
     "matching_rule", "auth_provider"}
)


def _edges(G: nx.Graph):
    """``(u, v, data)`` for every edge, multigraph or not."""
    return G.edges(data=True)


def graph_summary(G: nx.Graph) -> dict:
    """Summarize *G* (see module docstring). Lists are sorted for stable diffs."""
    nodes = dict(G.nodes(data=True))
    by_type = Counter(d.get("file_type", "") for d in nodes.values())
    by_relation = Counter(d.get("relation", "") for _, _, d in _edges(G))
    stubs = Counter(d.get("file_type", "") for d in nodes.values() if d.get("sf_stub"))

    field_owners: Counter = Counter(
        u for u, _, d in _edges(G) if d.get("relation") == "field_of"
    )
    apex_mismatch = sorted(
        n for n, d in nodes.items()
        if d.get("sf_code_type") in ("class", "trigger", "interface", "enum")
        and not d.get("sf_stub")
        and d.get("source_file")
        and d.get("label") != PurePosixPath(d["source_file"]).name.split(".")[0]
    )

    integrity = {
        "absolute_source_files": sum(
            1 for d in (*nodes.values(), *(e for _, _, e in _edges(G)))
            if str(d.get("source_file", "")).startswith("/")
        ),
        "nodes_with_source_text": sum(
            1 for d in nodes.values() if "source" in d or "_source" in d
        ),
        "fields_with_multiple_objects": sum(1 for c in field_owners.values() if c > 1),
        "apex_label_mismatches": len(apex_mismatch),
        "cpq_rules_without_rule_data": sum(
            1 for d in nodes.values()
            if d.get("file_type") == "cpq_rule" and d.get("source_file") != "cpq_data"
        ),
        "nodes_missing_qualified_name": sum(
            1 for d in nodes.values()
            if not d.get("sf_qualified_name")
            and (
                d.get("file_type") in _QUALIFIED_TYPES
                or d.get("sf_code_type") in ("class", "trigger", "interface", "enum")
                or d.get("sf_method_type") == "method"
            )
        ),
    }

    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "nodes_by_type": dict(sorted(by_type.items())),
        "edges_by_relation": dict(sorted(by_relation.items())),
        "stubs_by_type": dict(sorted(stubs.items())),
        "unresolved_fields": sorted(
            n for n, d in nodes.items()
            if d.get("file_type") == "field" and d.get("sf_unresolved")
        ),
        "ambiguous_edges": sum(
            1 for _, _, d in _edges(G)
            if d.get("sf_ambiguous") or d.get("confidence") == "AMBIGUOUS"
        ),
        "dynamic_soql_sites": sum(1 for _, _, d in _edges(G) if d.get("sf_dynamic")),
        "integrity": integrity,
    }


def integrity_errors(summary: dict) -> list[str]:
    """Human-readable list of non-zero integrity counters in *summary*."""
    return [
        f"{key}={summary['integrity'][key]}"
        for key in _INTEGRITY_KEYS
        if summary["integrity"].get(key)
    ]
