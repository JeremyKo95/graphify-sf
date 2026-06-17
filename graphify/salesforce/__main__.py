"""CLI entry point for graphify-sf."""

import json
import sys
from pathlib import Path
from graphify.salesforce import extract_sf

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m graphify.salesforce <sf-repo-path> [--output-dir <dir>]")
        sys.exit(1)

    repo_path = Path(sys.argv[1])
    output_dir = Path("graphify-out")

    # Parse --output-dir flag
    if "--output-dir" in sys.argv:
        idx = sys.argv.index("--output-dir")
        if idx + 1 < len(sys.argv):
            output_dir = Path(sys.argv[idx + 1])

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"🚀 Analyzing {repo_path}")
    result = extract_sf(repo_path)

    # Save to graph.json
    graph_file = output_dir / "graph.json"
    with open(graph_file, 'w') as f:
        json.dump(result, f, indent=2)

    print(f"✅ Analysis complete!")
    print(f"📊 Nodes: {len(result['nodes'])}")
    print(f"📊 Edges: {len(result['edges'])}")
    print(f"💾 Output: {graph_file}")
