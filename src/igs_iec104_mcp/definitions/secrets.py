"""The secrets of the benches: the certificate and key material that a definition names
as `secret:<name>` (IMPLEMENTATION.md §5, §6, §9; task V1b).

The secrets live in one directory, which the server is given. It must be outside the
project directory, since the project is versioned, and readable by its owner only. Each
secret is one file, named after the secret. A file that is a symbolic link, readable by
others, or larger than the limit is refused. The errors name the secret, never its
content, and the content is read only when a bench starts. Its digest is what a run
records.
"""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path

from igs_iec104_mcp.models.common import SECRET_PATTERN

# The largest secret: a DER certificate or key is well under this (a certificate of the
# profile is at most 8 KiB, the minimum a conformant implementation handles).
MAX_SECRET_BYTES: int = 64 * 1024
_OTHERS: int = 0o077


class SecretError(Exception):
    """A secret cannot be read from the secrets directory. The message names it, and
    never shows its content."""


class SecretStore:
    """The secrets of a server, read from a directory the server is given."""

    def __init__(self, root: Path, *, project: Path) -> None:
        if not root.is_dir():
            raise SecretError(f"the secrets directory {root} does not exist")
        resolved = root.resolve()
        if resolved.is_relative_to(project.resolve()):
            raise SecretError(
                f"the secrets directory {root} is inside the project directory: keep "
                "the secrets outside the project, which is versioned"
            )
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if mode & _OTHERS:
            raise SecretError(
                f"the secrets directory {root} is readable by others: make it "
                "owner-only (chmod 700)"
            )
        self.root = resolved

    def read(self, reference: str) -> bytes:
        """The content of the secret a reference names (`secret:<name>`)."""
        if not _is_reference(reference):
            raise SecretError(f"{reference!r} is not a secret reference")
        path = self.root / reference.removeprefix("secret:")
        if path.is_symlink() or not path.is_file():
            raise SecretError(f"{reference} is not a file of the secrets directory")
        status = path.stat()
        if stat.S_IMODE(status.st_mode) & _OTHERS:
            raise SecretError(
                f"{reference} is readable by others: make the file owner-only "
                "(chmod 600)"
            )
        if status.st_size > MAX_SECRET_BYTES:
            raise SecretError(
                f"{reference} is {status.st_size} bytes, and a secret is at most "
                f"{MAX_SECRET_BYTES} bytes"
            )
        return path.read_bytes()


def digest(content: bytes) -> str:
    """The SHA-256 of a secret's content: what a run records, never the content."""
    return hashlib.sha256(content).hexdigest()


def _is_reference(reference: str) -> bool:
    return re.fullmatch(SECRET_PATTERN, reference) is not None
