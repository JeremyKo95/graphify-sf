"""
graphify-sf: Salesforce-specific knowledge graph extraction.

Main API:
- register(): Register SF parsers with graphify
- extract_sf(): Extract and analyze Salesforce repository
"""

def register():
    """Register Salesforce parsers with main graphify pipeline."""
    from graphify.extract import _DISPATCH
    from graphify.detect import CODE_EXTENSIONS
    from graphify.validate import VALID_FILE_TYPES

    # Add to _DISPATCH (will be done in phase 0, step 1)
    # Currently stub
    pass

def extract_sf(path, **kwargs):
    """
    Extract Salesforce repository to knowledge graph.

    Args:
        path: Path to Salesforce repository (force-app directory)
        **kwargs: Additional options (neo4j-uri, output-dir, etc.)

    Returns:
        dict: {nodes: [...], edges: [...]}
    """
    # Will be implemented in phases
    raise NotImplementedError("extract_sf will be implemented during Harness execution")
