#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["bashlex"]
# ///
"""Claude Code PreToolUse policy for Bash commands."""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

main = importlib.import_module("bash_policy.hook").main

if __name__ == "__main__":
    main()
