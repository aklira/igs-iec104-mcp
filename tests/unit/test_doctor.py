"""The read-only preflight: each check against a probe that answers from a table, and
the output and exit code of the command (IMPLEMENTATION.md §14, task I2)."""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from igs_iec104_mcp.checks import Check, Report, Status
from igs_iec104_mcp.doctor import (
    CommandResult,
    EngineReply,
    EngineUnreachable,
    SystemProbe,
    check_host,
    engine_kind,
    main,
    run_checks,
)

TOOLCHAIN = Path("/pinned/runtime/rust-toolchain.toml")
MISSING_STATE = Path("/nonexistent/igs-test-state/install.json")
PODMAN = "unix:///run/user/1000/podman/podman.sock"
DOCKER = "unix:///var/run/docker.sock"
ENV = {"XDG_RUNTIME_DIR": "/run/user/1000"}


@dataclass
class FakeProbe:
    programs: dict[str, str] = field(default_factory=dict)
    answers: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    files: dict[Path, str] = field(default_factory=dict)
    engines: dict[str, EngineReply | str] = field(default_factory=dict)
    images: set[str] = field(default_factory=set)
    image_error: str | None = None

    def which(self, program: str) -> str | None:
        return self.programs.get(program)

    def run(self, argv: Sequence[str]) -> CommandResult | None:
        return self.answers.get(tuple(argv))

    def read_text(self, path: Path) -> str | None:
        return self.files.get(path)

    def engine_version(self, host: str) -> EngineReply:
        answer = self.engines.get(host, "no socket")
        if isinstance(answer, str):
            raise EngineUnreachable(answer)
        return answer

    def image_exists(self, host: str, identity: str) -> bool:
        if self.image_error is not None:
            raise EngineUnreachable(self.image_error)
        return identity in self.images


def healthy_probe() -> FakeProbe:
    return FakeProbe(
        programs={
            "uv": "/usr/bin/uv",
            "rustc": "/usr/bin/rustc",
            "pkg-config": "/usr/bin/pkg-config",
            "claude": "/bin/claude",
            "codex": "/bin/codex",
        },
        answers={
            ("/usr/bin/uv", "--version"): CommandResult(0, "uv 0.13.1"),
            ("/usr/bin/rustc", "--version"): CommandResult(0, "rustc 1.99.0 (abc)"),
            ("/usr/bin/pkg-config", "--modversion", "openssl"): CommandResult(0, "3.5"),
        },
        files={
            TOOLCHAIN: '[toolchain]\nchannel = "1.99.0"\n',
            Path("/proc/sys/kernel/osrelease"): "6.18.0-microsoft-standard-WSL2\n",
        },
        engines={PODMAN: EngineReply("podman", "5.7.0")},
    )


def run(
    probe: FakeProbe,
    *,
    engine: str = "auto",
    env: dict[str, str] | None = None,
    uid: int = 1000,
    platform: str = "linux",
    python_version: tuple[int, int] = (3, 12),
    project_dir: Path | None = None,
    state_file: Path = MISSING_STATE,
) -> Report:
    return run_checks(
        probe,
        engine=engine,
        project_dir=project_dir,
        state_file=state_file,
        env=ENV if env is None else env,
        uid=uid,
        platform=platform,
        python_version=python_version,
        toolchain=TOOLCHAIN,
    )


def check(report: Report, name: str) -> Check:
    return next(item for item in report.checks if item.name == name)


def no_server() -> list[Check]:
    return []


@pytest.fixture
def pinned_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The variables the checks read from this machine, fixed for the output tests."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("XDG_STATE_HOME", str(MISSING_STATE.parent))
    monkeypatch.setattr(os, "getuid", lambda: 1000)


def test_healthy_machine_passes_and_only_warns_about_missing_clients() -> None:
    probe = healthy_probe()
    del probe.programs["claude"]
    report = run(probe)
    assert report.ok
    assert check(report, "engine").status is Status.OK
    assert check(report, "engine").data["engine"] == "podman"
    assert check(report, "client-claude").status is Status.WARNING
    assert check(report, "client-codex").status is Status.OK


def test_missing_uv_fails_with_the_corrective_step() -> None:
    probe = healthy_probe()
    del probe.programs["uv"]
    report = run(probe)
    assert not report.ok
    uv = check(report, "uv")
    assert uv.status is Status.FAIL
    assert "docs.astral.sh/uv" in uv.action


def test_rust_other_than_the_pin_warns_and_still_passes() -> None:
    probe = healthy_probe()
    probe.answers[("/usr/bin/rustc", "--version")] = CommandResult(0, "rustc 1.90.0")
    report = run(probe)
    rust = check(report, "rust")
    assert rust.status is Status.WARNING
    assert "1.99.0" in rust.action
    assert report.ok


def test_unreadable_pin_warns_that_the_version_is_unknown() -> None:
    probe = healthy_probe()
    del probe.files[TOOLCHAIN]
    assert check(run(probe), "rust").status is Status.WARNING


def test_missing_rustc_fails() -> None:
    probe = healthy_probe()
    del probe.programs["rustc"]
    report = run(probe)
    assert check(report, "rust").status is Status.FAIL
    assert not report.ok


def test_missing_openssl_development_files_fail() -> None:
    probe = healthy_probe()
    probe.answers[("/usr/bin/pkg-config", "--modversion", "openssl")] = CommandResult(
        1, ""
    )
    openssl = check(run(probe), "openssl")
    assert openssl.status is Status.FAIL
    assert "libssl-dev" in openssl.action


def test_python_older_than_3_12_fails() -> None:
    assert check(run(healthy_probe(), python_version=(3, 11)), "python").status is (
        Status.FAIL
    )


def test_auto_falls_back_to_docker_when_podman_does_not_answer() -> None:
    probe = healthy_probe()
    del probe.engines[PODMAN]
    probe.engines[DOCKER] = EngineReply("docker", "27.0")
    engine = check(run(probe), "engine")
    assert engine.status is Status.OK
    assert engine.data["engine"] == "docker"
    assert engine.data["host"] == DOCKER


def test_podman_requested_and_unreachable_fails_naming_the_service() -> None:
    probe = healthy_probe()
    del probe.engines[PODMAN]
    engine = check(run(probe, engine="podman"), "engine")
    assert engine.status is Status.FAIL
    assert "docs/setup.md#podman" in engine.action


def test_docker_host_answering_as_podman_is_refused_for_docker() -> None:
    probe = healthy_probe()
    engine = check(
        run(probe, engine="docker", env={"DOCKER_HOST": PODMAN}),
        "engine",
    )
    assert engine.status is Status.FAIL
    assert "answers as podman, not docker" in engine.detail
    assert "--engine podman" in engine.action


def test_permission_denied_asks_for_access_and_changes_nothing() -> None:
    probe = healthy_probe()
    probe.engines[PODMAN] = "Permission denied"
    engine = check(run(probe), "engine")
    assert engine.status is Status.FAIL
    assert "does not change groups" in engine.action


def test_podman_as_root_warns() -> None:
    engine = check(run(healthy_probe(), uid=0), "engine")
    assert engine.status is Status.WARNING
    assert "as root" in engine.detail


def test_host_other_than_linux_fails() -> None:
    host = check(run(healthy_probe(), platform="darwin"), "host")
    assert host.status is Status.FAIL
    assert "WSL2" in host.action


def test_wsl1_kernel_warns() -> None:
    host = check_host("linux", "4.4.0-19041-Microsoft")
    assert host.status is Status.WARNING
    assert host.data["host"] == "wsl1"


def test_wsl2_kernel_is_reported_as_wsl2() -> None:
    host = check_host("linux", "6.18.0-microsoft-standard-WSL2\n")
    assert host.status is Status.OK
    assert host.data["host"] == "wsl2"


def test_engine_kind_recognises_podman_components() -> None:
    assert engine_kind([{"Name": "Podman Engine"}]) == "podman"
    assert engine_kind([{"Name": "Engine"}]) == "docker"
    assert engine_kind([]) == "docker"


def test_project_dir_that_does_not_exist_is_created_by_nobody(tmp_path: Path) -> None:
    project = tmp_path / "igs-project"
    report = run(healthy_probe(), project_dir=project)
    assert check(report, "project-dir").status is Status.OK
    assert not project.exists()


def test_project_dir_that_is_a_file_fails(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    assert check(run(healthy_probe(), project_dir=target), "project-dir").status is (
        Status.FAIL
    )


def test_project_dir_writable_by_everyone_warns(tmp_path: Path) -> None:
    tmp_path.chmod(0o777)
    try:
        project = check(run(healthy_probe(), project_dir=tmp_path), "project-dir")
    finally:
        tmp_path.chmod(0o700)
    assert project.status is Status.WARNING


def test_project_dir_under_an_unwritable_parent_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "access", lambda *_args: False)
    project = check(run(healthy_probe(), project_dir=tmp_path / "new"), "project-dir")
    assert project.status is Status.FAIL


def test_the_system_probe_reports_a_missing_socket_without_connecting() -> None:
    with pytest.raises(EngineUnreachable, match="no socket at"):
        SystemProbe().engine_version("unix:///nonexistent/igs-test.sock")


def test_the_system_probe_returns_none_for_a_missing_program() -> None:
    assert SystemProbe().run(["/nonexistent/igs-test-program"]) is None


def test_text_output_names_each_action_and_counts_failures(
    pinned_machine: None, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = healthy_probe()
    del probe.programs["uv"]
    assert main([], probe=probe, server=no_server) == 1
    out = capsys.readouterr().out
    assert "FAIL  uv" in out
    assert "-> install uv" in out
    assert "1 required check(s) failed." in out


def test_json_output_is_parseable_and_carries_the_status(
    pinned_machine: None, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = healthy_probe()
    del probe.programs["uv"]
    assert main(["--json"], probe=probe, server=no_server) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    statuses = {item["name"]: item["status"] for item in payload["checks"]}
    assert statuses["uv"] == "fail"
    assert statuses["openssl"] == "ok"


def test_server_only_skips_the_host_checks_and_reports_the_server(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def server() -> list[Check]:
        return [Check("server", Status.OK, "answered")]

    assert main(["--server-only", "--json"], server=server) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in payload["checks"]] == ["server"]


def test_a_failed_server_check_makes_the_doctor_exit_with_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def server() -> list[Check]:
        return [Check("server", Status.FAIL, "no answer", "start it by hand")]

    assert main(["--server-only"], server=server) == 1
    assert "-> start it by hand" in capsys.readouterr().out


def test_run_bench_cannot_be_combined_with_server_only() -> None:
    with pytest.raises(SystemExit) as refused:
        main(["--run-bench", "--server-only"], server=no_server)
    assert refused.value.code == 2


def test_nothing_starts_without_run_bench(capsys: pytest.CaptureFixture[str]) -> None:
    def bench(*_args: object) -> list[Check]:
        raise AssertionError("the bench must not start without --run-bench")

    assert main(["--server-only"], server=no_server, bench=bench) == 0


def test_a_failed_bench_check_makes_the_doctor_exit_with_1(
    pinned_machine: None, capsys: pytest.CaptureFixture[str]
) -> None:
    def bench(
        engine: Check | None, runtime: str | None, relay: str | None
    ) -> list[Check]:
        assert engine is not None and engine.name == "engine"
        assert (runtime, relay) == ("sha256:a", "sha256:b")
        return [Check("bench-run", Status.FAIL, "run ended failed", "read the run")]

    probe = healthy_probe()
    code = main(
        ["--run-bench", "--runtime-image", "sha256:a", "--relay-image", "sha256:b"],
        probe=probe,
        server=no_server,
        bench=bench,
    )
    assert code == 1
    assert "-> read the run" in capsys.readouterr().out


def _installed(path: Path, engine: str = "podman") -> Path:
    path.write_text(
        json.dumps(
            {
                "engine": engine,
                "runtime_image": "sha256:aa",
                "relay_image": "sha256:bb",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_the_installation_images_on_the_engine_pass(tmp_path: Path) -> None:
    probe = healthy_probe()
    probe.images = {"sha256:aa", "sha256:bb"}
    images = check(
        run(probe, state_file=_installed(tmp_path / "install.json")), "images"
    )
    assert images.status is Status.OK
    assert images.data["runtime_image"] == "sha256:aa"


def test_a_missing_installation_image_warns_and_names_the_installer(
    tmp_path: Path,
) -> None:
    probe = healthy_probe()
    probe.images = {"sha256:aa"}
    report = run(probe, state_file=_installed(tmp_path / "install.json"))
    images = check(report, "images")
    assert images.status is Status.WARNING
    assert images.data["missing"] == "sha256:bb"
    assert "scripts/install.sh" in images.action
    assert report.ok


def test_no_installation_yet_is_a_warning_not_a_failure() -> None:
    report = run(healthy_probe())
    images = check(report, "images")
    assert images.status is Status.WARNING
    assert "scripts/install.sh" in images.action
    assert report.ok


def test_images_of_another_engine_are_not_checked_against_this_one(
    tmp_path: Path,
) -> None:
    images = check(
        run(healthy_probe(), state_file=_installed(tmp_path / "x.json", "docker")),
        "images",
    )
    assert images.status is Status.WARNING
    assert "the installed images are for docker" in images.action


def test_no_image_check_without_a_working_engine(tmp_path: Path) -> None:
    probe = healthy_probe()
    del probe.engines[PODMAN]
    report = run(probe, state_file=_installed(tmp_path / "install.json"))
    assert all(item.name != "images" for item in report.checks)


def test_an_engine_that_does_not_answer_about_images_warns(tmp_path: Path) -> None:
    probe = healthy_probe()
    probe.image_error = "connection reset"
    images = check(
        run(probe, state_file=_installed(tmp_path / "install.json")), "images"
    )
    assert images.status is Status.WARNING
    assert "connection reset" in images.detail
