"""The secrets of the benches (IMPLEMENTATION.md §5, §6, §9; task V1b).

The secrets directory is outside the project and readable by its owner only. A secret
is one owner-only file, not a link, and not larger than the limit. A refusal names the
secret and never shows its content.
"""

import hashlib
import os
from pathlib import Path

import pytest

from igs_iec104_mcp.definitions.secrets import (
    MAX_SECRET_BYTES,
    SecretError,
    SecretStore,
    digest,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def secrets(tmp_path: Path) -> Path:
    path = tmp_path / "secrets"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def write(directory: Path, name: str, content: bytes, mode: int = 0o600) -> Path:
    path = directory / name
    path.write_bytes(content)
    path.chmod(mode)
    return path


def test_a_missing_directory_is_named(tmp_path: Path, project: Path) -> None:
    with pytest.raises(SecretError, match="does not exist"):
        SecretStore(tmp_path / "absent", project=project)


def test_the_directory_must_be_outside_the_project(project: Path) -> None:
    inside = project / "secrets"
    inside.mkdir(mode=0o700)
    with pytest.raises(SecretError, match="inside the project directory"):
        SecretStore(inside, project=project)


def test_the_directory_must_be_owner_only(tmp_path: Path, project: Path) -> None:
    open_dir = tmp_path / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    with pytest.raises(SecretError, match="readable by others"):
        SecretStore(open_dir, project=project)


def test_a_secret_is_read_as_its_bytes(secrets: Path, project: Path) -> None:
    content = b"\x30\x82 a DER certificate"
    write(secrets, "station-cert", content)
    store = SecretStore(secrets, project=project)
    assert store.read("secret:station-cert") == content
    assert digest(content) == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize(
    "reference", ["station-cert", "secret:../etc", "secret:A", "secret:a/b"]
)
def test_a_value_that_is_not_a_secret_reference_is_refused(
    secrets: Path, project: Path, reference: str
) -> None:
    store = SecretStore(secrets, project=project)
    with pytest.raises(SecretError, match="not a secret reference"):
        store.read(reference)


def test_a_missing_secret_is_named(secrets: Path, project: Path) -> None:
    store = SecretStore(secrets, project=project)
    with pytest.raises(SecretError, match="secret:station-cert is not a file"):
        store.read("secret:station-cert")


def test_a_secret_readable_by_others_is_refused_without_its_content(
    secrets: Path, project: Path
) -> None:
    write(secrets, "station-key", b"PRIVATE-KEY-BYTES", mode=0o640)
    store = SecretStore(secrets, project=project)
    with pytest.raises(SecretError, match="chmod 600") as error:
        store.read("secret:station-key")
    assert "PRIVATE-KEY-BYTES" not in str(error.value)


def test_a_symbolic_link_is_refused(
    secrets: Path, project: Path, tmp_path: Path
) -> None:
    target = write(tmp_path, "elsewhere", b"content")
    os.symlink(target, secrets / "linked")
    store = SecretStore(secrets, project=project)
    with pytest.raises(SecretError, match="is not a file"):
        store.read("secret:linked")


def test_a_secret_larger_than_the_limit_is_refused(
    secrets: Path, project: Path
) -> None:
    write(secrets, "huge", b"x" * (MAX_SECRET_BYTES + 1))
    store = SecretStore(secrets, project=project)
    with pytest.raises(SecretError, match="at most"):
        store.read("secret:huge")
