"""
graphify-sf: Flow ↔ CPQ infinite-loop detection pass (ADR-029).

Graph-wide diagnostic pass that flags the classic CPQ feedback loop: a
Record-Triggered Flow updates a Quote (or QuoteLine) → CPQ's Calc Engine saves
the Quote → the save re-fires the Record-Triggered Flow → … This step covers
**Type A (Direct)** only:

    Type A — Direct: a Record-Triggered Flow performs DML on a Quote object,
             so its own save re-triggers it. Severity CRITICAL (ADR-029).

Type B (Flow → Workflow → Quote) is intentionally out of scope here — Workflow
loop analysis lands in step 5. Conditional (Type C) Decision analysis is not
attempted: only static (declared trigger + declared DML) signals are used.

Safe-pattern recognition (ADR-029 "방지 패턴 인식"): when a flow carries a
recursion-prevention marker (a record-tracking ``Set<Id>``, an ``isFirstRun``
flag, a ``RecursionFlag__c`` custom flag, or an ``ISCHANGED`` entry-condition
guard) the loop is still reported, but tagged ``sf_loop_prevention=True`` and
downgraded to INFO severity — the cycle exists structurally but is guarded.

Each detected loop becomes an ``infinite_loop_risk`` self-edge (flow -> flow,
registered in validate_sf.py), tagged ``INFERRED`` (0.85): the structural cycle
is real, but whether it runs unbounded depends on runtime values (ADR-029).

CRITICAL (ADR-016): graph-wide O(N) pass, NOT cached per file — always re-run on
the full merged graph. Like ``permission_analysis_pass`` it is a pure function:
it returns the diagnostic edges for the caller to ``all_edges.extend(...)`` and
mutates neither input list.
"""

from __future__ import annotations

import re

#: A DML target whose node ID contains this token is treated as a CPQ Quote
#: object (``sobject_sbqq__quote__c`` / ``…quoteline__c`` both match).
_QUOTE_TOKEN = "quote"

#: Recursion-prevention markers (ADR-029 "방지 패턴 인식"). A flow whose string
#: attributes match any of these is considered loop-guarded — the risk edge is
#: still emitted but flagged ``sf_loop_prevention`` and downgraded to INFO.
_PREVENTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ProcessedQuoteIds", re.IGNORECASE),       # Set<Id> tracking
    re.compile(r"\bisFirstRun\b", re.IGNORECASE),          # first-run flag
    re.compile(r"RecursionFlag", re.IGNORECASE),           # custom flag
    re.compile(r"\bISCHANGED\s*\(", re.IGNORECASE),        # entry-condition guard
]


def _is_record_triggered(node: dict) -> bool:
    """Return True if *node* is a Record-Triggered Flow.

    The Flow parser sets ``sf_trigger_type`` from ``<recordTriggerType>`` only
    for record-triggered flows (Create / Update / CreateAndUpdate / Delete);
    Screen / plain AutoLaunched flows leave it empty (flow.py).
    """
    return bool(node.get("sf_trigger_type"))


def _has_loop_prevention(node: dict) -> bool:
    """Return True if the flow carries a recursion-prevention marker.

    Honors an explicit ``sf_loop_prevention`` boolean first (an upstream parser
    may have set it), then scans the flow node's string attribute values for any
    known prevention pattern (ADR-029).
    """
    if node.get("sf_loop_prevention"):
        return True
    for value in node.values():
        if isinstance(value, str) and any(
            pat.search(value) for pat in _PREVENTION_PATTERNS
        ):
            return True
    return False


def detect_flow_cpq_loops(
    all_nodes: list[dict], all_edges: list[dict]
) -> list[dict]:
    """Detect Flow → CPQ → Quote-save → Flow Type A (Direct) loops.

    A Record-Triggered Flow that performs DML on a Quote object re-triggers
    itself once CPQ saves the recalculated Quote. One ``infinite_loop_risk``
    self-edge is emitted per such flow (deduplicated across multiple Quote DML
    sites). Loop-guarded flows are still reported, tagged ``sf_loop_prevention``
    and downgraded from CRITICAL to INFO severity.

    Pure function: neither list is mutated; the caller does
    ``all_edges.extend(...)``. Self-edges reference an existing flow node, so the
    result carries no dangling edges.

    Args:
        all_nodes: Merged node list (read-only).
        all_edges: Merged edge list (read-only).

    Returns:
        List of ``infinite_loop_risk`` diagnostic self-edges (one per flow).
    """
    # Flow nodes that are Record-Triggered, indexed by ID for self-edge emission.
    record_triggered_flows = {
        node["id"]: node
        for node in all_nodes
        if node.get("file_type") == "flow" and _is_record_triggered(node)
    }
    if not record_triggered_flows:
        return []

    # Record-Triggered flows that perform DML on a Quote object (dedup per flow).
    quote_updating_flows: set[str] = set()
    for edge in all_edges:
        if edge.get("relation") != "dml_operates_on":
            continue
        source = edge.get("source", "")
        if source not in record_triggered_flows:
            continue
        if _QUOTE_TOKEN in str(edge.get("target", "")).lower():
            quote_updating_flows.add(source)

    loops: list[dict] = []
    for flow_id in sorted(quote_updating_flows):
        guarded = _has_loop_prevention(record_triggered_flows[flow_id])
        loops.append(
            {
                "source": flow_id,
                "target": flow_id,
                "relation": "infinite_loop_risk",
                "confidence": "INFERRED",
                "confidence_value": 0.85,
                "sf_loop_type": "A_DIRECT",
                "sf_severity": "INFO" if guarded else "CRITICAL",
                "sf_loop_prevention": guarded,
                "sf_reason": (
                    "Record-Triggered Flow updates Quote -> CPQ saves Quote -> "
                    "Flow triggered again"
                ),
            }
        )

    return loops
