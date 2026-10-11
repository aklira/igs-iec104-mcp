"""Runs the MCP server: python -m igs_iec104_mcp --project DIR (task M1)."""

import sys

from igs_iec104_mcp.server import main

if __name__ == "__main__":
    sys.exit(main())
