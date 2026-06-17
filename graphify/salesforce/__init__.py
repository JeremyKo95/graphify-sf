"""
graphify-sf: Salesforce-specific knowledge graph extraction.

Main API:
- register(): Register SF parsers with graphify's core _DISPATCH table.
- extract_sf(): Extract a Salesforce repository and run the SF analysis passes.

``extract_sf`` is the pipeline wrapper described in ARCHITECTURE.md ("서브패키지
구조" / "분석 패스 실행 순서"): it dispatches each Salesforce file to its parser,
merges the per-file results into one node/edge list (cross-file resolution via
``sobject_nid`` — ADR-002), then runs the graph-wide analysis passes in the fixed
order CPQ -> LWC merge -> OoE -> Governor (ADR-011). Errors degrade gracefully
(ADR-009 lenient): an unreadable / unparseable file is skipped, never aborting
the whole analysis.
"""

from __future__ import annotations

from pathlib import Path


def register():
    """Register Salesforce parsers with the main graphify pipeline.

    Patches ``extract._DISPATCH`` so the core pipeline routes Salesforce
    metadata suffixes to the SF parsers. ``extract_sf`` does NOT depend on this
    (it dispatches internally), but ``import graphify.salesforce; register()``
    enables SF parsing through the plain ``graphify`` entry points (ADR-014).
    """
    from graphify.extract import _DISPATCH

    from .apex_enhanced import extract_apex_enhanced
    from .flow import extract_flow
    from .lwc import extract_lwc_html, extract_lwc_js
    from .objects import extract_custom_field, extract_custom_object
    from .profiles import extract_permission_set, extract_profile

    _DISPATCH[".cls"] = extract_apex_enhanced
    _DISPATCH[".trigger"] = extract_apex_enhanced
    _DISPATCH[".flow-meta.xml"] = extract_flow
    _DISPATCH[".object-meta.xml"] = extract_custom_object
    _DISPATCH[".field-meta.xml"] = extract_custom_field
    _DISPATCH[".profile-meta.xml"] = extract_profile
    _DISPATCH[".permissionset-meta.xml"] = extract_permission_set
    _DISPATCH[".html"] = extract_lwc_html
    _DISPATCH[".js"] = extract_lwc_js


def _parser_for(path: Path):
    """Return the SF parser callable for a file, or ``None`` if unsupported.

    Suffix matching mirrors ``extract._get_extractor`` (ADR-003): multi-part
    ``*.meta.xml`` names are matched on ``name``, while ``.cls`` / ``.trigger``
    match on the plain suffix. ``.html`` / ``.js`` are LWC-only — limited to
    files living under an ``lwc/`` directory so ordinary web assets are ignored.
    """
    from .apex_enhanced import extract_apex_enhanced
    from .flow import extract_flow
    from .lwc import extract_lwc_html, extract_lwc_js
    from .objects import extract_custom_field, extract_custom_object
    from .profiles import extract_permission_set, extract_profile

    name = path.name
    if name.endswith(".object-meta.xml"):
        return extract_custom_object
    if name.endswith(".field-meta.xml"):
        return extract_custom_field
    if name.endswith(".flow-meta.xml"):
        return extract_flow
    if name.endswith(".profile-meta.xml"):
        return extract_profile
    if name.endswith(".permissionset-meta.xml"):
        return extract_permission_set

    suffix = path.suffix
    if suffix in (".cls", ".trigger"):
        return extract_apex_enhanced

    # LWC HTML/JS only — guard on an lwc/ directory in the path.
    is_lwc = "lwc" in {p.lower() for p in path.parts[:-1]}
    if is_lwc and suffix == ".html":
        return extract_lwc_html
    if is_lwc and suffix == ".js":
        return extract_lwc_js

    return None


def _merge_into(all_nodes, node_by_id, node):
    """Merge ``node`` into the accumulated node list, deduping by ``id``.

    First occurrence wins for the ``id`` / ``label`` (ADR-012); subsequent nodes
    with the same ID only fill in attributes the existing node is missing. This
    is what lets a full SObject node (from the Object parser) and a stub SObject
    node (from an Apex SOQL ``FROM`` clause) collapse into a single node.
    """
    existing = node_by_id.get(node["id"])
    if existing is None:
        node_by_id[node["id"]] = node
        all_nodes.append(node)
        return
    for key, value in node.items():
        if key in ("id", "label"):
            continue
        if existing.get(key) in (None, "", []) and value not in (None, "", []):
            existing[key] = value


def _merge_lwc_components(all_nodes, all_edges):
    """LWC merge pass (Pass SF-2 / ADR-008): fold each ``*.html`` template node
    into its sibling ``*.js`` controller node so one ``lwc_component`` node
    remains per component.

    The HTML parser emits ``lwc_<stem>_html``; the JS parser emits ``lwc_<stem>``.
    When both exist we keep the JS node (it carries the ``@wire`` / ``@api``
    signal) and drop the HTML node, tagging the survivor ``sf_has_template``.
    A template-only component is promoted to the base ID so it is still a single
    ``lwc_component`` node.
    """
    node_by_id = {n["id"]: n for n in all_nodes}
    html_nodes = [n for n in all_nodes if n.get("sf_lwc_file_type") == "html"]
    for html in html_nodes:
        html_id = html["id"]
        base_id = html_id[: -len("_html")] if html_id.endswith("_html") else html_id
        base = node_by_id.get(base_id)
        if base is None or base is html:
            # Template-only component: promote to the base ID.
            all_nodes.remove(html)
            html["id"] = base_id
            html.pop("sf_lwc_file_type", None)
            html["sf_has_template"] = True
            node_by_id[base_id] = html
            all_nodes.append(html)
            continue
        base["sf_has_template"] = True
        all_nodes.remove(html)
        node_by_id.pop(html_id, None)


def extract_sf(path, **kwargs):
    """Extract a Salesforce repository into a knowledge graph.

    Dispatches every supported Salesforce file under ``path`` to its parser,
    merges the results (deduping nodes by ID for cross-file resolution), then
    runs the four SF analysis passes in the fixed ADR-011 order:

        1. ``cpq_analysis_pass``       — reclassify SBQQ__ nodes, detect QCP.
        2. ``_merge_lwc_components``   — fold LWC HTML + JS into one node.
        3. ``ooe_analysis_pass``       — Order of Execution chains.
        4. ``governor_limit_analysis_pass`` — diagnostic ``governor_violation``
           edges (appended to the edge list).

    Args:
        path: Path to a Salesforce repository / metadata directory (or a single
            metadata file).
        **kwargs: Reserved for future options (output-dir, neo4j-uri, …);
            currently ignored by the in-memory extraction wrapper.

    Returns:
        ``{"nodes": [...], "edges": [...]}`` — the merged, analyzed graph.
    """
    from .cpq import cpq_analysis_pass
    from .governor_limits import governor_limit_analysis_pass
    from .order_of_execution import ooe_analysis_pass

    root = Path(path)
    if root.is_file():
        files = [root]
    else:
        files = sorted(p for p in root.rglob("*") if p.is_file())

    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    node_by_id: dict[str, dict] = {}

    for file_path in files:
        parser = _parser_for(file_path)
        if parser is None:
            continue
        try:
            result = parser(file_path)
        except Exception:
            # Lenient mode (ADR-009): skip a file the parser could not handle.
            continue
        for node in result.get("nodes", []):
            _merge_into(all_nodes, node_by_id, node)
        all_edges.extend(result.get("edges", []))

    # Analysis passes — fixed order (ADR-011). Each mutates in place.
    cpq_analysis_pass(all_nodes, all_edges)
    _merge_lwc_components(all_nodes, all_edges)
    ooe_analysis_pass(all_nodes, all_edges)
    violations = governor_limit_analysis_pass(all_nodes, all_edges)
    all_edges.extend(violations)

    return {"nodes": all_nodes, "edges": all_edges}
