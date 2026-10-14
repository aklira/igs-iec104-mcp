"""The opt-in bench run of the preflight without an engine: the images it runs with, the
parameters of its server, and the checks it reports when it cannot start (task I5)."""

import json
from pathlib import Path

from mcp import StdioServerParameters

from igs_iec104_mcp.bench_run import (
    Images,
    bench_checks,
    bench_parameters,
    images_for,
)
from igs_iec104_mcp.checks import Check, Status

ENGINE_OK = Check(
    "engine", Status.OK, "podman", data={"engine": "podman", "host": "unix:///x.sock"}
)


def _no_run(project: Path, images: Images, host: str) -> StdioServerParameters:
    raise AssertionError("no bench may start here")


def test_images_given_on_the_command_line_need_no_state(tmp_path: Path) -> None:
    images = images_for("podman", "sha256:a", "sha256:b", tmp_path / "missing.json")
    assert images == Images(runtime="sha256:a", relay="sha256:b")


def test_missing_images_name_the_installer(tmp_path: Path) -> None:
    message = images_for("podman", None, None, tmp_path / "missing.json")
    assert isinstance(message, str)
    assert "scripts/install.sh --engine podman" in message


def test_images_of_another_engine_are_refused(tmp_path: Path) -> None:
    state = tmp_path / "install.json"
    state.write_text(
        json.dumps(
            {"engine": "docker", "runtime_image": "sha256:a", "relay_image": "sha256:b"}
        ),
        encoding="utf-8",
    )
    message = images_for("podman", None, None, state)
    assert isinstance(message, str)
    assert "the installed images are for docker" in message


def test_the_installed_images_are_read_from_the_state(tmp_path: Path) -> None:
    state = tmp_path / "install.json"
    state.write_text(
        json.dumps(
            {"engine": "podman", "runtime_image": "sha256:a", "relay_image": "sha256:b"}
        ),
        encoding="utf-8",
    )
    assert images_for("podman", None, None, state) == Images(
        runtime="sha256:a", relay="sha256:b"
    )


def test_a_state_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "install.json"
    state.write_text("[1, 2]", encoding="utf-8")
    message = images_for("podman", None, None, state)
    assert isinstance(message, str)
    assert "is not a JSON object" in message


def test_the_server_of_the_run_gets_the_images_and_the_engine(tmp_path: Path) -> None:
    parameters = bench_parameters(
        tmp_path, Images("sha256:a", "sha256:b"), "unix:///x.sock"
    )
    assert "--runtime-image" in parameters.args
    assert parameters.args[parameters.args.index("--runtime-image") + 1] == "sha256:a"
    assert parameters.args[parameters.args.index("--relay-image") + 1] == "sha256:b"
    assert parameters.env is not None
    assert parameters.env["DOCKER_HOST"] == "unix:///x.sock"


def test_no_engine_means_no_run() -> None:
    checks = bench_checks(None, "sha256:a", "sha256:b", parameters_for=_no_run)
    assert [check.name for check in checks] == ["bench-run"]
    assert checks[0].status is Status.FAIL


def test_a_failed_engine_check_means_no_run() -> None:
    failed = Check("engine", Status.FAIL, "no engine")
    checks = bench_checks(failed, "sha256:a", "sha256:b", parameters_for=_no_run)
    assert checks[0].status is Status.FAIL


def test_missing_images_mean_no_run(tmp_path: Path) -> None:
    checks = bench_checks(
        ENGINE_OK,
        None,
        None,
        state_file=tmp_path / "missing.json",
        parameters_for=_no_run,
    )
    assert checks[0].status is Status.FAIL
    assert "scripts/install.sh" in checks[0].detail
