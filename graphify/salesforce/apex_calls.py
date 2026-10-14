"""
graphify-sf: Apex cross-class call resolution (review items 9 / 16).

Graph-wide pass that links Apex classes / triggers to the Apex classes they
invoke. A per-file parser cannot do this: whether ``Foo.bar()`` is a call into
an Apex class depends on every class in the repo, so the pass runs after all
files are merged and matches against the full set of class names.

Three invocation shapes count, matched case-insensitively (Apex identifiers are
case-insensitive) on comment- and string-stripped code:

    - ``Foo.bar(...)`` / ``Outer.Inner.bar(...)`` — static call (also an
      instance call through a variable named like its class);
    - ``new Foo(...)`` / ``new Outer.Inner(...)`` — instantiation;
    - ``Foo.class`` — type literal handed to a dispatcher / framework.

A bare type declaration (``Foo x;``) is not a call and is ignored. Each caller ->
callee pair becomes one class-level ``calls`` edge, ``INFERRED``: a name match
cannot rule out a local variable that shadows a class name. Dynamic dispatch
(``Type.forName``, handler classes named in Custom Metadata) stays unresolved.

CRITICAL (ADR-016): graph-wide pass, never cached per file. Reads the transient
``_source`` the Apex parser leaves on class / trigger nodes; mutates
``all_edges`` in place.
"""

from __future__ import annotations

import re

from graphify.salesforce.apex_enhanced import strip_apex

#: Class-level Apex code types that can be the target of a call.
_CALLABLE_TYPES = ("class", "interface", "enum")

_STATIC_CALL_RE = re.compile(r"(?<![\w.])(\w+)\s*\.\s*\w+(?:\s*\.\s*\w+)*\s*\(")
_NEW_RE = re.compile(r"\bnew\s+(\w+)(?:\s*\.\s*\w+)*\s*\(", re.IGNORECASE)
_CLASS_LITERAL_RE = re.compile(r"(?<![\w.])(\w+)\s*\.\s*class\b", re.IGNORECASE)


def apex_calls_pass(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Append INFERRED ``calls`` edges between Apex code nodes (in place)."""
    callees = {
        str(n["label"]).lower(): n["id"]
        for n in all_nodes
        if n.get("sf_code_type") in _CALLABLE_TYPES and not n.get("sf_stub")
    }
    if not callees:
        return

    for node in all_nodes:
        source = node.get("_source")
        if not source or node.get("sf_code_type") not in (*_CALLABLE_TYPES, "trigger"):
            continue
        code, _strings = strip_apex(source)
        first_seen: dict[str, int] = {}
        for pattern in (_STATIC_CALL_RE, _NEW_RE, _CLASS_LITERAL_RE):
            for match in pattern.finditer(code):
                target = callees.get(match.group(1).lower())
                if target and target != node["id"]:
                    pos = match.start()
                    if pos < first_seen.get(target, len(code)):
                        first_seen[target] = pos
        for target in sorted(first_seen):
            all_edges.append({
                "source": node["id"],
                "target": target,
                "relation": "calls",
                "confidence": "INFERRED",
                "confidence_value": 0.8,
                "source_location": f"L{code.count(chr(10), 0, first_seen[target]) + 1}",
                "source_file": node.get("source_file", ""),
            })
