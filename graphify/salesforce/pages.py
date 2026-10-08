"""
graphify-sf: UI placement metadata — Lightning pages, Experience Cloud views,
Aura bundles.

Answers "which screen is this component on?":

    - ``extract_flexipage``: ``flexipages/<Name>.flexipage-meta.xml`` -> a
      ``flexipage`` node, ``page_for`` -> SObject for record pages, ``hosts``
      -> each custom component instance, and ``reads_field`` field references
      for Dynamic Forms ``<fieldItem>Record.Field</fieldItem>`` entries.
    - ``extract_experience_view``: ``experiences/<site>/views/<view>.json`` ->
      an ``experience_page`` node (``<site>/<view>``) with ``hosts`` -> every
      ``c:<component>`` it places.
    - ``extract_aura_bundle``: ``aura/<Bundle>/<Bundle>.cmp|.app`` -> an
      ``aura_component`` node and ``aura_controller`` -> its Apex controller.

Custom components are named without a namespace in FlexiPages and as
``c:<name>`` in Experience views; both match the bundle folder name. Pages do
not know whether a name is an LWC or an Aura bundle, so ``hosts`` edges carry
the transient ``_component`` name and ``extract_sf`` points them at the LWC or
Aura node of that name (``resolve_hosted_components``). Standard components
(``flexipage:``, ``force:``, ``forceCommunity:`` …) are counted, not linked.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from graphify.salesforce.constants import apex_nid, sobject_stub
from graphify.salesforce.field_refs import field_ref

_NS = {"md": "http://soap.sforce.com/2006/04/metadata"}

#: ``controller="Ns.ClassName"`` on ``<aura:component>`` / ``<aura:application>``.
_AURA_CONTROLLER_RE = re.compile(r"<aura:(?:component|application)\b[^>]*\bcontroller\s*=\s*\"([\w.]+)\"",
                                 re.IGNORECASE)


def _custom_component(name: str) -> str | None:
    """Bundle name of a custom component reference, ``None`` for standard ones."""
    if name.startswith("c:"):
        return name[2:]
    return None if ":" in name else name


def _hosts_edge(page_id: str, component: str, path: Path) -> dict:
    return {"source": page_id, "target": None, "_component": component,
            "relation": "hosts", "confidence": "EXTRACTED", "source_file": str(path)}


def _error(node_id: str, path: Path, exc: Exception) -> dict:
    return {"nodes": [{"id": node_id, "label": f"[Parse Error] {path.name}",
                       "file_type": "concept", "source_file": str(path),
                       "sf_error_type": "parse_error", "sf_error_message": str(exc)}],
            "edges": []}


def extract_flexipage(path: Path) -> dict:
    """Parse a ``*.flexipage-meta.xml`` Lightning page."""
    path = Path(path)
    name = path.name.split(".")[0]
    page_id = f"flexipage_{name.lower()}"
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return _error(page_id, path, exc)

    sobject = root.findtext("md:sobjectType", namespaces=_NS)
    components = [e.text.strip() for e in root.iter(f"{{{_NS['md']}}}componentName")
                  if e.text and e.text.strip()]
    custom = sorted({c for c in map(_custom_component, components) if c})
    node = {
        "id": page_id, "label": name, "file_type": "flexipage", "source_file": str(path),
        "sf_qualified_name": name,
        "sf_page_type": root.findtext("md:type", namespaces=_NS),
        "sf_label": root.findtext("md:masterLabel", namespaces=_NS),
        "sf_standard_component_count": len(components) - sum(
            1 for c in components if _custom_component(c)),
    }
    nodes: list[dict] = [node]
    edges = [_hosts_edge(page_id, c, path) for c in custom]
    refs: list[dict] = []
    if sobject:
        node["sf_sobject"] = sobject
        stub = sobject_stub(sobject, path)
        nodes.append(stub)
        edges.append({"source": page_id, "target": stub["id"], "relation": "page_for",
                      "confidence": "EXTRACTED", "source_file": str(path)})
        for item in root.iter(f"{{{_NS['md']}}}fieldItem"):
            text = (item.text or "").strip()
            if text.startswith("Record."):
                refs.append(field_ref(page_id, sobject, text[len("Record."):], "reads_field",
                                      path, confidence="EXTRACTED", sf_displayed=True))
    return {"nodes": nodes, "edges": edges, "field_refs": refs}


def _component_names(data) -> list[str]:
    """Every ``componentName`` / ``definition`` value in an Experience view."""
    found: list[str] = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in ("componentName", "definition"):
                if isinstance(item.get(key), str):
                    found.append(item[key])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def extract_experience_view(path: Path) -> dict:
    """Parse ``experiences/<site>/views/<view>.json`` (Experience Cloud page)."""
    path = Path(path)
    site, view = path.parent.parent.name, path.stem
    qualified = f"{site}/{view}"
    page_id = f"experience_{site.lower()}.{view.lower()}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _error(page_id, path, exc)

    components = _component_names(data)
    custom = sorted({c[2:] for c in components if c.startswith("c:")})
    node = {
        "id": page_id, "label": qualified, "file_type": "experience_page",
        "source_file": str(path), "sf_qualified_name": qualified, "sf_site": site,
        "sf_label": data.get("label") if isinstance(data, dict) else None,
        "sf_standard_component_count": sum(1 for c in components if not c.startswith("c:")),
    }
    return {"nodes": [node], "edges": [_hosts_edge(page_id, c, path) for c in custom]}


def extract_aura_bundle(path: Path) -> dict:
    """Parse ``aura/<Bundle>/<Bundle>.cmp|.app`` into an ``aura_component`` node."""
    path = Path(path)
    bundle = path.parent.name
    node = {"id": f"aura_{bundle.lower()}", "label": bundle, "file_type": "aura_component",
            "source_file": str(path), "sf_qualified_name": bundle}
    try:
        markup = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _error(node["id"], path, exc)
    nodes, edges = [node], []
    controller = _AURA_CONTROLLER_RE.search(markup)
    if controller:
        cls = controller.group(1).split(".")[-1]
        nodes.append({"id": apex_nid(cls), "label": cls, "file_type": "code",
                      "source_file": str(path), "sf_stub": True})
        edges.append({"source": node["id"], "target": apex_nid(cls),
                      "relation": "aura_controller", "confidence": "EXTRACTED",
                      "source_file": str(path)})
    return {"nodes": nodes, "edges": edges}


def resolve_hosted_components(all_nodes: list[dict], all_edges: list[dict]) -> None:
    """Point ``hosts`` edges at the LWC or Aura node of their component name.

    A name with no bundle in the repo becomes an ``lwc_component`` stub
    (``sf_stub``) so the placement is still visible.
    """
    ids = {n["id"] for n in all_nodes}
    for edge in all_edges:
        component = edge.pop("_component", None)
        if component is None:
            continue
        for candidate in (f"lwc_{component.lower()}", f"aura_{component.lower()}"):
            if candidate in ids:
                edge["target"] = candidate
                break
        else:
            edge["target"] = f"lwc_{component.lower()}"
            all_nodes.append({"id": edge["target"], "label": component,
                              "file_type": "lwc_component", "source_file": edge["source_file"],
                              "sf_qualified_name": component, "sf_stub": True})
            ids.add(edge["target"])
