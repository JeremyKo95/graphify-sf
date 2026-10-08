"""
graphify-sf: consumption-layer pipeline (enrichment + persistence).

``extract_sf()`` returns a raw ``{"nodes", "edges"}`` extraction. To make that
queryable with the token-aware base graphify consumption stack (the MCP
``serve`` server, ``god_nodes``, ``shortest_path``, community navigation), the
graph must first be (1) built into a NetworkX multigraph and (2) enriched with
community labels. Output is deterministic: the same repo always yields a
byte-identical ``graph.json``.

Design notes:
    - **Directed multigraph** keyed by relation: SF impact analysis needs
      source→target direction (a trigger acts ON an SObject; a Flow updates a
      Quote), and one pair may carry several relations (reads AND writes).
    - **Direct construction, NOT ``build.build_from_json``**: the base builder
      hard-codes a file_type whitelist (``code/document/paper/image/rationale/
      concept``) and collapses every other type to ``concept`` (build.py:187),
      which would erase the SF semantics (``sobject``/``flow``/``cpq_rule``/
      ``validation_rule``/…); it also renames a node's ``source`` attribute to
      ``source_file``. So we build the graph ourselves, preserving every attribute. SF IDs are already canonical /
      deduped via ``sobject_nid`` + ``_merge_into`` in ``extract_sf``, so no
      entity dedup is needed.
    - **Community label persistence**: ``serve`` reconstructs communities from a
      ``community`` attribute on each node (``serve._communities_from_graph``);
      we write exactly that attribute so ``graph_stats`` / ``get_community`` work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import networkx as nx
from networkx.readwrite import json_graph



#: Per-occurrence attributes kept as a sorted list when one (source, target,
#: relation) edge occurs several times (e.g. three SOQL sites on Account).
_PER_SITE_ATTRS = ("source_location", "sf_dml_type", "sf_flow_element", "source_file")


def _aggregate_edges(edges: list[dict], node_ids) -> dict[tuple, dict]:
    """Fold edges into one record per ``(source, target, relation)``.

    The first occurrence (in a canonical sort order, so input order never
    matters) provides the attributes; repeats add ``sf_occurrences`` and plural
    ``<attr>s`` lists for the per-site attributes. ``sf_in_loop`` is true if any
    site is in a loop and the highest-confidence occurrence wins.
    """
    def order(edge):
        return (edge["source"], edge["target"], edge.get("relation", ""),
                json.dumps(edge, sort_keys=True, ensure_ascii=False, default=str))

    grouped: dict[tuple, dict] = {}
    sites: dict[tuple, dict[str, set]] = {}
    for edge in sorted(edges, key=order):
        if edge["source"] not in node_ids or edge["target"] not in node_ids:
            continue  # never serialize a dangling edge
        key = (edge["source"], edge["target"], edge.get("relation", ""))
        attrs = {k: v for k, v in edge.items() if k not in ("source", "target")}
        seen = sites.setdefault(key, {a: set() for a in _PER_SITE_ATTRS})
        for a in _PER_SITE_ATTRS:
            if attrs.get(a) not in (None, ""):
                seen[a].add(str(attrs[a]))
        first = grouped.get(key)
        if first is None:
            grouped[key] = attrs
            continue
        first["sf_occurrences"] = first.get("sf_occurrences", 1) + 1
        if attrs.get("sf_in_loop"):
            first["sf_in_loop"] = True
        if attrs.get("confidence_value", 1.0) > first.get("confidence_value", 1.0):
            first["confidence"] = attrs.get("confidence", first.get("confidence"))
            first["confidence_value"] = attrs["confidence_value"]

    for key, attrs in grouped.items():
        for a, values in sites[key].items():
            if len(values) > 1:
                attrs[f"{a}s"] = sorted(values)
    return grouped


def _communities(G: nx.MultiDiGraph, resolution: float) -> list[list[str]]:
    """Deterministic communities, largest first (ties by smallest node ID).

    Seeded NetworkX Louvain on a canonical simple graph: integer node labels in
    sorted-ID order and attribute-free ``(min, max)`` edges. The base
    ``graphify.cluster`` Leiden backend (graspologic's native core) splits the
    same subgraph differently from one process to the next, which re-numbered
    ~24k of 30k nodes on every run and made daily graph diffs useless.
    """
    ids = sorted(G.nodes)
    rank = {node_id: i for i, node_id in enumerate(ids)}
    canonical = nx.Graph()
    canonical.add_nodes_from(range(len(ids)))
    canonical.add_edges_from(sorted(
        {(min(rank[u], rank[v]), max(rank[u], rank[v])) for u, v in G.edges() if u != v}
    ))
    groups = nx.community.louvain_communities(canonical, seed=42, resolution=resolution)
    ordered = sorted((sorted(g) for g in groups), key=lambda g: (-len(g), g[0]))
    return [[ids[i] for i in g] for g in ordered]


def build_sf_graph(extraction: dict, *, resolution: float = 1.0) -> nx.MultiDiGraph:
    """Build + enrich an SF extraction into a queryable directed multigraph.

    Args:
        extraction: ``{"nodes": [...], "edges": [...]}`` from ``extract_sf``.
        resolution: Louvain resolution (>1 = more, smaller communities).

    Returns:
        A NetworkX ``MultiDiGraph`` whose edge keys are the relation, so one pair
        can carry several relations (an Apex class that both ``queries`` and
        ``dml_operates_on`` Account keeps both — a ``DiGraph`` kept only the last,
        silently dropping 902 relations on a real org). Nodes and edges are
        inserted in sorted order, and every node that landed in a community
        carries ``community`` (matching ``serve._communities_from_graph``).
    """
    G = nx.MultiDiGraph()
    for node in sorted(extraction.get("nodes", []), key=lambda n: n["id"]):
        attrs = {k: v for k, v in node.items() if k != "id"}
        G.add_node(node["id"], **attrs)
    grouped = _aggregate_edges(extraction.get("edges", []), G)
    for (source, target, relation), attrs in sorted(grouped.items()):
        G.add_edge(source, target, key=relation, **attrs)

    for community_id, members in enumerate(_communities(G, resolution)):
        for node_id in members:
            G.nodes[node_id]["community"] = community_id

    return G


def write_sf_graph(G: nx.MultiDiGraph, out_path: Path | str) -> Path:
    """Serialize the enriched graph to node-link JSON at ``out_path``.

    Written with the ``links`` edge key (newer NetworkX) so the base
    ``serve._load_graph`` loads it without remapping; older NetworkX falls back
    to the default key, which ``serve`` also accepts.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json_graph.node_link_data(G, edges="links")
    except TypeError:  # NetworkX < 3.4 has no ``edges`` kwarg
        data = json_graph.node_link_data(G)
    # Compact separators (indentation alone was ~25% of a real org's file) and
    # sorted keys so identical input always yields byte-identical output. Written
    # to a sibling temp file and swapped in, so readers never see a partial file.
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, out_path)
    return out_path


def write_sf_summary(G: nx.DiGraph, out_path: Path | str) -> dict:
    """Write ``quality.graph_summary(G)`` as pretty JSON and return it."""
    from graphify.salesforce.quality import graph_summary

    summary = graph_summary(G)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary
