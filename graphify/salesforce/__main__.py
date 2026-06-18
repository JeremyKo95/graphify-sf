"""CLI entry point for graphify-sf (delegates to the ``sf`` subcommand CLI)."""

import sys

from graphify.salesforce.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
