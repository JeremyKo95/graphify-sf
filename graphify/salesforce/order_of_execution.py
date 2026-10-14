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
    3. Attaches every active trigger, record-triggered flow, process, validation
       / assignment / escalation / auto-response / duplicate rule, workflow and
       criteria-based sharing rule to the step it runs in (``runs_in`` edge).

The resulting ``order_of_execution`` subgraph is a DAG (enforced by
``validate_sf.validate_sf_graph`` check 2).

CRITICAL (ADR-016): graph-wide O(N) pass, NOT cached per file — always re-run on
the full merged graph. It mutates ``all_nodes`` / ``all_edges`` IN PLACE and
returns ``None`` (it never replaces the list objects, since callers hold
references), matching ``cpq_analysis_pass``. Running it twice is a no-op for
SObjects that already have a chain.
"""

from __future__ import annotations

from graphify.salesforce.constants import SALESFORCE_OOE_STEPS, sobject_nid

#: Edge relations whose *target* SObject qualifies for an OoE chain (ADR-005).
#: SOQL ``queries`` / ``dml_operates_on`` are intentionally absent: a query does
#: not run the Order of Execution, so SOQL-only SObjects get no chain.
_OOE_TRIGGERING_RELATIONS = {"triggers_on", "validates"}

#: OoE step number per ``component_kind`` (third field of SALESFORCE_OOE_STEPS).
_STEP_BY_KIND = {kind: num for num, _desc, kind in SALESFORCE_OOE_STEPS}

#: Flow ``<start><triggerType>`` -> component kind.
_FLOW_KINDS = {"RecordBeforeSave": "flow_before_save", "RecordAfterSave": "flow_after_save"}

#: ``rule_of`` source types that run in a save, by file type (= component kind).
_RULE_KINDS = frozenset({"assignment_rule", "auto_response_rule", "escalation_rule",
                         "duplicate_rule"})


def _components(all_nodes: list[dict], all_edges: list[dict]):
    """Yield ``(component node, SObject id, component_kind)`` for every active
    automation that runs while a record of that SObject is saved."""
    by_id = {n["id"]: n for n in all_nodes}
    for edge in all_edges:
        node = by_id.get(edge.get("source"))
        target = edge.get("target", "")
        if node is None or not target.startswith("sobject_"):
            continue
        relation = edge.get("relation")
        if relation == "triggers_on" and node.get("sf_code_type") == "trigger":
            phases = {e.split()[0] for e in edge.get("sf_trigger_events", [])}
            for phase in sorted(phases & {"before", "after"}):
                yield node, target, f"trigger_{phase}"
        elif relation == "triggers_on" and node.get("file_type") == "flow":
            if node.get("sf_status") not in (None, "Active"):
                continue
            kind = ("process_builder" if node.get("sf_process_type") == "Workflow"
                    else _FLOW_KINDS.get(node.get("sf_flow_trigger_type")))
            if kind:
                yield node, target, kind
        elif relation == "validates" and node.get("sf_active") is not False:
            yield node, target, "validation_rule"
        elif (relation == "rule_of" and node.get("file_type") in _RULE_KINDS
              and node.get("sf_active")):
            yield node, target, node["file_type"]
        elif (relation == "shares" and node.get("sf_rule_type") == "sharingCriteriaRules"):
            yield node, target, "sharing"
    for node in all_nodes:
        if node.get("file_type") == "workflow" and node.get("sf_active_rules"):
            yield node, sobject_nid(node["sf_object"]), "workflow_rule"


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
    existing_ids = {n["id"] for n in all_nodes}
    components = [(node, sobject, kind)
                  for node, sobject, kind in _components(all_nodes, all_edges)
                  if sobject in existing_ids]
    sobjects: set[str] = {sobject for _node, sobject, _kind in components}
    for edge in all_edges:
        if edge.get("relation") in _OOE_TRIGGERING_RELATIONS:
            target = edge.get("target", "")
            if target.startswith("sobject_"):
                sobjects.add(target)

    if not sobjects:
        return

    # Human-readable SObject labels + re-run guard (skip already-built chains).
    labels = {n["id"]: n.get("label", n["id"]) for n in all_nodes}

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

    # 3. Attach each running component to its step (idempotent).
    attached = {(e["source"], e["target"]) for e in all_edges
                if e.get("relation") == "runs_in"}
    for node, sobject_id, kind in components:
        step_id = f"ooe_{sobject_id}_{_STEP_BY_KIND[kind]}"
        if (node["id"], step_id) in attached:
            continue
        attached.add((node["id"], step_id))
        all_edges.append(
            {
                "source": node["id"],
                "target": step_id,
                "relation": "runs_in",
                "confidence": "EXTRACTED",
                "source_file": node.get("source_file", ""),
            }
        )
