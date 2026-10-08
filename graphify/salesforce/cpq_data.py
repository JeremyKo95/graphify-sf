"""
graphify-sf: CPQ configuration-data ingester (SFDX JSON exports).

Salesforce CPQ's *rule logic* — Price Rules, Price Conditions/Actions, Product
Rules, Error Conditions — lives as ``SBQQ__*__c`` **data records**, not as
deployable metadata files. A metadata-only parse therefore never sees "under
which condition does a rule write which field". This module ingests those
records from SFDX JSON exports (``sf data query --json``) and turns them into
graph nodes/edges, following the ``pg_introspect`` / ``mcp_ingest`` pattern
(source → ``{"nodes", "edges"}``).

The crucial output is ``sf_target_fields`` aggregated onto each ``cpq_rule``
node (from its Price/Product Actions' target fields) and the condition fields —
these are exactly what ``validation_cpq`` and ``sf_impact`` need to fire on real
CPQ logic.

Lenient (ADR-009): an unreadable / malformed JSON file is skipped, never raising.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from graphify.salesforce.constants import sobject_stub
from graphify.salesforce.objects import field_node

#: SBQQ object → role in the graph.
_RULE_OBJECTS = {"SBQQ__PriceRule__c", "SBQQ__ProductRule__c"}
_CONDITION_OBJECTS = {"SBQQ__PriceCondition__c", "SBQQ__ErrorCondition__c"}
_ACTION_OBJECTS = {"SBQQ__PriceAction__c", "SBQQ__ProductAction__c"}
#: QCP plugin implemented as a JavaScript Custom Script (not Apex).
_SCRIPT_OBJECTS = {"SBQQ__CustomScript__c"}

#: CPQ condition / action object picklist values -> SObject API names.
_CPQ_OBJECT_PICKLIST = {
    "quote": "SBQQ__Quote__c",
    "quote line": "SBQQ__QuoteLine__c",
    "quote line group": "SBQQ__QuoteLineGroup__c",
    "product option": "SBQQ__ProductOption__c",
    "configuration attributes": "SBQQ__ConfigurationAttribute__c",
    "product": "Product2",
}

#: Object a rule applies to when none of its conditions / actions names one.
_DEFAULT_RULE_OBJECT = "SBQQ__Quote__c"


def _cpq_object(value) -> str | None:
    """Map a CPQ object picklist value (``Quote Line``) to its API name."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if " " not in value and ("__" in value or value[:1].isupper()):
        return _CPQ_OBJECT_PICKLIST.get(value.lower(), value)
    return _CPQ_OBJECT_PICKLIST.get(value.lower())


#: QCP JavaScript API lifecycle hooks (Quote Calculator Plugin, JS flavour).
_QCP_JS_METHODS = (
    "onInit", "onBeforeCalculate", "onBeforePriceRules", "onAfterPriceRules",
    "onAfterCalculate", "onBeforeCalculateForBatch", "isFieldEditable",
)


def _records(data: object) -> list[dict]:
    """Extract the record list from the various SFDX JSON shapes."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("records"), list):
            return [r for r in data["records"] if isinstance(r, dict)]
        result = data.get("result")
        if isinstance(result, dict) and isinstance(result.get("records"), list):
            return [r for r in result["records"] if isinstance(r, dict)]
    return []


def _object_type(record: dict, fallback: str) -> str:
    attrs = record.get("attributes")
    if isinstance(attrs, dict) and attrs.get("type"):
        return str(attrs["type"])
    return fallback


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gearset_flat(data: dict, fallback_type: str) -> dict:
    """Flatten a Gearset ``*.gs.json`` record into the SFDX-style flat shape.

    Gearset exports one record per file as ``{"Object", "Fields", "References"}``:
    the Id lives in ``Fields.GearsetExternalId__c``, the parent rule in a
    ``SBQQ__Rule__r`` reference, and an action's target field in ``SBQQ__Field__c``
    (SFDX would use ``SBQQ__TargetField__c``). This maps all of that onto the same
    keys the SFDX path already consumes, so the rest of the parser is shared.
    """
    fields = data.get("Fields", {})
    flat = dict(fields)
    flat["_obj_type"] = data.get("Object") or fallback_type
    rid = fields.get("GearsetExternalId__c")
    if rid:
        flat["Id"] = rid
    for ref in data.get("References", []) or []:
        if ref.get("Name") == "SBQQ__Rule__r":
            parent = (ref.get("ReferencedFields") or {}).get("GearsetExternalId__c")
            if parent:
                flat["SBQQ__Rule__c"] = parent
    if flat["_obj_type"] in _ACTION_OBJECTS and fields.get("SBQQ__Field__c"):
        flat.setdefault("SBQQ__TargetField__c", fields["SBQQ__Field__c"])
    return flat


def _normalized_records(data, fallback_type: str) -> list[dict]:
    """Yield flat records (each with ``_obj_type``) from SFDX *or* Gearset JSON."""
    # Gearset single-record shape.
    if isinstance(data, dict) and "Object" in data and "Fields" in data:
        return [_gearset_flat(data, fallback_type)]
    # SFDX shapes (list / {records} / {result.records}).
    out = []
    for rec in _records(data):
        flat = dict(rec)
        flat["_obj_type"] = _object_type(rec, fallback_type)
        out.append(flat)
    return out


def _rule_nid(record_id: str) -> str:
    return f"cpq_rule_{record_id.lower()}"


def _node_id(prefix: str, record_id: str) -> str:
    return f"{prefix}_{record_id.lower()}"


def extract_cpq_data(data_dir: Path | str) -> dict:
    """Ingest SFDX JSON exports of SBQQ CPQ records into graph nodes/edges.

    Reads every ``*.json`` under ``data_dir``. Each record's object type is read
    from ``attributes.type`` (falling back to the file stem). Produces:

        - ``cpq_rule`` nodes (Price/Product Rule) carrying ``sf_cpq_object``,
          ``sf_eval_order``, and ``sf_target_fields`` (aggregated from actions).
        - ``cpq_condition`` / ``cpq_action`` nodes.
        - edges: ``cpq_applies_to`` (rule → object, with ``execution_order``),
          ``cpq_has_condition`` / ``cpq_has_action`` (rule → child),
          ``cpq_reads_field`` / ``cpq_writes_field`` (child → field).

    Returns ``{"nodes": [...], "edges": [...]}`` (lenient: bad files skipped).
    """
    data_dir = Path(data_dir)
    if not data_dir.exists():
        return {"nodes": [], "edges": []}

    # Bucket records by role across all files first (children may precede rules).
    rules: dict[str, dict] = {}        # record_id -> record
    rule_types: dict[str, str] = {}    # record_id -> SBQQ object type
    conditions: list[tuple[str, dict]] = []
    actions: list[tuple[str, dict]] = []
    scripts: list[dict] = []

    for json_file in sorted(data_dir.rglob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # lenient
        # Fallback type from the parent directory name (Gearset layout:
        # configdata/<ObjectName>/*.gs.json) or the file stem (SFDX-per-object).
        fallback = json_file.parent.name if json_file.parent.name.startswith("SBQQ__") else json_file.stem
        for record in _normalized_records(data, fallback):
            rec_id = record.get("Id")
            if not rec_id:
                continue
            obj_type = record.get("_obj_type")
            if obj_type in _RULE_OBJECTS:
                rules[rec_id] = record
                rule_types[rec_id] = obj_type
            elif obj_type in _CONDITION_OBJECTS:
                conditions.append((obj_type, record))
            elif obj_type in _ACTION_OBJECTS:
                actions.append((obj_type, record))
            elif obj_type in _SCRIPT_OBJECTS:
                scripts.append(record)

    nodes: list[dict] = []
    edges: list[dict] = []
    node_ids: set[str] = set()
    target_fields: dict[str, list[str]] = {}  # rule_id -> [field api names]
    # rule_id -> objects its actions write / conditions test (actions first).
    rule_objects: dict[str, list[str]] = {}

    def _ensure_field_node(field_api: str, object_api: str | None) -> str:
        node = field_node(object_api, field_api, "cpq_data", sf_stub=True)
        if node["id"] not in node_ids:
            nodes.append(node)
            node_ids.add(node["id"])
        return node["id"]

    # --- Conditions (read fields) -------------------------------------------
    for obj_type, rec in conditions:
        cid = _node_id("cpq_condition", rec["Id"])
        field = rec.get("SBQQ__Field__c") or rec.get("SBQQ__TestedField__c")
        field_object = _cpq_object(
            rec.get("SBQQ__Object__c") or rec.get("SBQQ__TestedObject__c")
        )
        nodes.append({
            "id": cid, "label": rec.get("Name") or cid, "file_type": "cpq_condition",
            "source_file": "cpq_data", "sf_cpq_object": obj_type,
            "sf_field": field, "sf_field_object": field_object,
            "sf_operator": rec.get("SBQQ__Operator__c"),
        })
        node_ids.add(cid)
        rule_id = rec.get("SBQQ__Rule__c")
        if rule_id and field_object:
            rule_objects.setdefault(rule_id, []).append(field_object)
        if rule_id:
            edges.append({"source": _rule_nid(rule_id), "target": cid,
                          "relation": "cpq_has_condition", "confidence": "EXTRACTED",
                          "source_file": "cpq_data"})
        if field:
            edges.append({"source": cid, "target": _ensure_field_node(field, field_object),
                          "relation": "cpq_reads_field", "confidence": "EXTRACTED",
                          "source_file": "cpq_data"})

    # --- Actions (write fields) ---------------------------------------------
    for obj_type, rec in actions:
        aid = _node_id("cpq_action", rec["Id"])
        field = rec.get("SBQQ__TargetField__c")
        field_object = _cpq_object(rec.get("SBQQ__TargetObject__c"))
        nodes.append({
            "id": aid, "label": rec.get("Name") or aid, "file_type": "cpq_action",
            "source_file": "cpq_data", "sf_cpq_object": obj_type,
            "sf_target_field": field, "sf_field_object": field_object,
            "sf_formula": rec.get("SBQQ__Formula__c"),
        })
        node_ids.add(aid)
        rule_id = rec.get("SBQQ__Rule__c")
        if rule_id and field_object:
            # Actions name the object the rule really changes: put them first.
            rule_objects.setdefault(rule_id, []).insert(0, field_object)
        if rule_id:
            edges.append({"source": _rule_nid(rule_id), "target": aid,
                          "relation": "cpq_has_action", "confidence": "EXTRACTED",
                          "source_file": "cpq_data"})
            if field:
                target_fields.setdefault(rule_id, []).append(field)
        if field:
            edges.append({"source": aid, "target": _ensure_field_node(field, field_object),
                          "relation": "cpq_writes_field", "confidence": "EXTRACTED",
                          "source_file": "cpq_data"})

    # --- Rules (carry aggregated target fields + applies_to edge) -----------
    for rec_id, rec in rules.items():
        rid = _rule_nid(rec_id)
        is_price = rule_types[rec_id] == "SBQQ__PriceRule__c"
        # SBQQ__EvaluationEvent__c is a timing picklist (Always / Save / On
        # Calculate) and SBQQ__LookupObject__c a lookup-table source; neither is
        # the object the rule applies to, so both are kept as attributes only.
        objects = rule_objects.get(rec_id) or [_DEFAULT_RULE_OBJECT]
        applies_object = objects[0]
        eval_order = _num(rec.get("SBQQ__EvaluationOrder__c"))  # Gearset stores "50.0" as str
        fields = sorted(set(target_fields.get(rec_id, [])))
        node = {
            "id": rid, "label": rec.get("Name") or rid, "file_type": "cpq_rule",
            "source_file": "cpq_data", "sf_cpq_object": applies_object,
            "sf_cpq_rule_type": "Price" if is_price else "Product",
            "sf_eval_order": eval_order,
            "sf_evaluation_event": rec.get("SBQQ__EvaluationEvent__c"),
            "sf_lookup_object": rec.get("SBQQ__LookupObject__c"),
        }
        if fields:
            node["sf_target_fields"] = fields
        nodes.append(node)
        node_ids.add(rid)

        # rule -> object it applies to (feeds sf_cpq_chain; execution_order req'd).
        stub = sobject_stub(applies_object, "cpq_data")
        target_obj = stub["id"]
        if target_obj not in node_ids:
            nodes.append(stub)
            node_ids.add(target_obj)
        edges.append({
            "source": rid, "target": target_obj, "relation": "cpq_applies_to",
            "confidence": "EXTRACTED", "source_file": "cpq_data",
            "sf_cpq_rule_type": "Price" if is_price else "Product",
            "execution_order": int(eval_order) if isinstance(eval_order, (int, float)) else 0,
        })

    # --- QCP Custom Scripts (JavaScript QCP plugin) -------------------------
    # The org's calc logic may be a JS Custom Script, not an Apex plugin. Treat
    # it like the Apex QCP (cpq.py): emit a sf_qcp_implementation node + one
    # cpq_qcp_method per detected JS hook, and surface the declared Quote/
    # QuoteLine fields as sf_target_fields (so validation_cpq / impact see them).
    for rec in scripts:
        sid = _node_id("cpq_script", rec["Id"])
        code = rec.get("SBQQ__Code__c") or ""
        if not isinstance(code, str):
            code = json.dumps(code)
        declared: list[str] = []
        for key in ("SBQQ__QuoteFields__c", "SBQQ__QuoteLineFields__c", "SBQQ__GroupFields__c"):
            val = rec.get(key)
            if isinstance(val, list):
                declared += [str(f) for f in val if f]
        script_node = {
            "id": sid, "label": rec.get("Name") or sid, "file_type": "code",
            "source_file": "cpq_data", "sf_qcp_implementation": True,
            "sf_qcp_language": "javascript", "sf_cpq_object": "SBQQ__Quote__c",
        }
        if declared:
            script_node["sf_target_fields"] = sorted(set(declared))
        nodes.append(script_node)
        node_ids.add(sid)

        for method in _QCP_JS_METHODS:
            if re.search(r"\b" + method + r"\b", code):
                mid = f"{sid}_qcp_{method.lower()}"
                nodes.append({
                    "id": mid, "label": f"{script_node['label']}.{method}()",
                    "file_type": "cpq_qcp_method", "source_file": "cpq_data",
                    "sf_qcp_method": method, "sf_qcp_class": sid,
                    "sf_qcp_language": "javascript",
                })
                node_ids.add(mid)

    # Drop edges whose rule parent was never seen (no dangling).
    edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]
    return {"nodes": nodes, "edges": edges}
