"""Atomic text-file replace shared by the on-disk state writers.

Used by ``session/handoff.py`` and ``memory/persistent.py`` (``MEMORY.md``
index and entry files) so every writer shares one tmp + ``os.replace``
pattern. A crash / full disk / concurrent reader mid-write then sees either
the old file or the new one, never a truncated or half-encoded one (a bare
``Path.write_text`` can leave one behind, and the next ``PersistentMemory()``
would then die with ``UnicodeDecodeError``).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` via a same-directory temp file + ``os.replace``.

    Args:
        path: Destination file. Its parent directory must exist.
        text: Full file content.
        encoding: Text encoding (default UTF-8).

    Raises:
        OSError: Propagated from the temp write / rename (caller decides
            whether a full disk is fatal). The temp file is removed on
            failure.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
