"""
graphify-sf: Custom Metadata field-mapping analysis pass.

Some ``__mdt`` records are field-mapping tables: their ``<values>`` name a source
object/field and a target object/field (e.g. ``Opportunity_To_Quote_Mapping__mdt``
copies Opportunity fields onto ``SBQQ__Quote__c`` during quote creation). The
parser (``metadata.extract_custom_metadata_record``) keeps the raw pairs on the
node as ``sf_values``; this pass turns recognized mappings into traversable
``maps_to`` edges (source field -> target field) so ``sf_impact`` can follow a
field change across the Opp->Quote boundary.

Detection is heuristic and best-effort (ADR-009). A value is treated as a field
reference when it is either:

    1. **Dotted** — ``Object.Field`` (e.g. ``Opportunity.Amount``); or
    2. **Separated** — a ``*Object*`` key (holding the object API name) paired by
       shared prefix with a ``*Field*`` key (holding the field API name), as in
       ``Main_Object__c`` + ``Main_Object_Api_Field__c``.

Keys are split into words on ``_`` and camelCase (``SourceField__c`` -> source,
field). Direction (source vs target) is read from role words in the key / pairing
prefix (``main``/``source``/``from``/``first`` vs ``second``/``target``/``to``/
``dest``); a field key's leading words may also name its object (``QuoteField__c``
-> ``Quote``). A record with no role words is a mapping only when its type name says
so (``mapping`` / ``sync`` / ``copy`` / ``to``); otherwise it is configuration
naming fields (e.g. a tracking config's ``DateField`` / ``NameField``) and gets
``reads_field`` edges from the record. A mapping whose direction cannot be
classified still emits ``maps_to`` in record order, flagged ``sf_ambiguous``
with ``AMBIGUOUS`` confidence (ADR-020).

This is an analysis pass (CLAUDE.md rule 4): it runs over the whole node/edge list
and mutates it in place, appending field stub nodes (no dangling edges, ADR-012)
and the ``maps_to`` edges. Records that are not field mappings produce no edges,
so the graph stays token-economical.
"""

from __future__ import annotations

import re

from graphify.salesforce.objects import field_node

#: A value shaped like ``Object.Field`` (single dot, identifier on each side).
_DOTTED_RE = re.compile(r"^[A-Za-z][\w]*\.[A-Za-z][\w]*$")

#: Words in an ``__mdt`` type name that mark a field-mapping table.
_MAPPING_TYPE_WORDS = frozenset({"mapping", "mappings", "map", "sync", "copy", "twin", "to"})

#: A value that can be an API name (no spaces, punctuation or numbers-only).
_IDENT_RE = re.compile(r"^[A-Za-z][\w]*$")

#: Role hint tokens (matched as whole ``_``-separated tokens, not substrings).
_SOURCE_HINTS = frozenset({"main", "source", "from", "first", "primary", "origin", "src"})
_TARGET_HINTS = frozenset({"second", "target", "to", "dest", "destination", "result"})


def _tokens(key: str) -> list[str]:
    """Lower-cased words of an API name: ``VRBQuoteField__c`` -> vrb, quote, field."""
    key = re.sub(r"__c$", "", key, flags=re.IGNORECASE)
    return [t.lower() for t in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", key)]


def _classify(tokens: list[str]) -> str | None:
    """Map key words to ``"source"`` / ``"target"`` / ``None`` by role token."""
    words = set(tokens)
    if words & _SOURCE_HINTS:
        return "source"
    if words & _TARGET_HINTS:
        return "target"
    return None


def _head_index(tokens: list[str], head: str) -> int | None:
    """Index of *head* when the key names one (``SourceField``, ``FieldName``,
    ``ObjectApiName``), not a property of one (``FieldLabel``, ``FieldType``)."""
    for tail in ((), ("name",), ("api",), ("api", "name")):
        at = len(tokens) - len(tail) - 1
        if at >= 0 and tokens[at] == head and tuple(tokens[at + 1:]) == tail:
            return at
    return None


def _is_mapping_type(record: dict) -> bool:
    """Whether the record's ``__mdt`` type name says it maps fields."""
    words = set(_tokens(str(record.get("sf_mdt_type", "")).removesuffix("__mdt")))
    return bool(words & _MAPPING_TYPE_WORDS)


def _object_from_prefix(prefix: list[str], objects: dict[str, str]) -> str | None:
    """``QuoteField`` -> ``Quote`` when the words before ``field`` name an SObject."""
    if not prefix:
        return None
    for candidate in ("".join(prefix), "_".join(prefix)):
        for name in (candidate, f"{candidate}__c"):
            if name in objects:
                return objects[name]
    return None


def _refs_from_values(values: dict, objects: dict[str, str] | None = None
                      ) -> list[tuple[str | None, str | None, str]]:
    """Return ``(role, object_api_or_None, field_api)`` field references in a record.

    *objects* maps lower-cased SObject API names to their spelling, used to read
    the object from a field key's own words (``QuoteField__c``).
    """
    objects = objects or {}
    refs: list[tuple[str | None, str | None, str]] = []
    used: set[str] = set()

    # 1. Dotted Object.Field values — role read from the key name.
    for key, val in values.items():
        if isinstance(val, str) and _DOTTED_RE.match(val.strip()):
            obj, fld = val.strip().split(".", 1)
            refs.append((_classify(_tokens(key)), obj, fld))
            used.add(key)

    # 2. Separated *Object* + *Field* keys; a field pairs with the object key
    # whose leading words it shares (``Main_Object`` + ``Main_Object_Api_Field``).
    obj_keys: dict[tuple[str, ...], str] = {}
    fld_keys: list[tuple[list[str], str, int]] = []
    for key, val in values.items():
        if key in used or not isinstance(val, str) or not _IDENT_RE.match(val.strip()):
            continue
        tokens = _tokens(key)
        if (at := _head_index(tokens, "field")) is not None:
            fld_keys.append((tokens, val.strip(), at))
        elif (at := _head_index(tokens, "object")) is not None:
            obj_keys[tuple(tokens[:at])] = val.strip()

    for tokens, fval, at in fld_keys:
        prefix = tokens[:at]
        match = None
        for obj_prefix in obj_keys:
            if tuple(prefix[: len(obj_prefix)]) == obj_prefix and (
                    match is None or len(obj_prefix) > len(match)):
                match = obj_prefix
        obj_api = obj_keys[match] if match is not None else _object_from_prefix(prefix, objects)
        role = _classify(tokens) or (_classify(list(match)) if match else None)
        refs.append((role, obj_api, fval))

    return refs


def _ensure_field(nodes, by_id, api, obj, record) -> str:
    node = field_node(obj, api, record.get("source_file", ""), sf_stub=True)
    if node["id"] not in by_id:
        nodes.append(node)
        by_id[node["id"]] = node
    return node["id"]


def mdt_mapping_pass(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Emit ``maps_to`` field->field edges for field-mapping ``cmt_record`` nodes.

    A record without role words is a mapping only when its ``__mdt`` type name
    says so (``…Mapping__mdt``, ``Sync…``); otherwise it is configuration that
    *names* fields and its references become ``reads_field`` edges from the
    record instead.

    Mutates ``nodes`` / ``edges`` in place (appends field stubs + edges) and also
    returns the new edges for callers that prefer the functional form.
    """
    by_id = {n["id"]: n for n in nodes}
    objects = {
        str(n.get("sf_api_name") or n.get("label", "")).lower(): n.get("sf_api_name") or n["label"]
        for n in nodes if n.get("file_type") == "sobject"
    }
    new_edges: list[dict] = []

    for record in [n for n in nodes if n.get("file_type") == "cmt_record"]:
        values = record.get("sf_values") or {}
        if not values:
            continue
        refs = _refs_from_values(values, objects)
        sources = [r for r in refs if r[0] == "source"]
        targets = [r for r in refs if r[0] == "target"]

        pairs: list[tuple] = []
        ambiguous = False
        if sources and targets:
            for s in sources:
                for t in targets:
                    pairs.append((s, t, "INFERRED"))
        elif not (sources or targets) and len(refs) >= 2 and _is_mapping_type(record):
            # Mapping table without role words — assume the first ref feeds the rest.
            ambiguous = True
            for t in refs[1:]:
                pairs.append((refs[0], t, "AMBIGUOUS"))
        else:
            # Without an object a bare name (``Name``, ``Address``) is noise.
            for _, obj, fld in dict.fromkeys(r for r in refs if r[1]):
                new_edges.append({
                    "source": record["id"],
                    "target": _ensure_field(nodes, by_id, fld, obj, record),
                    "relation": "reads_field", "confidence": "INFERRED",
                    "source_file": record.get("source_file", ""),
                })
            continue

        for (_, sobj, sfld), (_, tobj, tfld), conf in pairs:
            if (sobj, sfld) == (tobj, tfld):
                continue
            sid = _ensure_field(nodes, by_id, sfld, sobj, record)
            tid = _ensure_field(nodes, by_id, tfld, tobj, record)
            edge = {
                "source": sid, "target": tid, "relation": "maps_to",
                "confidence": conf, "source_file": record.get("source_file", ""),
                "sf_via": record["id"], "sf_mapping_type": record.get("sf_mdt_type"),
            }
            if sobj:
                edge["sf_source_object"] = sobj
            if tobj:
                edge["sf_target_object"] = tobj
            if ambiguous:
                edge["sf_ambiguous"] = True
            new_edges.append(edge)

    edges.extend(new_edges)
    return new_edges
