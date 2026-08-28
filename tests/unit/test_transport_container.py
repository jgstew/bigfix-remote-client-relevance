"""Tests for TransportContainer and the ContainerEngine seam.

Containers answer the "does this client relevance work on Ubuntu 22.04 / RHEL 9
/ Amazon Linux" question on demand, with no SSH credentials and no long-lived
VM per distro. The docker SDK sits behind ContainerEngine so all of that is
driven here without a daemon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_BOOTSTRAP,
    ERROR_KIND_QNA,
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_TRANSPORT,
    ResolvedQna,
)
from bigfix_remote_client_relevance.transports.container import (
    ARTIFACT_MOUNT,
    ContainerEngineError,
    TransportContainer,
)


@dataclass
class ExecCall:
    command: str
    input: str | None


@dataclass
class FakeEngine:
    """Records container lifecycle and answers commands by regex."""

    responses: list[tuple[str, tuple[str, str, int]]] = field(default_factory=list)
    default: tuple[str, str, int] = ("", "", 0)
    pulled: list[str] = field(default_factory=list)
    started: list[dict[str, object]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    one_shots: list[dict[str, object]] = field(default_factory=list)
    execs: list[ExecCall] = field(default_factory=list)

    def _answer(self, command: str) -> tuple[str, str, int]:
        for pattern, response in self.responses:
            if re.search(pattern, command):
                return response
        return self.default

    async def ensure_image(self, image: str, *, platform: str | None = None) -> None:
        self.pulled.append(image)

    async def run_one_shot(
        self,
        image: str,
        command: str,
        *,
        input: str | None = None,
        mounts: dict[str, str] | None = None,
        platform: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str, int]:
        self.one_shots.append(
            {"image": image, "command": command, "input": input, "mounts": mounts or {}}
        )
        return self._answer(command)

    async def start(
        self,
        image: str,
        *,
        mounts: dict[str, str] | None = None,
        platform: str | None = None,
    ) -> str:
        self.started.append({"image": image, "mounts": mounts or {}, "platform": platform})
        return f"container-{len(self.started)}"

    async def exec_in(
        self,
        container_id: str,
        command: str,
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str, int]:
        self.execs.append(ExecCall(command=command, input=input))
        return self._answer(command)

    async def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)

    # helpers
    def commands(self) -> list[str]:
        return [c.command for c in self.execs] + [
            str(s["command"]) for s in self.one_shots
        ]


QNA_OUT = "A: Ubuntu 22.04.3 LTS\nI: singular string\nT: 0.2 ms\n"
EVAL_OK = (r"-showtypes", (QNA_OUT, "", 0))
PREREQS_OK = (r"prereq-probe", ("dpkg-deb ar tar", "", 0))
RPM_PREREQS_OK = (r"prereq-probe", ("rpm2cpio cpio", "", 0))
PROBE_UBUNTU = (r"os-release", ("Linux\nubuntu debian", "", 0))
PROBE_ALMA = (r"os-release", ("Linux\nalmalinux rhel fedora centos", "", 0))


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    import platformdirs

    monkeypatch.setattr(platformdirs, "user_state_dir", lambda *a, **k: str(tmp_path / "state"))


@pytest.fixture
def resolved(tmp_path) -> ResolvedQna:
    artifact = tmp_path / "cache" / "BESAgent-11.0.6.137-ubuntu18.amd64.deb"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"fake deb")
    return ResolvedQna(version="11.0.6.137", artifact_path=artifact)


# --- one-shot evaluation ---------------------------------------------------


async def test_evaluate_in_a_one_shot_container():
    engine = FakeEngine(responses=[EVAL_OK])

    result = await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance(
        "name of operating system"
    )

    assert result.transport == "container"
    assert result.answers == ["Ubuntu 22.04.3 LTS"]
    assert result.error_kind is None
    assert len(engine.one_shots) == 1
    assert engine.started == [], "no persistent container needed without provisioning"


async def test_host_field_identifies_image_and_arch():
    engine = FakeEngine(responses=[EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, arch="arm64"
    ).evaluate_client_relevance("true")

    assert result.host == "container:ubuntu:22.04@arm64"


async def test_image_is_ensured_before_running():
    engine = FakeEngine(responses=[EVAL_OK])

    await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance("true")

    assert engine.pulled == ["ubuntu:22.04"]


async def test_client_relevance_piped_with_q_prefix_stripped():
    engine = FakeEngine(responses=[EVAL_OK])

    await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance(
        "Q: version of client"
    )

    assert engine.one_shots[0]["input"] == "version of client\n"


async def test_eval_command_uses_t_and_showtypes():
    engine = FakeEngine(responses=[EVAL_OK])

    await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance("true")

    assert "-t" in str(engine.one_shots[0]["command"])
    assert "-showtypes" in str(engine.one_shots[0]["command"])


async def test_relevance_error_maps_to_relevance(qna_output):
    engine = FakeEngine(responses=[(r"-showtypes", (qna_output("relevance_error"), "", 0))])

    result = await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance(
        "namez of it"
    )

    assert result.error_kind == ERROR_KIND_RELEVANCE


async def test_missing_qna_in_image_maps_to_bootstrap():
    engine = FakeEngine(responses=[(r"-showtypes", ("", "qna: command not found", 127))])

    result = await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance(
        "true"
    )

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "qna" in (result.error or "").lower()


async def test_other_nonzero_exit_maps_to_qna():
    engine = FakeEngine(responses=[(r"-showtypes", ("", "segmentation fault", 139))])

    result = await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance(
        "true"
    )

    assert result.error_kind == ERROR_KIND_QNA


async def test_engine_error_maps_to_transport():
    class BrokenEngine(FakeEngine):
        async def ensure_image(self, image, *, platform=None):
            raise ContainerEngineError("cannot connect to the Docker daemon")

    result = await TransportContainer(
        "ubuntu:22.04", engine=BrokenEngine()
    ).evaluate_client_relevance("true")

    assert result.error_kind == ERROR_KIND_TRANSPORT
    assert "daemon" in (result.error or "")


async def test_timeout_maps_to_transport():
    class SlowEngine(FakeEngine):
        async def run_one_shot(self, *a, **k):
            raise TimeoutError("container timed out")

    result = await TransportContainer("ubuntu:22.04", engine=SlowEngine()).evaluate_client_relevance(
        "true", timeout_s=0.1
    )

    assert result.error_kind == ERROR_KIND_TRANSPORT


async def test_no_exception_escapes():
    class ExplodingEngine(FakeEngine):
        async def run_one_shot(self, *a, **k):
            raise RuntimeError("unexpected")

    result = await TransportContainer(
        "ubuntu:22.04", engine=ExplodingEngine()
    ).evaluate_client_relevance("true")

    assert result.error_kind is not None


# --- provisioning a pinned version ----------------------------------------


async def test_provisioning_mounts_the_artifact_read_only(resolved):
    engine = FakeEngine(
        responses=[(r"bfrcr-complete", ("", "", 1)), PREREQS_OK, EVAL_OK]
    )

    await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu"
    ).evaluate_client_relevance("true", qna=resolved)

    assert engine.started, "provisioning needs a persistent container"
    mounts = engine.started[0]["mounts"]
    assert str(resolved.artifact_path.parent) in mounts
    assert mounts[str(resolved.artifact_path.parent)].endswith(":ro"), "cache must be read-only"


async def test_provisioning_extracts_from_the_mount(resolved):
    engine = FakeEngine(responses=[(r"bfrcr-complete", ("", "", 1)), PREREQS_OK, EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu"
    ).evaluate_client_relevance("true", qna=resolved)

    joined = " ; ".join(engine.commands())
    assert ARTIFACT_MOUNT in joined, "artifact should be read from the mount, not copied in"
    assert "dpkg-deb -x" in joined or "ar x" in joined
    assert result.qna_version == "11.0.6.137"


async def test_provisioned_qna_path_is_version_scoped(resolved):
    engine = FakeEngine(responses=[(r"bfrcr-complete", ("ok", "", 0)), EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu"
    ).evaluate_client_relevance("true", qna=resolved)

    assert "11.0.6.137" in result.qna_path
    assert result.qna_path.endswith("opt/BESClient/bin/qna")


async def test_baked_in_version_skips_extraction(resolved):
    """An image already carrying the version just runs it."""
    engine = FakeEngine(responses=[(r"bfrcr-complete", ("ok", "", 0)), EVAL_OK])

    await TransportContainer(
        "bigfix_ubuntu", engine=engine, target="ubuntu"
    ).evaluate_client_relevance("true", qna=resolved)

    assert not any("dpkg-deb -x" in c for c in engine.commands())


async def test_missing_prereq_in_image_maps_to_bootstrap(resolved):
    engine = FakeEngine(
        responses=[(r"bfrcr-complete", ("", "", 1)), (r"prereq-probe", ("", "", 0))]
    )

    result = await TransportContainer(
        "rockylinux:9", engine=engine, target="rhel"
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "install" in (result.error or "")


async def test_provisioning_container_is_stopped_when_not_keep_alive(resolved):
    engine = FakeEngine(responses=[(r"bfrcr-complete", ("ok", "", 0)), EVAL_OK])

    await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu"
    ).evaluate_client_relevance("true", qna=resolved)

    assert len(engine.stopped) == 1


# --- platform probing --------------------------------------------------------
#
# Issue #1: the transport used to assume "ubuntu" and silently hand rpm-family
# images the Debian agent. With no explicit target it must probe the image the
# way TransportSSH probes a host, and refuse rather than guess.

async def test_unspecified_target_probes_the_image_before_provisioning(resolved):
    engine = FakeEngine(
        responses=[PROBE_ALMA, (r"bfrcr-complete", ("", "", 1)), RPM_PREREQS_OK, EVAL_OK]
    )

    await TransportContainer("almalinux:9", engine=engine).evaluate_client_relevance(
        "true", qna=resolved
    )

    joined = " ; ".join(engine.commands())
    assert "rpm2cpio" in joined, "probed rpm-family image must extract with rpm tools"
    assert "dpkg-deb -x" not in joined, "the Debian agent path is the issue #1 bug"


async def test_probe_output_feeds_resolve_platform():
    engine = FakeEngine(responses=[PROBE_ALMA])

    platform = await TransportContainer("almalinux:9", engine=engine).resolve_platform()

    assert platform == "rhel"


async def test_probe_runs_once_per_transport_instance(resolved):
    engine = FakeEngine(
        responses=[PROBE_ALMA, (r"bfrcr-complete", ("ok", "", 0)), EVAL_OK]
    )
    transport = TransportContainer("almalinux:9", engine=engine)

    await transport.evaluate_client_relevance("true", qna=resolved)
    await transport.evaluate_client_relevance("true", qna=resolved)

    probes = [c for c in engine.commands() if "os-release" in c]
    assert len(probes) == 1, "probe result must be cached on the transport"


async def test_unclassifiable_probe_maps_to_bootstrap(resolved):
    engine = FakeEngine(responses=[(r"os-release", ("Linux\nsomething-exotic", "", 0))])

    result = await TransportContainer("weird:latest", engine=engine).evaluate_client_relevance(
        "true", qna=resolved
    )

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "platform" in (result.error or "").lower(), "error must point at the escape hatch"


async def test_probe_failure_maps_to_transport(resolved):
    class ProbeBrokenEngine(FakeEngine):
        async def run_one_shot(self, image, command, **kwargs):
            if "os-release" in command:
                raise ContainerEngineError("exec failed: no shell in image")
            return await super().run_one_shot(image, command, **kwargs)

    result = await TransportContainer(
        "distroless:latest", engine=ProbeBrokenEngine()
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_TRANSPORT


async def test_keep_alive_probe_reuses_the_running_container_flow(resolved):
    engine = FakeEngine(
        responses=[PROBE_ALMA, (r"bfrcr-complete", ("ok", "", 0)), EVAL_OK]
    )
    transport = TransportContainer("almalinux:9", engine=engine, keep_alive=True)

    await transport.evaluate_client_relevance("true", qna=resolved)
    await transport.evaluate_client_relevance("true", qna=resolved)

    probes = [c for c in engine.commands() if "os-release" in c]
    assert len(probes) == 1, "keep-alive evals share one probe"
    await transport.aclose()


async def test_explicit_target_skips_the_probe(resolved):
    engine = FakeEngine(responses=[(r"bfrcr-complete", ("ok", "", 0)), EVAL_OK])

    await TransportContainer("ubuntu:22.04", engine=engine, target="ubuntu").evaluate_client_relevance(
        "true", qna=resolved
    )

    assert not any("uname -s" in c for c in engine.commands())


async def test_one_shot_eval_without_qna_does_not_probe():
    """Without provisioning the platform is cosmetic; keep the cheap path cheap."""
    engine = FakeEngine(responses=[EVAL_OK])

    await TransportContainer("almalinux:9", engine=engine).evaluate_client_relevance("true")

    assert not any("uname -s" in c for c in engine.commands())
    assert len(engine.one_shots) == 1


# --- family sanity check -----------------------------------------------------
#
# A probed platform is authoritative, but an explicit --platform can be wrong,
# and a wrong one used to run to completion with the wrong agent (issue #1).
# Before provisioning, an explicit deb/rpm platform is checked against which
# package manager the image actually carries.

SANITY_RPM_ONLY = (r"command -v dpkg", ("rpm", "", 0))
SANITY_DEB_ONLY = (r"command -v dpkg", ("dpkg", "", 0))
SANITY_NEITHER = (r"command -v dpkg", ("", "", 0))


async def test_explicit_deb_platform_on_an_rpm_image_fails_loudly(resolved):
    engine = FakeEngine(responses=[SANITY_RPM_ONLY, (r"bfrcr-complete", ("ok", "", 0)), EVAL_OK])

    result = await TransportContainer(
        "almalinux:9", engine=engine, target="ubuntu"
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "rpm" in (result.error or "")
    assert "platform" in (result.error or "").lower()


async def test_explicit_rpm_platform_on_a_deb_image_fails_loudly(resolved):
    engine = FakeEngine(responses=[SANITY_DEB_ONLY, (r"bfrcr-complete", ("ok", "", 0)), EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, target="rhel"
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "dpkg" in (result.error or "") or "deb" in (result.error or "")


async def test_matching_explicit_platform_passes_the_sanity_check(resolved):
    engine = FakeEngine(responses=[SANITY_DEB_ONLY, (r"bfrcr-complete", ("ok", "", 0)), EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu"
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind is None
    assert any("command -v rpm" in c for c in engine.commands()), "check must actually run"


async def test_inconclusive_sanity_output_does_not_block(resolved):
    """No package manager found (busybox-ish): proceed, the prereq probe governs."""
    engine = FakeEngine(responses=[SANITY_NEITHER, (r"bfrcr-complete", ("ok", "", 0)), EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu"
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind is None
    assert any("command -v rpm" in c for c in engine.commands()), "check must actually run"


async def test_probed_platform_skips_the_sanity_check(resolved):
    """A probed platform came from the image itself; re-checking wastes an exec."""
    engine = FakeEngine(responses=[PROBE_ALMA, (r"bfrcr-complete", ("ok", "", 0)), EVAL_OK])

    result = await TransportContainer("almalinux:9", engine=engine).evaluate_client_relevance(
        "true", qna=resolved
    )

    assert result.error_kind is None
    assert not any("command -v dpkg" in c for c in engine.commands())


# --- keep-alive ------------------------------------------------------------


async def test_keep_alive_reuses_one_container():
    engine = FakeEngine(responses=[EVAL_OK])
    transport = TransportContainer("ubuntu:22.04", engine=engine, keep_alive=True)

    await transport.evaluate_client_relevance("true")
    await transport.evaluate_client_relevance("true")

    assert len(engine.started) == 1
    assert len(engine.execs) >= 2
    assert engine.stopped == [], "keep-alive container must survive between evals"

    await transport.aclose()
    assert len(engine.stopped) == 1


async def test_one_shot_is_the_default():
    engine = FakeEngine(responses=[EVAL_OK])
    transport = TransportContainer("ubuntu:22.04", engine=engine)

    await transport.evaluate_client_relevance("true")
    await transport.evaluate_client_relevance("true")

    assert len(engine.one_shots) == 2
    assert engine.started == []


async def test_platform_flag_passed_through_for_arch_emulation():
    engine = FakeEngine(responses=[EVAL_OK], default=("", "", 0))
    transport = TransportContainer("ubuntu:22.04", engine=engine, arch="arm64", keep_alive=True)

    await transport.evaluate_client_relevance("true")

    assert engine.started[0]["platform"] == "linux/arm64"


# --- DockerEngine client discovery ----------------------------------------


def test_candidate_sockets_cover_engines_the_sdk_would_miss():
    """The SDK assumes /var/run/docker.sock and ignores Docker contexts, so
    Colima and Rancher Desktop need their own paths tried."""
    from bigfix_remote_client_relevance.transports.container import candidate_docker_sockets

    candidates = candidate_docker_sockets()

    assert any("/var/run/docker.sock" in c for c in candidates)
    assert any(".docker/run/docker.sock" in c for c in candidates)
    assert any(".colima" in c for c in candidates)
    assert all(c.startswith("unix://") for c in candidates)


def test_docker_host_env_takes_precedence(monkeypatch):
    from bigfix_remote_client_relevance.transports.container import candidate_docker_sockets

    monkeypatch.setenv("DOCKER_HOST", "unix:///custom/docker.sock")

    assert candidate_docker_sockets()[0] == "unix:///custom/docker.sock"


async def test_engine_error_names_the_sockets_it_tried():
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    engine = DockerEngine(socket_candidates=["unix:///definitely/not/here.sock"])

    with pytest.raises(ContainerEngineError) as excinfo:
        await engine.ensure_image("ubuntu:22.04")

    assert "not/here.sock" in str(excinfo.value)


async def test_writes_nothing_to_stdout(capsys):
    engine = FakeEngine(responses=[EVAL_OK])

    await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance("true")

    assert capsys.readouterr().out == ""
