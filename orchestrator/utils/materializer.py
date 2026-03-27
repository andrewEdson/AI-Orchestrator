"""
File materializer — parses fenced code blocks from agent output and writes
them to disk.

The ClaudeAgent instructs Claude to format output like:

    ```python:src/models/user.py
    # ... code ...
    ```

    ```html:templates/index.html
    <!-- ... code ... -->
    ```

This module extracts those blocks and writes each one to the specified path
relative to a base directory.  It is called by the Executor after every
successful task so that generated code lands as real files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# Matches:  ```<lang>:<path>\n<content>\n```
# The language part is optional (some agents omit it).
_FENCE_RE = re.compile(
    r"```(?:[a-zA-Z0-9_\-+.]*):([^\n]+)\n([\s\S]*?)```",
    re.MULTILINE,
)


def materialize(output: str, base_dir: str | Path) -> list[Path]:
    """
    Parse fenced code blocks from *output* and write each to disk under *base_dir*.

    Only blocks that include a file path (``lang:path`` syntax) are written.
    Blocks without a path are silently skipped — they're prose, not files.

    Args:
        output:   Raw text returned by an agent.
        base_dir: Root directory for all written files.

    Returns:
        List of absolute Path objects for every file that was written.
    """
    base = Path(base_dir).resolve()
    written: list[Path] = []

    for match in _FENCE_RE.finditer(output):
        rel_path = match.group(1).strip()
        content = match.group(2)

        if not rel_path:
            continue

        # Resolve the path safely — prevent directory traversal
        target = (base / rel_path).resolve()
        if not str(target).startswith(str(base)):
            continue  # skip paths that escape the base dir

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(target)

    return written
