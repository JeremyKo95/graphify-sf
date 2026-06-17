"""
graphify-sf: Governor Limit analysis pass (Pass SF-4).

Graph-wide diagnostic pass that flags Salesforce Governor Limit risks the Apex
parser already located. It does NOT re-parse source — it reads the ``sf_in_loop``
flags the parser tagged onto ``queries`` / ``dml_operates_on`` edges (see
apex_enhanced.py) and turns the risky ones into ``governor_violation`` edges:

    - SOQL-in-loop: a ``queries`` edge with ``sf_in_loop=True`` (limit 100/txn).
    - DML-in-loop:  a ``dml_operates_on`` edge with ``sf_in_loop=True`` (150/txn).

ADR-004: violations are expressed as graph *edges* pointing at a per-type
*sentinel* ``concept`` node (``gov_limit_soql_in_loop`` / ``gov_limit_dml_in_loop``),
NOT a separate report. Exactly one sentinel node is created per violation type no
matter how many offenders exist (CLAUDE.md: prevent node explosion). Multiple
in-loop sites in the same method aggregate into a single edge with
``sf_violation_count``.

CRITICAL (ADR-006): limit values come *only* from ``constants.GOVERNOR_LIMITS``;
they are never recomputed or estimated here (static analysis only — ADR-019).

CRITICAL (ADR-016): graph-wide O(N) pass, NOT cached per file — always re-run on
the full merged graph. Unlike ``cpq_analysis_pass`` / ``ooe_analysis_pass`` (which
return ``None``), this pass *returns* the diagnostic edges for the caller to
``all_edges.extend(...)`` — matching the pipeline order in ARCHITECTURE.md. It
appends sentinel ``concept`` nodes to ``all_nodes`` IN PLACE (guarded so a re-run
never duplicates them), so that the returned edges are never dangling.
"""

from __future__ import annotations

from graphify.salesforce.constants import GOVERNOR_LIMITS

#: In-loop governor violations, keyed by the edge relation that carries the
#: ``sf_in_loop`` flag. One sentinel ``concept`` node per entry (ADR-004).
_IN_LOOP_VIOLATIONS: dict[str, dict] = {
    "queries": {
        "violation_type": "soql_in_loop",
        "sentinel_id": "gov_limit_soql_in_loop",
        "sentinel_label": "⚠ SOQL-in-loop",
        "limit": GOVERNOR_LIMITS["soql_queries_per_transaction"],
    },
    "dml_operates_on": {
        "violation_type": "dml_in_loop",
        "sentinel_id": "gov_limit_dml_in_loop",
        "sentinel_label": "⚠ DML-in-loop",
        "limit": GOVERNOR_LIMITS["dml_statements_per_transaction"],
    },
}


def governor_limit_analysis_pass(
    all_nodes: list[dict], all_edges: list[dict]
) -> list[dict]:
    """Detect Governor Limit violations on the merged graph.

    For each in-loop SOQL / DML site (``sf_in_loop=True``), emits a
    ``governor_violation`` edge from the offending method to the matching
    per-type sentinel node, aggregating multiple sites in one method into a
    single edge (``sf_violation_count``).

    Sentinel ``concept`` nodes are appended to ``all_nodes`` in place — one per
    violation type that actually occurs — so the returned edges resolve cleanly
    in ``build_graph()``. Called after ``extract()`` / the CPQ / OoE passes;
    the caller does ``all_edges.extend(violations)`` (ADR-011 pipeline order).

    Args:
        all_nodes: Merged node list (mutated: sentinel nodes appended).
        all_edges: Merged edge list (read-only here).

    Returns:
        List of ``governor_violation`` diagnostic edges to add to the graph.
    """
    violations: list[dict] = []
    existing_ids = {n["id"] for n in all_nodes}

    for relation, spec in _IN_LOOP_VIOLATIONS.items():
        # Aggregate in-loop sites per offending method (source node).
        offenders: dict[str, dict] = {}
        for edge in all_edges:
            if edge.get("relation") != relation or not edge.get("sf_in_loop"):
                continue
            source = edge.get("source", "")
            info = offenders.setdefault(
                source,
                {
                    "count": 0,
                    "source_file": edge.get("source_file", ""),
                    "source_location": edge.get("source_location", ""),
                },
            )
            info["count"] += 1

        if not offenders:
            continue

        # One sentinel node per violation type (idempotent across re-runs).
        sentinel_id = spec["sentinel_id"]
        if sentinel_id not in existing_ids:
            all_nodes.append(
                {
                    "id": sentinel_id,
                    "label": spec["sentinel_label"],
                    "file_type": "concept",
                    "source_file": "",
                    "sf_violation_type": spec["violation_type"],
                }
            )
            existing_ids.add(sentinel_id)

        for method_id in sorted(offenders):
            info = offenders[method_id]
            violations.append(
                {
                    "source": method_id,
                    "target": sentinel_id,
                    "relation": "governor_violation",
                    "confidence": "EXTRACTED",
                    "sf_violation_type": spec["violation_type"],
                    "sf_violation_count": info["count"],
                    "sf_severity": "HIGH",
                    "sf_limit": spec["limit"],
                    "sf_in_loop": True,
                    "source_location": info["source_location"],
                    "source_file": info["source_file"],
                }
            )

    return violations
