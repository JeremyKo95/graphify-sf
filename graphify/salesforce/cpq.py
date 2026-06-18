"""
graphify-sf: CPQ analysis pass (Quote Calc Engine mapping).

Graph-wide analysis pass that runs *after* per-file extraction and *before*
``build_graph()`` (Pass SF-1 in the pipeline; see ARCHITECTURE.md "분석 패스
실행 순서"). It enriches the merged graph with Salesforce CPQ (SteelBrick)
semantics:

    1. Reclassify SBQQ__ objects: nodes whose label carries the CPQ managed
       package prefix become ``cpq_rule`` (so OoE / Neo4j export treat them as
       CPQ rules, not generic SObjects).
    2. Detect QCP implementations: Apex classes implementing
       ``SBQQ.QuoteCalculatorPlugin`` are tagged, and one ``cpq_qcp_method`` node
       is created per QCP callback found in the source — these model the Calc
       Engine call order (ADR-025).
    3. Annotate ``cpq_applies_to`` edges with an ``execution_order`` matching the
       CPQ Calc Engine sequence (Product Rules -> Price Rules -> QCP hooks).

CRITICAL (ADR-016): this is a graph-wide O(N) pass, NOT cached per file. It is
always re-run on the full merged graph.

The pass mutates ``all_nodes`` / ``all_edges`` IN PLACE and returns ``None``.
It never replaces the list objects (callers hold references) — see ADR-011/016.
This keeps it composable with the other analysis passes that append to the same
lists.
"""

from __future__ import annotations

import re

from graphify.salesforce.constants import (
    CPQ_QCP_INTERFACE,
    CPQ_QCP_METHODS,
    CPQ_RULE_PREFIX,
)

#: Field WRITES inside QCP plugin Apex. Two idioms the Calc Engine plugins use:
#:   1. ``line.put('SBQQ__Discount__c', v)`` / ``.put("Discount__c", v)``
#:   2. ``quoteLine.SBQQ__Discount__c = v`` (direct custom-field assignment;
#:      ``=`` not ``==``, custom ``__c`` / ``__r`` fields only to stay precise).
_QCP_PUT_RE = re.compile(r"\.put\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")
_QCP_ASSIGN_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*__[rc])\s*=(?!=)")

#: QCP plugins operate on the Quote / QuoteLine; the object recorded for
#: CPQ↔Validation overlap matching (validation_cpq, ADR-030).
_QCP_CPQ_OBJECT = "SBQQ__Quote__c"


def _extract_qcp_field_writes(source: str) -> list[str]:
    """Extract the fields a QCP plugin writes from its Apex source.

    Best-effort static parse (ADR-019 regex-first): returns the distinct field
    API names assigned via ``.put('Field')`` or ``obj.Field__c =``. Dynamic
    field names (``.put(varName)``) are unresolved and intentionally skipped.
    """
    fields: list[str] = []
    seen: set[str] = set()
    for match in (*_QCP_PUT_RE.finditer(source), *_QCP_ASSIGN_RE.finditer(source)):
        field = match.group(1)
        if field not in seen:
            seen.add(field)
            fields.append(field)
    return fields

#: CPQ Calc Engine execution order (ADR-025, PRD "CPQ Calc Engine 호출 순서").
#: Maps a rule-class / QCP-callback name fragment to its order in the chain.
CPQ_CALC_ENGINE_ORDER: dict[str, int] = {
    "Product Rules": 1,
    "Price Rules": 2,
    "onBeforePriceRules": 3,
    "calculate": 4,
    "onAfterPriceRules": 5,
    "onAfterCalculate": 6,
    "Line Items": 7,
}

#: Default execution order for a cpq_applies_to edge whose rule type is unknown.
#: Price Rules is the most common applies-to target (PRD).
_DEFAULT_EXECUTION_ORDER = CPQ_CALC_ENGINE_ORDER["Price Rules"]


def cpq_analysis_pass(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Analyze CPQ rules and QCP implementations on the merged graph.

    Modifies ``all_nodes`` and ``all_edges`` in place (returns ``None``):

        - SBQQ__ prefixed nodes are reclassified to ``cpq_rule``.
        - QCP ``SBQQ.QuoteCalculatorPlugin`` implementations are detected and a
          ``cpq_qcp_method`` node is added per implemented callback.
        - ``cpq_applies_to`` edges receive an ``execution_order`` attribute.

    Called after ``extract()``, before ``build_graph()``.

    Args:
        all_nodes: Merged node list across all extracted files (mutated).
        all_edges: Merged edge list across all extracted files (mutated).
    """
    # 1. Reclassify SBQQ__ nodes to cpq_rule -----------------------------------
    # Use the explicit ``SBQQ__`` prefix (not a bare ``SBQQ`` startswith): the
    # latter would wrongly catch the ``SBQQ.QuoteCalculatorPlugin`` interface
    # node emitted by the Apex parser.
    for node in all_nodes:
        label = node.get("label", "")
        if CPQ_RULE_PREFIX in label:
            node["file_type"] = "cpq_rule"
            node["sf_cpq_object"] = label

    # 2. Detect QCP interface implementations ----------------------------------
    # Iterate over a snapshot of the current code nodes: we append new
    # ``cpq_qcp_method`` nodes below and must not re-scan them.
    code_nodes = [n for n in all_nodes if n.get("file_type") == "code"]
    for node in code_nodes:
        source = node.get("source", "")
        if not source:
            continue
        if (
            CPQ_QCP_INTERFACE in source
            or "implements QuoteCalculatorPlugin" in source
        ):
            class_id = node["id"]
            node["sf_qcp_implementation"] = True

            # Record the fields the plugin writes so the CPQ ↔ Validation overlap
            # pass (validation_cpq, ADR-030) can treat the QCP as a CPQ writer.
            field_writes = _extract_qcp_field_writes(source)
            if field_writes:
                node["sf_target_fields"] = field_writes
                node.setdefault("sf_cpq_object", _QCP_CPQ_OBJECT)

            for method_name in CPQ_QCP_METHODS:
                if method_name not in source:
                    continue
                all_nodes.append(
                    {
                        "id": f"{class_id}_qcp_{method_name}",
                        "label": f"{node.get('label', class_id)}.{method_name}()",
                        "file_type": "cpq_qcp_method",
                        "source_file": node.get("source_file", ""),
                        "sf_qcp_method": method_name,
                        "sf_qcp_class": class_id,
                    }
                )

    # 3. Annotate cpq_applies_to edges with Calc Engine execution order --------
    # Precedence (Step 2.3): a real ``SBQQ__EvaluationOrder__c`` from CPQ data
    # (``sf_eval_order`` on the rule) wins; then the rule's declared
    # ``sf_cpq_rule_type``; then a label-substring heuristic for metadata-only
    # rules; finally a default.
    cpq_rules = {
        n["id"]: n for n in all_nodes if n.get("file_type") == "cpq_rule"
    }
    for edge in all_edges:
        if edge.get("relation") != "cpq_applies_to":
            continue
        rule = cpq_rules.get(edge.get("source", ""))
        eval_order = rule.get("sf_eval_order") if rule else None
        if isinstance(eval_order, (int, float)):
            edge["execution_order"] = int(eval_order)
            continue
        rule_type = rule.get("sf_cpq_rule_type") if rule else None
        label = rule.get("label", "") if rule else ""
        if rule_type == "Product" or "Product" in label:
            edge["execution_order"] = CPQ_CALC_ENGINE_ORDER["Product Rules"]
        elif rule_type == "Price" or "Price" in label:
            edge["execution_order"] = CPQ_CALC_ENGINE_ORDER["Price Rules"]
        else:
            edge["execution_order"] = _DEFAULT_EXECUTION_ORDER
