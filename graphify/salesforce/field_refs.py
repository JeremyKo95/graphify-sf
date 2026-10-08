"""
graphify-sf: field-reference resolution pass (field-level reads / writes).

Parsers see one file at a time, so they cannot know that ``Account.Owner.Name``
read from a Contact query means ``User.Name``, or that ``(SELECT … FROM
Contacts)`` is the Contact object. Instead they emit *field references* in
``result["field_refs"]`` and this graph-wide pass resolves them against the
merged object / field metadata:

    {"source": <node id>, "object": <context SObject API name>,
     "path": "Account.Owner.Name", "relation": "reads_field" | "writes_field",
     "source_file": ..., "source_location": "L12", "strict": bool, ...}

    {"source": <node id>, "object": <parent SObject>, "child": "Contacts",
     "paths": [...], "source_file": ..., ...}        # child-relationship subquery

``require_object`` references (Apex variables whose declared type might be a
wrapper class) are kept only when the type is a known SObject that is not also
an Apex class / interface / enum.

Resolution walks each relationship segment (``Rel__r`` -> the lookup field's
``referenceTo``; ``Account`` -> ``AccountId``'s target, or the object of that
name; ``Owner`` / ``CreatedBy`` / ``LastModifiedBy`` -> User). A path that
cannot be walked is attributed to the lookup field it starts from, tagged
``sf_unresolved_path``. ``strict`` references (formula tokens) are dropped
unless they land on a field defined in the repo, so formula functions and
literals never become fields.

CRITICAL (ADR-016): graph-wide pass, never cached per file.
"""

from __future__ import annotations

from graphify.salesforce.constants import field_nid, sobject_nid, sobject_stub

#: Relationship names whose target is fixed by the platform.
_USER_RELATIONSHIPS = frozenset({"owner", "createdby", "lastmodifiedby"})

#: Standard child relationships that are not the plural of their object.
_STANDARD_CHILDREN = {"childaccounts": "Account"}

#: Field paths that carry no field-level information.
_IGNORED_FIELDS = frozenset({"id"})


def field_ref(source: str, object_api: str | None, path: str, relation: str,
              source_file, **attrs) -> dict:
    """Build one field reference for ``result["field_refs"]``."""
    ref = {"source": source, "object": object_api, "path": path,
           "relation": relation, "source_file": str(source_file)}
    ref.update(attrs)
    return ref


class _Index:
    """Object / field / relationship lookups over the merged node list."""

    def __init__(self, nodes: list[dict]):
        self.objects: dict[str, str] = {}
        self.fields: dict[tuple[str, str], dict] = {}
        self.child_relationships: dict[str, str] = {}
        self.apex_types: set[str] = set()
        for node in nodes:
            if node.get("file_type") == "sobject":
                api = node.get("sf_api_name") or node.get("label", "")
                self.objects.setdefault(api.lower(), api)
            elif node.get("sf_code_type") in ("class", "interface", "enum"):
                self.apex_types.add(str(node.get("label", "")).lower())
        for node in nodes:
            if node.get("file_type") != "field" or not node.get("sf_object"):
                continue
            obj = node["sf_object"]
            self.fields[(obj.lower(), node["sf_api_name"].lower())] = node
            rel = node.get("sf_relationship_name")
            if rel and not node.get("sf_stub"):
                self.child_relationships.setdefault(f"{rel}__r".lower(), obj)

    def is_sobject(self, name: str) -> bool:
        """Known SObject that is not also an Apex type (wrapper classes)."""
        lc = name.lower()
        return lc in self.objects and lc not in self.apex_types

    def canon(self, obj: str) -> str:
        return self.objects.get(obj.lower(), obj)

    def defined(self, obj: str, field: str) -> bool:
        node = self.fields.get((obj.lower(), field.lower()))
        return bool(node) and not node.get("sf_stub")

    def relationship_target(self, obj: str, rel: str) -> str | None:
        lc = rel.lower()
        if lc.endswith("__r"):
            node = self.fields.get((obj.lower(), lc[:-1] + "c"))
        else:
            node = self.fields.get((obj.lower(), lc + "id"))
        targets = (node or {}).get("sf_reference_to") or []
        if len(targets) == 1:
            return self.canon(targets[0])
        if lc in _USER_RELATIONSHIPS:
            return "User"
        if lc == "parent":
            return obj
        if not lc.endswith("__r") and lc in self.objects:
            return self.objects[lc]
        return None

    def child_object(self, rel: str) -> tuple[str | None, str]:
        lc = rel.lower()
        if lc in self.child_relationships:
            return self.child_relationships[lc], "EXTRACTED"
        if lc in _STANDARD_CHILDREN:
            return _STANDARD_CHILDREN[lc], "INFERRED"
        for singular in (lc[:-3] + "y" if lc.endswith("ies") else None,
                         lc[:-1] if lc.endswith("s") else None):
            if singular and singular in self.objects:
                return self.objects[singular], "INFERRED"
        return None, "AMBIGUOUS"


def _walk(index: _Index, obj: str, path: str) -> tuple[str, str, bool]:
    """Resolve ``path`` from ``obj`` to ``(object, field, resolved)``."""
    segments = path.split(".")
    current = index.canon(obj)
    for rel in segments[:-1]:
        nxt = index.relationship_target(current, rel)
        if nxt is None:
            lookup = rel[:-1] + "c" if rel.lower().endswith("__r") else rel + "Id"
            return index.canon(obj), lookup, False
        current = nxt
    return current, segments[-1], True


def resolve_field_refs(all_nodes: list[dict], all_edges: list[dict],
                       refs: list[dict]) -> None:
    """Turn field references into ``reads_field`` / ``writes_field`` / child
    ``queries`` edges, adding field / SObject stubs for undefined targets.

    Mutates ``all_nodes`` / ``all_edges`` in place.
    """
    index = _Index(all_nodes)
    node_ids = {n["id"] for n in all_nodes}

    def ensure_object(obj: str, source_file: str) -> str:
        sid = sobject_nid(obj)
        if sid not in node_ids:
            all_nodes.append(sobject_stub(obj, source_file))
            node_ids.add(sid)
            index.objects.setdefault(obj.lower(), obj)
        return sid

    def ensure_field(obj: str, field: str, source_file: str) -> str:
        from graphify.salesforce.objects import field_node

        fid = field_nid(obj, field)
        if fid not in node_ids:
            all_nodes.append(field_node(obj, field, source_file, sf_stub=True))
            node_ids.add(fid)
            index.fields[(obj.lower(), field.lower())] = all_nodes[-1]
            all_edges.append({"source": fid, "target": ensure_object(obj, source_file),
                              "relation": "field_of", "confidence": "EXTRACTED",
                              "source_file": source_file})
        return fid

    def edge_attrs(ref: dict) -> dict:
        skip = {"source", "object", "path", "relation", "strict", "child", "paths",
                "require_object"}
        attrs = {k: v for k, v in ref.items() if k not in skip}
        attrs.setdefault("confidence", "EXTRACTED")
        return attrs

    def add_field_edge(ref: dict, obj: str, path: str) -> None:
        if ref["source"] not in node_ids:
            return
        target_obj, field, resolved = _walk(index, obj, path)
        if field.lower() in _IGNORED_FIELDS and resolved:
            return
        if ref.get("strict") and not index.defined(target_obj, field):
            return
        target = ensure_field(target_obj, field, ref["source_file"])
        if target == ref["source"]:
            return
        edge = {"source": ref["source"], "target": target, "relation": ref["relation"],
                **edge_attrs(ref)}
        if not resolved:
            edge.update(confidence="INFERRED", sf_unresolved_path=path)
        elif "." in path:
            edge["sf_path"] = path
        all_edges.append(edge)

    for ref in refs:
        obj = ref.get("object")
        if not obj or (ref.get("require_object") and not index.is_sobject(obj)):
            continue
        if "child" in ref:
            child, confidence = index.child_object(ref["child"])
            if child is None or ref["source"] not in node_ids:
                continue
            all_edges.append({
                "source": ref["source"], "target": ensure_object(child, ref["source_file"]),
                "relation": "queries", **edge_attrs(ref), "confidence": confidence,
                "sf_child_relationship": ref["child"],
            })
            for path in ref.get("paths", []):
                add_field_edge({**ref, "relation": "reads_field", "confidence": confidence},
                               child, path)
            continue
        add_field_edge(ref, obj, ref["path"])
