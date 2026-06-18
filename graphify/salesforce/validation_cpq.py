"""
graphify-sf: CPQ Rule ↔ Validation Rule field-overlap pass (ADR-030).

Graph-wide diagnostic pass that flags the classic CPQ/Validation conflict: a CPQ
Price/Product Rule *writes* a field on a Quote object while a Validation Rule on
the **same object** *checks* that field. Because Validation runs at Order-of-
Execution step 13 (Custom Validation) — after the CPQ Calc Engine has already
recalculated and written the field — an overlapping field is a likely source of
"CPQ set a value the Validation Rule then rejects" save failures (ADR-030).

Each overlap becomes a ``cpq_validation_risk`` edge (CPQ rule -> Validation rule,
registered in validate_sf.py) tagged ``INFERRED`` (0.7): the field overlap is
structurally real, but whether it actually fails depends on the runtime values
and on the Validation Rule's formula logic, which static analysis cannot resolve
(ADR-030 limitation).

Field-data contract (read-only; the pass never invents fields):
    - A ``cpq_rule`` node exposes the fields it writes via ``sf_target_fields``
      (a list of field API names) and its SObject via ``sf_cpq_object`` / label.
    - A ``validation_rule`` node exposes the fields its formula references via
      ``sf_referenced_fields`` (a list), and its SObject via ``sf_object`` or the
      target of its outgoing ``validates`` edge.
    - Field names are compared on their bare token (the part after the last
      ``.``), case-insensitively, so ``SBQQ__Quote__c.Discount__c`` and
      ``Discount__c`` match.
    - A conflict requires BOTH rules to sit on the same SObject AND share at
      least one field. When either side carries no field data the pass yields
      nothing for that pair (no false positives from missing data).

CRITICAL (ADR-016): graph-wide O(N) pass, NOT cached per file — always re-run on
the full merged graph. Like ``permission_analysis_pass`` / ``detect_flow_cpq_loops``
it is a pure function: it returns the diagnostic edges for the caller to
``all_edges.extend(...)`` and mutates neither input list.
"""

from __future__ import annotations

import re

#: Matches a Salesforce CPQ (SteelBrick) object API name token, e.g.
#: ``SBQQ__Quote__c``. Used to normalize a cpq_rule's object name.
_SBQQ_TOKEN_RE = re.compile(r"SBQQ__\w+")


def _bare_field(field: str) -> str:
    """Return the bare, lower-cased field token (drops an ``Object.`` prefix)."""
    return field.rsplit(".", 1)[-1].strip().lower()


def _cpq_object_name(node: dict) -> str | None:
    """Return the ``SBQQ__`` object API name a cpq_rule node represents.

    Reads ``sf_cpq_object`` (set by cpq_analysis_pass) falling back to ``label``,
    and extracts the first ``SBQQ__…`` token. Returns ``None`` when absent.
    """
    text = node.get("sf_cpq_object") or node.get("label", "")
    match = _SBQQ_TOKEN_RE.search(text)
    return match.group(0) if match else None


def _validation_object(node: dict, validates_targets: dict[str, str]) -> str | None:
    """Return the SObject a validation_rule node validates.

    Prefers the explicit ``sf_object`` attribute, then falls back to the target
    of the node's outgoing ``validates`` edge (``validates_targets`` maps a
    validation_rule node ID to that target SObject node ID).
    """
    explicit = node.get("sf_object")
    if explicit:
        return str(explicit)
    return validates_targets.get(node["id"])


def _normalize_object(value: str | None) -> str | None:
    """Normalize an object identifier for comparison.

    CPQ object names arrive as API names (``SBQQ__Quote__c``) while a ``validates``
    edge target is a node ID (``sobject_sbqq__quote__c``). Lower-casing and
    stripping the ``sobject_`` prefix lets the two forms compare equal.
    """
    if value is None:
        return None
    token = value.lower()
    if token.startswith("sobject_"):
        token = token[len("sobject_"):]
    return token


def validation_cpq_analysis_pass(
    all_nodes: list[dict], all_edges: list[dict]
) -> list[dict]:
    """Detect CPQ Rule ↔ Validation Rule field overlaps on the same SObject.

    Emits one ``cpq_validation_risk`` edge per (cpq_rule, validation_rule) pair
    that targets the same SObject and shares at least one field. Overlapping
    field names are reported (sorted, de-duplicated) in ``sf_overlapping_fields``.

    Pure function: neither list is mutated; the caller does
    ``all_edges.extend(...)``. Both endpoints reference existing nodes, so the
    result carries no dangling edges.

    Args:
        all_nodes: Merged node list (read-only).
        all_edges: Merged edge list (read-only).

    Returns:
        List of ``cpq_validation_risk`` diagnostic edges (possibly empty).
    """
    # validation_rule -> SObject (node ID) from outgoing ``validates`` edges.
    validates_targets: dict[str, str] = {
        edge["source"]: edge["target"]
        for edge in all_edges
        if edge.get("relation") == "validates"
    }

    # CPQ writers: (id, normalized object, {bare field tokens it writes}). A
    # writer is a ``cpq_rule`` node OR a QCP plugin (``sf_qcp_implementation``),
    # since file-based analysis sees CPQ field writes in the QCP Apex (cpq.py),
    # not in the Price Rule data records.
    cpq_rules: list[tuple[str, str, set[str]]] = []
    for node in all_nodes:
        is_cpq_writer = (
            node.get("file_type") == "cpq_rule"
            or node.get("sf_qcp_implementation")
        )
        if not is_cpq_writer:
            continue
        obj = _normalize_object(_cpq_object_name(node))
        fields = {
            _bare_field(f) for f in node.get("sf_target_fields", []) if f
        }
        if obj and fields:
            cpq_rules.append((node["id"], obj, fields))

    # Validation rules: (id, normalized object, {bare field tokens it checks}).
    validation_rules: list[tuple[str, str, set[str]]] = []
    for node in all_nodes:
        if node.get("file_type") != "validation_rule":
            continue
        obj = _normalize_object(_validation_object(node, validates_targets))
        fields = {
            _bare_field(f) for f in node.get("sf_referenced_fields", []) if f
        }
        if obj and fields:
            validation_rules.append((node["id"], obj, fields))

    if not cpq_rules or not validation_rules:
        return []

    risk_edges: list[dict] = []
    for cpq_id, cpq_obj, cpq_fields in cpq_rules:
        for val_id, val_obj, val_fields in validation_rules:
            if cpq_obj != val_obj:
                continue
            overlap = cpq_fields & val_fields
            if not overlap:
                continue
            risk_edges.append(
                {
                    "source": cpq_id,
                    "target": val_id,
                    "relation": "cpq_validation_risk",
                    "confidence": "INFERRED",
                    "confidence_value": 0.7,
                    "sf_risk_level": "MEDIUM",
                    "sf_overlapping_fields": sorted(overlap),
                    "sf_note": (
                        "CPQ rule writes field(s) a Validation Rule on the same "
                        "object checks; Validation runs after CPQ recalculation "
                        "(OoE step 13)"
                    ),
                }
            )

    return risk_edges
