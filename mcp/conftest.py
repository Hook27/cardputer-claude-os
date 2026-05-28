"""Pytest bootstrap: put the mcp/ directory on sys.path so tests can
import the flat modules (`auth`, `server`) the same way the entrypoints
do. There's no package __init__ here on purpose — `server.py` is run
directly by `claude mcp add` / launchd, not imported as a package."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
