"""
graphify-sf: Order of Execution (OoE) analysis pass.

Graph-wide pass (Pass SF-3) that materializes the Salesforce Order of Execution
as an explicit 18-step chain per SObject that *actually triggers* the execution
order — i.e. an SObject with an Apex trigger or a Validation Rule attached
(ADR-005, ADR-017). SObjects referenced only by SOQL are deliberately excluded:
``SELECT`` does not run the Order of Execution, and generating 18 nodes for every
queried standard object (Account, Contact, … referenced in hundreds of files)
would explode the graph.

For each qualifying SObject this pass:
    1. Creates 18 ``concept`` nodes, one per OoE step (``SALESFORCE_OOE_STEPS``).
    2. Chains them step N -> step N+1 with ``order_of_execution`` edges, and
       anchors the chain to its SObject (SObject -> step 1) so the steps are
       reachable from the object they belong to.

The resulting ``order_of_execution`` subgraph is a DAG (enforced by
``validate_sf.validate_sf_graph`` check 2).

CRITICAL (ADR-016): graph-wide O(N) pass, NOT cached per file — always re-run on
the full merged graph. It mutates ``all_nodes`` / ``all_edges`` IN PLACE and
returns ``None`` (it never replaces the list objects, since callers hold
references), matching ``cpq_analysis_pass``. Running it twice is a no-op for
SObjects that already have a chain.
"""

from __future__ import annotations

from graphify.salesforce.constants import SALESFORCE_OOE_STEPS

#: Edge relations whose *target* SObject qualifies for an OoE chain (ADR-005).
#: SOQL ``queries`` / ``dml_operates_on`` are intentionally absent: a query does
#: not run the Order of Execution, so SOQL-only SObjects get no chain.
_OOE_TRIGGERING_RELATIONS = {"triggers_on", "validates"}


def ooe_analysis_pass(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Generate an Order of Execution chain for each triggered SObject.

    Modifies ``all_nodes`` and ``all_edges`` in place (returns ``None``):

        - For every SObject targeted by a ``triggers_on`` or ``validates`` edge,
          appends 18 ``concept`` OoE-step nodes.
        - Chains those steps with ``order_of_execution`` edges and anchors the
          chain to its SObject node.

    Called after ``extract()`` and ``cpq_analysis_pass()`` (so CPQ SObject types
    are settled first — ADR-011), before ``build_graph()``.

    Args:
        all_nodes: Merged node list across all extracted files (mutated).
        all_edges: Merged edge list across all extracted files (mutated).
    """
    # 1. Collect SObjects that actually trigger the Order of Execution.
    sobjects: set[str] = set()
    for edge in all_edges:
        if edge.get("relation") in _OOE_TRIGGERING_RELATIONS:
            target = edge.get("target", "")
            if target.startswith("sobject_"):
                sobjects.add(target)

    if not sobjects:
        return

    # Human-readable SObject labels + re-run guard (skip already-built chains).
    labels = {n["id"]: n.get("label", n["id"]) for n in all_nodes}
    existing_ids = {n["id"] for n in all_nodes}

    # 2. Build the 18-step chain per qualifying SObject (sorted for determinism).
    for sobject_id in sorted(sobjects):
        if f"ooe_{sobject_id}_1" in existing_ids:
            continue  # chain already materialized — keep the pass idempotent

        sobject_label = labels.get(sobject_id, sobject_id)
        step_ids: list[str] = []
        for step_num, step_desc, _trigger_type in SALESFORCE_OOE_STEPS:
            node_id = f"ooe_{sobject_id}_{step_num}"
            step_ids.append(node_id)
            all_nodes.append(
                {
                    "id": node_id,
                    "label": f"{sobject_label}: {step_desc}",
                    "file_type": "concept",
                    "source_file": "",
                    "sf_ooe_step": step_num,
                    "sf_ooe_sobject": sobject_id,
                }
            )

        # Anchor the chain to its SObject, then link step N -> step N+1.
        all_edges.append(
            {
                "source": sobject_id,
                "target": step_ids[0],
                "relation": "order_of_execution",
                "confidence": "EXTRACTED",
                "source_file": "",
            }
        )
        for src, dst in zip(step_ids, step_ids[1:]):
            all_edges.append(
                {
                    "source": src,
                    "target": dst,
                    "relation": "order_of_execution",
                    "confidence": "EXTRACTED",
                    "source_file": "",
                }
            )
