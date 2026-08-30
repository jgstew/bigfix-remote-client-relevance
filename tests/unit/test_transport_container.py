"""Tests for TransportContainer and the ContainerEngine seam.

Containers answer the "does this client relevance work on Ubuntu 22.04 / RHEL 9
/ Amazon Linux" question on demand, with no SSH credentials and no long-lived
VM per distro. The docker SDK sits behind ContainerEngine so all of that is
driven here without a daemon.
"""

from __future__ import annotations

import logging
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
    QNA_MOUNT,
    ContainerEngineError,
    TransportContainer,
)


@dataclass
class ExecCall:
    command: str
    input: str | None


def link_failure(soname: str) -> str:
    """The dynamic linker's message, as captured from rockylinux:9."""
    return (
        f"/opt/bigfix_qna/opt/BESClient/bin/qna: error while loading shared libraries: "
        f"{soname}: cannot open shared object file: No such file or directory"
    )


def qemu_interpreter_failure(interpreter_path: str) -> str:
    """qemu-user's message when a foreign-arch ELF interpreter is missing, as
    captured running the raspbian armhf agent under an arm64 debian:12."""
    return f"qemu-arm: Could not open '{interpreter_path}': No such file or directory"


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
    digest: str = "sha256:basedigest"
    existing_tags: set = field(default_factory=set)
    committed: list[tuple[str, str]] = field(default_factory=list)
    cp_exit_code: int = 0
    # Sonames the image is missing; a successful install consumes one, so a
    # list of two models "fixing one library reveals the next".
    missing_libs: list[str] = field(default_factory=list)
    # The foreign-arch ELF interpreter the image is missing (e.g.
    # "/lib/ld-linux-armhf.so.3"), or None once the whole-architecture fix
    # (dpkg --add-architecture + install) has landed. A field rather than a
    # list like missing_libs: only one interpreter is ever missing at once,
    # unlike a chain of shared-library dependencies.
    missing_interpreter: str | None = None
    # Set once `dpkg --add-architecture` runs; from then on a native package
    # no longer satisfies the (foreign-architecture) binary.
    foreign_arch_enabled: bool = False
    package_manager: str = "dnf"
    install_exit_code: int = 0
    installs: list[str] = field(default_factory=list)

    def _answer(self, command: str) -> tuple[str, str, int]:
        if "cp -a" in command:
            return ("", "" if self.cp_exit_code == 0 else "cp: no such shell", self.cp_exit_code)
        if "command -v dnf" in command:
            return (self.package_manager, "", 0)
        if " install " in command or "apt-get update" in command:
            self.installs.append(command)
            if "add-architecture" in command:
                self.foreign_arch_enabled = True
            if self.install_exit_code == 0 and " install " in command:
                if self.missing_interpreter is not None:
                    # Only the foreign-arch libc provides the interpreter.
                    if ":armhf" in command:
                        self.missing_interpreter = None
                elif self.missing_libs and (
                    # Once the binary is known to be a foreign architecture,
                    # only a foreign-arch package actually satisfies it --
                    # installing the native one changes nothing, which is the
                    # whole failure this models.
                    not self.foreign_arch_enabled or ":armhf" in command
                ):
                    self.missing_libs.pop(0)
            return (
                "",
                "" if self.install_exit_code == 0 else "No match for argument",
                self.install_exit_code,
            )
        # Any attempt to run qna fails while something is missing — the
        # build's link probe and the evaluation alike. Must precede the regex
        # table, since both carry -showtypes and would otherwise match
        # EVAL_OK.
        if "-showtypes" in command and self.missing_interpreter:
            return ("", qemu_interpreter_failure(self.missing_interpreter), 127)
        if "-showtypes" in command and self.missing_libs:
            return ("", link_failure(self.missing_libs[0]), 127)
        if "< /dev/null" in command:
            return ("", "", 0)
        for pattern, response in self.responses:
            if re.search(pattern, command):
                return response
        return self.default

    # Set to simulate DockerEngine.ensure_image's real return value -- a
    # local alias distinct from the upstream image -- so tests can prove
    # TransportContainer actually uses it rather than falling straight back
    # to self.image everywhere.
    resolved_image: str | None = None

    async def ensure_image(self, image: str, *, platform: str | None = None) -> str:
        self.pulled.append(image)
        return self.resolved_image or image

    async def image_digest(self, image: str) -> str:
        return self.digest

    async def image_exists(self, image: str) -> bool:
        return image in self.existing_tags

    async def commit(self, container_id: str, tag: str) -> None:
        self.committed.append((container_id, tag))
        self.existing_tags.add(tag)

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
            {
                "image": image,
                "command": command,
                "input": input,
                "mounts": mounts or {},
                "timeout": timeout,
            }
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
        return [c.command for c in self.execs] + [str(s["command"]) for s in self.one_shots]


QNA_OUT = "A: Ubuntu 22.04.3 LTS\nI: singular string\nT: 0.2 ms\n"
EVAL_OK = (r"-showtypes", (QNA_OUT, "", 0))
PROBE_UBUNTU = (r"os-release", ("Linux\nubuntu debian", "", 0))
PROBE_ALMA = (r"os-release", ("Linux\nalmalinux rhel fedora centos", "", 0))

# The CLI tests raise the package logger to WARNING, so log assertions must
# name the logger they care about rather than relying on the root level.
CONTAINER_LOGGER = "bigfix_remote_client_relevance.transports.container"


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

    result = await TransportContainer(
        "ubuntu:22.04", engine=SlowEngine()
    ).evaluate_client_relevance("true", timeout_s=0.1)

    assert result.error_kind == ERROR_KIND_TRANSPORT


async def test_no_exception_escapes():
    class ExplodingEngine(FakeEngine):
        async def run_one_shot(self, *a, **k):
            raise RuntimeError("unexpected")

    result = await TransportContainer(
        "ubuntu:22.04", engine=ExplodingEngine()
    ).evaluate_client_relevance("true")

    assert result.error_kind is not None


# --- platform probing --------------------------------------------------------
#
# Issue #1: the transport used to assume "ubuntu" and silently hand rpm-family
# images the Debian agent. With no explicit target it must probe the image the
# way TransportSSH probes a host, and refuse rather than guess.


async def test_unspecified_target_is_probed_before_extraction(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_ALMA, EVAL_OK])

    transport = TransportContainer("almalinux:9", engine=engine, extractor=extracted)
    await transport.evaluate_client_relevance("true", qna=resolved)

    assert any("os-release" in c for c in engine.commands()), "an unset target must be probed"
    assert transport._probed == "rhel"


async def test_probe_output_feeds_resolve_platform():
    engine = FakeEngine(responses=[PROBE_ALMA])

    platform = await TransportContainer("almalinux:9", engine=engine).resolve_platform()

    assert platform == "rhel"


async def test_probe_runs_once_per_transport_instance(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_ALMA, EVAL_OK])
    transport = TransportContainer("almalinux:9", engine=engine, extractor=extracted)

    await transport.evaluate_client_relevance("true", qna=resolved)
    await transport.evaluate_client_relevance("true", qna=resolved)

    probes = [c for c in engine.commands() if "os-release" in c]
    assert len(probes) == 1, "probe result must be cached on the transport"


async def test_unclassifiable_probe_maps_to_bootstrap(resolved, extracted):
    engine = FakeEngine(responses=[(r"os-release", ("Linux\nsomething-exotic", "", 0))])

    result = await TransportContainer(
        "weird:latest", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "platform" in (result.error or "").lower(), "error must point at the escape hatch"


async def test_probe_failure_maps_to_transport(resolved, extracted):
    class ProbeBrokenEngine(FakeEngine):
        async def run_one_shot(self, image, command, **kwargs):
            if "os-release" in command:
                raise ContainerEngineError("exec failed: no shell in image")
            return await super().run_one_shot(image, command, **kwargs)

    result = await TransportContainer(
        "distroless:latest", engine=ProbeBrokenEngine(), extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_TRANSPORT


async def test_keep_alive_probe_reuses_the_running_container_flow(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_ALMA, EVAL_OK])
    transport = TransportContainer(
        "almalinux:9", engine=engine, keep_alive=True, extractor=extracted
    )

    await transport.evaluate_client_relevance("true", qna=resolved)
    await transport.evaluate_client_relevance("true", qna=resolved)

    probes = [c for c in engine.commands() if "os-release" in c]
    assert len(probes) == 1, "keep-alive evals share one probe"
    await transport.aclose()


async def test_explicit_target_skips_the_probe(resolved, extracted):
    engine = FakeEngine(responses=[EVAL_OK])

    await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu", extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

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
# Before evaluation, an explicit deb/rpm platform is checked against which
# package manager the image actually carries.

SANITY_RPM_ONLY = (r"command -v dpkg", ("rpm", "", 0))
SANITY_DEB_ONLY = (r"command -v dpkg", ("dpkg", "", 0))
SANITY_NEITHER = (r"command -v dpkg", ("", "", 0))


async def test_explicit_deb_platform_on_an_rpm_image_fails_loudly(resolved, extracted):
    engine = FakeEngine(responses=[SANITY_RPM_ONLY, EVAL_OK])

    result = await TransportContainer(
        "almalinux:9", engine=engine, target="ubuntu", extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "rpm" in (result.error or "")
    assert "platform" in (result.error or "").lower()


async def test_explicit_rpm_platform_on_a_deb_image_fails_loudly(resolved, extracted):
    engine = FakeEngine(responses=[SANITY_DEB_ONLY, EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, target="rhel", extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "dpkg" in (result.error or "") or "deb" in (result.error or "")


async def test_matching_explicit_platform_passes_the_sanity_check(resolved, extracted):
    engine = FakeEngine(responses=[SANITY_DEB_ONLY, EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu", extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind is None
    assert any("command -v rpm" in c for c in engine.commands()), "check must actually run"


async def test_inconclusive_sanity_output_does_not_block(resolved, extracted):
    """No package manager found (busybox-ish): proceed rather than refuse."""
    engine = FakeEngine(responses=[SANITY_NEITHER, EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu", extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind is None
    assert any("command -v rpm" in c for c in engine.commands()), "check must actually run"


async def test_sanity_check_container_is_stopped_afterward(resolved, extracted):
    """The sanity check needs its own transient container, stopped afterward."""
    engine = FakeEngine(responses=[SANITY_DEB_ONLY, EVAL_OK])

    await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu", extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    # One container to build the prepared image, one for the sanity check.
    assert len(engine.started) == 2
    assert len(engine.stopped) == 2


async def test_probed_platform_skips_the_sanity_check(resolved, extracted):
    """A probed platform came from the image itself; re-checking wastes an exec."""
    engine = FakeEngine(responses=[PROBE_ALMA, EVAL_OK])

    result = await TransportContainer(
        "almalinux:9", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind is None
    assert not any("command -v dpkg" in c for c in engine.commands())
    assert len(engine.started) == 1, "only the prepared-image build needs a container here"


# --- controller-side extraction ----------------------------------------------
#
# Issue #1: unpacking inside the target made dpkg-deb/ar+tar or rpm2cpio+cpio a
# prerequisite of every image. The engine is local, so the artifact is unpacked
# on the controller and the resulting tree bind-mounted read-only instead.


@pytest.fixture
def extracted(tmp_path):
    """A stand-in for a controller-side extracted tree, plus a call counter."""
    tree = tmp_path / "extracted" / "11.0.6.137" / "ubuntu-x86_64"
    (tree / "opt" / "BESClient" / "bin").mkdir(parents=True)
    (tree / "opt" / "BESClient" / "bin" / "qna").write_text("#!/bin/sh\n")
    calls: list[object] = []

    async def extractor(qna):
        calls.append(qna)
        return tree

    extractor.tree = tree
    extractor.calls = calls
    return extractor


async def test_qna_run_needs_no_extraction_tools_in_the_image(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_ALMA, EVAL_OK])

    await TransportContainer(
        "rockylinux:9", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    joined = " ; ".join(engine.commands())
    for absent in ("prereq-probe", "rpm2cpio", "dpkg-deb", "bfrcr-complete"):
        assert absent not in joined, f"{absent} must no longer run inside the image"


async def test_eval_uses_the_prepared_qna_path(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.qna_path == "/opt/bigfix_qna/opt/BESClient/bin/qna"
    assert "/opt/bigfix_qna/opt/BESClient/bin/qna" in str(engine.one_shots[-1]["command"])


async def test_keep_alive_runs_the_prepared_image_with_no_mount(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])
    transport = TransportContainer(
        "ubuntu:22.04", engine=engine, keep_alive=True, extractor=extracted
    )

    await transport.evaluate_client_relevance("true", qna=resolved)

    persistent = engine.started[-1]
    assert persistent["image"] in engine.existing_tags
    assert persistent["mounts"] == {}
    await transport.aclose()


async def test_extraction_failure_reports_bootstrap_and_starts_nothing(resolved):
    from bigfix_remote_client_relevance.bootstrap.extract_local import LocalExtractionError

    async def failing_extractor(qna):
        raise LocalExtractionError("could not extract fixture.deb: bad magic")

    engine = FakeEngine(responses=[EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, target="ubuntu", extractor=failing_extractor
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "extract" in (result.error or "")
    assert engine.started == [] and engine.one_shots == []


async def test_provisioning_timeout_is_not_inflated(resolved, extracted):
    """The 300s floor covered an in-image unpack that no longer happens."""
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])

    await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved, timeout_s=12.0)

    assert engine.one_shots[-1]["timeout"] == 12.0


# --- a qna that cannot link ---------------------------------------------------
#
# The dynamic linker's message ends in "No such file or directory", which the
# missing-binary heuristic matches — so a present-but-unlinkable qna on
# rockylinux:9 was reported as "no qna in image". Captured from that image.

ROCKY_LINK_FAILURE = (
    "/opt/bigfix_qna/opt/BESClient/bin/qna: error while loading shared libraries: "
    "libdbus-1.so.3: cannot open shared object file: No such file or directory"
)


async def test_an_unlinkable_qna_is_not_reported_as_missing():
    engine = FakeEngine(responses=[(r"-showtypes", ("", ROCKY_LINK_FAILURE, 127))])

    result = await TransportContainer("rockylinux:9", engine=engine).evaluate_client_relevance(
        "true"
    )

    assert "no qna in image" not in (result.error or "")
    assert "libdbus-1.so.3" in (result.error or ""), "the error must name the library"


async def test_a_link_failure_says_what_to_do_about_it():
    engine = FakeEngine(responses=[(r"-showtypes", ("", ROCKY_LINK_FAILURE, 127))])

    result = await TransportContainer("rockylinux:9", engine=engine).evaluate_client_relevance(
        "true"
    )

    error = (result.error or "").lower()
    assert "shared library" in error, "the message should name the actual failure mode"
    assert "install" in error, "and point at the fix"


async def test_a_link_failure_is_still_a_bootstrap_problem():
    """An image that cannot run the binary is a provisioning problem either way."""
    engine = FakeEngine(responses=[(r"-showtypes", ("", ROCKY_LINK_FAILURE, 127))])

    result = await TransportContainer("rockylinux:9", engine=engine).evaluate_client_relevance(
        "true"
    )

    assert result.error_kind == ERROR_KIND_BOOTSTRAP


async def test_a_genuinely_missing_qna_still_reads_as_missing():
    engine = FakeEngine(responses=[(r"-showtypes", ("", "qna: command not found", 127))])

    result = await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance(
        "true"
    )

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "no qna in image" in (result.error or "")


async def test_exit_126_still_reads_as_a_missing_qna():
    engine = FakeEngine(responses=[(r"-showtypes", ("", "permission denied", 126))])

    result = await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance(
        "true"
    )

    assert "no qna in image" in (result.error or "")


# --- prepared-image cache -----------------------------------------------------
#
# Issue #1: provisioning per run is the wrong unit of work for a matrix sweep.
# The first qna run against an image builds a derived image with the tree
# already baked in; later runs against the same (image digest, version, arch)
# start it directly, no mount, no unpack, sub-second start.


async def test_prepared_image_tag_scheme():
    from bigfix_remote_client_relevance.transports.container import prepared_image_tag

    tag = prepared_image_tag("sha256:" + "ab" * 32, "11.0.6.137", "x86_64")

    assert tag.startswith("bfrcr/prepared:")
    assert "11.0.6.137" in tag
    assert "x86_64" in tag
    # truncated, not the full 64-char digest
    assert "ab" * 32 not in tag


async def test_first_qna_run_builds_a_prepared_image(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])

    await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert len(engine.started) == 1, "the base image starts once to build the prepared image"
    build_mounts = engine.started[0]["mounts"]
    assert next(iter(build_mounts.values())).endswith(":ro")
    assert any("cp -a" in c for c in engine.commands())
    assert len(engine.committed) == 1
    tag = engine.committed[0][1]
    assert engine.one_shots[-1]["image"] == tag
    assert engine.one_shots[-1]["mounts"] == {}, "the prepared image needs no mount"


async def test_second_run_skips_the_build(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])
    transport = TransportContainer("ubuntu:22.04", engine=engine, extractor=extracted)

    await transport.evaluate_client_relevance("true", qna=resolved)
    await transport.evaluate_client_relevance("true", qna=resolved)

    assert len(engine.started) == 1, "only the first run builds"
    assert len(engine.committed) == 1
    assert len(extracted.calls) == 1, "a cached prepared image needs no re-extraction"


async def test_rebuild_image_forces_a_rebuild(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])

    await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)
    await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted, rebuild_image=True
    ).evaluate_client_relevance("true", qna=resolved)

    assert len(engine.committed) == 2


async def test_base_digest_change_produces_a_different_tag(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK], digest="sha256:first")
    await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    engine.digest = "sha256:second"
    await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    tags = {tag for _cid, tag in engine.committed}
    assert len(tags) == 2, "a moved base image must not reuse a stale prepared image"


async def test_build_failure_falls_back_to_the_mount_flow(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK], cp_exit_code=1)

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert any("cp -a" in c for c in engine.commands()), "a build must actually be attempted"
    assert result.error_kind is None, "a distroless base without cp/coreutils still works"
    assert engine.committed == []
    assert engine.one_shots[-1]["mounts"] == {str(extracted.tree): f"{QNA_MOUNT}:ro"}


# --- installing what the image is missing --------------------------------------
#
# rockylinux:9 and amazonlinux:2023 have no libdbus-1.so.3, so a correctly
# extracted qna still cannot start. The fix is installed while the prepared
# image is being built, so it is committed once and every later run gets it
# free. Gated by --no-auto-setup for air-gapped hosts.


async def test_a_missing_library_is_installed_into_the_prepared_image(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_ALMA, EVAL_OK], missing_libs=["libdbus-1.so.3"])

    result = await TransportContainer(
        "rockylinux:9", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert any("dbus-libs" in c for c in engine.installs), "the library must be installed"
    assert len(engine.committed) == 1, "and baked into the prepared image"
    assert result.error_kind is None


async def test_the_link_probe_runs_the_binary_from_the_prepared_tree(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_ALMA, EVAL_OK])

    await TransportContainer(
        "rockylinux:9", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    probes = [c for c in engine.commands() if "< /dev/null" in c]
    assert probes, "the build must check the binary actually links"
    assert QNA_MOUNT in probes[0]


async def test_installs_repeat_until_the_binary_links(resolved, extracted):
    """Installing one library commonly reveals the next."""
    engine = FakeEngine(
        responses=[PROBE_ALMA, EVAL_OK], missing_libs=["libdbus-1.so.3", "libnsl.so.1"]
    )

    result = await TransportContainer(
        "rockylinux:9", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert len(engine.installs) == 2
    assert len(engine.committed) == 1
    assert result.error_kind is None


async def test_the_install_loop_is_bounded(resolved, extracted):
    class NeverFixedEngine(FakeEngine):
        def _answer(self, command):
            # An install that reports success but changes nothing.
            if " install " in command:
                self.installs.append(command)
                return ("", "", 0)
            return super()._answer(command)

    engine = NeverFixedEngine(responses=[PROBE_ALMA, EVAL_OK], missing_libs=["libdbus-1.so.3"])

    result = await TransportContainer(
        "rockylinux:9", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert len(engine.installs) <= 3, "a broken image must not loop forever"
    assert engine.committed == [], "and must not be cached in a broken state"
    assert "libdbus-1.so.3" in (result.error or "")


async def test_the_deb_family_refreshes_the_index_before_installing(resolved, extracted):
    """Debian images ship no package lists at all."""
    engine = FakeEngine(
        responses=[PROBE_UBUNTU, EVAL_OK],
        missing_libs=["libdbus-1.so.3"],
        package_manager="apt-get",
    )

    await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert engine.installs[0].startswith("apt-get update")
    assert "libdbus-1-3" in engine.installs[1]
    assert sum("apt-get update" in c for c in engine.installs) == 1, "refresh once per build"


async def test_no_auto_setup_installs_nothing_and_names_the_library(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_ALMA, EVAL_OK], missing_libs=["libdbus-1.so.3"])

    result = await TransportContainer(
        "rockylinux:9", engine=engine, extractor=extracted, auto_setup=False
    ).evaluate_client_relevance("true", qna=resolved)

    assert engine.installs == []
    assert engine.committed == []
    assert "libdbus-1.so.3" in (result.error or "")


async def test_a_failed_install_does_not_commit_a_broken_image(resolved, extracted):
    engine = FakeEngine(
        responses=[PROBE_ALMA, EVAL_OK], missing_libs=["libdbus-1.so.3"], install_exit_code=1
    )

    result = await TransportContainer(
        "rockylinux:9", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert engine.committed == [], "a broken image must never be cached"
    assert engine.one_shots[-1]["mounts"] == {str(extracted.tree): f"{QNA_MOUNT}:ro"}
    assert "libdbus-1.so.3" in (result.error or "")


async def test_the_build_gets_a_longer_timeout_than_the_evaluation(resolved, extracted):
    """A package install routinely outlasts a 30s evaluation budget."""

    class TimeoutRecordingEngine(FakeEngine):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.exec_timeouts: list[float | None] = []

        async def exec_in(self, container_id, command, *, input=None, timeout=None):
            self.exec_timeouts.append(timeout)
            return await super().exec_in(container_id, command, input=input, timeout=timeout)

    engine = TimeoutRecordingEngine(
        responses=[PROBE_ALMA, EVAL_OK], missing_libs=["libdbus-1.so.3"]
    )

    await TransportContainer(
        "rockylinux:9", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved, timeout_s=30.0)

    install_timeouts = [
        t
        for call, t in zip(engine.execs, engine.exec_timeouts, strict=True)
        if " install " in call.command
    ]
    assert install_timeouts, "an install must have run"
    assert all(t is not None and t > 30.0 for t in install_timeouts)


async def test_a_linkable_image_installs_nothing(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_ALMA, EVAL_OK])

    await TransportContainer(
        "rockylinux:9", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert engine.installs == []
    assert len(engine.committed) == 1


# --- installing a missing foreign architecture (raspbian-on-arm64) -------------
#
# Running the raspbian armhf (32-bit ARM) agent under an arm64 Debian/Ubuntu
# container needs a whole architecture enabled, not one library -- a
# different remediation shape from the soname case above (dpkg
# --add-architecture + install, not just install).


async def test_a_missing_interpreter_is_fixed_by_enabling_the_foreign_arch(resolved, extracted):
    engine = FakeEngine(
        responses=[PROBE_UBUNTU, EVAL_OK],
        missing_interpreter="/lib/ld-linux-armhf.so.3",
        package_manager="apt-get",
    )

    result = await TransportContainer(
        "debian:12", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert any("dpkg --add-architecture armhf" in c for c in engine.installs)
    assert any("libc6:armhf" in c for c in engine.installs)
    assert len(engine.committed) == 1
    assert result.error_kind is None


async def test_an_unmapped_interpreter_is_reported_not_guessed(resolved, extracted):
    engine = FakeEngine(
        responses=[PROBE_UBUNTU, EVAL_OK],
        missing_interpreter="/lib/ld-linux-riscv64-lp64d.so.1",
        package_manager="apt-get",
    )

    result = await TransportContainer(
        "debian:12", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert engine.installs == []
    assert engine.committed == []
    assert "ld-linux-riscv64-lp64d.so.1" in (result.error or "")


async def test_a_missing_interpreter_on_an_rpm_family_image_is_refused(resolved, extracted):
    """dpkg --add-architecture is a deb-family mechanism; there is nothing to
    fall back to on rpm-family images, so this must refuse rather than try
    dnf/yum against a foreign-arch package name they don't understand."""
    engine = FakeEngine(
        responses=[PROBE_ALMA, EVAL_OK], missing_interpreter="/lib/ld-linux-armhf.so.3"
    )

    result = await TransportContainer(
        "rockylinux:9", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert engine.installs == []
    assert engine.committed == []
    assert result.error_kind == ERROR_KIND_BOOTSTRAP


async def test_libraries_needed_after_a_foreign_arch_switch_use_that_arch(resolved, extracted):
    """The gap that made this whole path useless in practice: enabling armhf
    and installing libc6:armhf gets qna far enough to report its *next*
    missing library -- which is also armhf. Installing the native package for
    it changes nothing, the probe reports the same soname again, and the
    build gives up and discards the container, so the evaluation runs against
    a pristine image and reports the original interpreter error."""
    engine = FakeEngine(
        responses=[PROBE_UBUNTU, EVAL_OK],
        missing_interpreter="/lib/ld-linux-armhf.so.3",
        missing_libs=["libstdc++.so.6"],
        package_manager="apt-get",
    )

    result = await TransportContainer(
        "ubuntu:24.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert any("libc6:armhf" in c for c in engine.installs)
    assert any("libstdc++6:armhf" in c for c in engine.installs), (
        "the follow-up library must be installed for armhf, not the native arch"
    )
    assert not any("libstdc++6 " in c for c in engine.installs), (
        "the native package would not satisfy an armhf binary"
    )
    assert len(engine.committed) == 1, "the prepared image must actually be usable"
    assert result.error_kind is None


async def test_no_auto_setup_installs_nothing_for_a_missing_interpreter(resolved, extracted):
    engine = FakeEngine(
        responses=[PROBE_UBUNTU, EVAL_OK],
        missing_interpreter="/lib/ld-linux-armhf.so.3",
        package_manager="apt-get",
    )

    result = await TransportContainer(
        "debian:12", engine=engine, extractor=extracted, auto_setup=False
    ).evaluate_client_relevance("true", qna=resolved)

    assert engine.installs == []
    assert engine.committed == []
    assert "ld-linux-armhf.so.3" in (result.error or "")


# --- emulation is never silent -------------------------------------------------
#
# The release site publishes no arm64 agent for any platform this tool targets,
# so on Apple Silicon every container run is emulated. That is the only thing
# available, but it is slow and can behave differently from native — and it
# used to happen with no indication at all.


async def test_running_a_foreign_architecture_says_so(caplog, monkeypatch):
    import bigfix_remote_client_relevance.transports.container as container_module

    monkeypatch.setattr(container_module, "host_arch", lambda: "arm64")
    engine = FakeEngine(responses=[EVAL_OK])

    with caplog.at_level(logging.INFO, logger=CONTAINER_LOGGER):
        await TransportContainer(
            "ubuntu:22.04", engine=engine, arch="x86_64"
        ).evaluate_client_relevance("true")

    assert any("emulat" in r.message.lower() for r in caplog.records), (
        "an x86_64 container on an arm64 host is emulated and must say so"
    )


async def test_a_native_architecture_says_nothing(caplog, monkeypatch):
    import bigfix_remote_client_relevance.transports.container as container_module

    monkeypatch.setattr(container_module, "host_arch", lambda: "arm64")
    engine = FakeEngine(responses=[EVAL_OK])

    with caplog.at_level(logging.INFO, logger=CONTAINER_LOGGER):
        await TransportContainer(
            "ubuntu:22.04", engine=engine, arch="arm64"
        ).evaluate_client_relevance("true")

    assert not any("emulat" in r.message.lower() for r in caplog.records)


async def test_the_emulation_notice_is_logged_once_per_transport(caplog, monkeypatch):
    import bigfix_remote_client_relevance.transports.container as container_module

    monkeypatch.setattr(container_module, "host_arch", lambda: "arm64")
    engine = FakeEngine(responses=[EVAL_OK])
    transport = TransportContainer("ubuntu:22.04", engine=engine, arch="x86_64")

    with caplog.at_level(logging.INFO, logger=CONTAINER_LOGGER):
        await transport.evaluate_client_relevance("true")
        await transport.evaluate_client_relevance("true")

    notices = [r for r in caplog.records if "emulat" in r.message.lower()]
    assert len(notices) == 1, "one notice per target, not one per evaluation"


async def test_aarch64_selects_the_arm64_docker_platform():
    """uname says aarch64; Docker only understands linux/arm64."""
    engine = FakeEngine(responses=[EVAL_OK], default=("", "", 0))

    transport = TransportContainer("ubuntu:22.04", engine=engine, arch="aarch64", keep_alive=True)
    await transport.evaluate_client_relevance("true")

    assert engine.started[0]["platform"] == "linux/arm64"
    await transport.aclose()


async def test_x86_64_still_selects_the_amd64_docker_platform():
    engine = FakeEngine(responses=[EVAL_OK], default=("", "", 0))

    transport = TransportContainer("ubuntu:22.04", engine=engine, arch="x86_64", keep_alive=True)
    await transport.evaluate_client_relevance("true")

    assert engine.started[0]["platform"] == "linux/amd64"
    await transport.aclose()


# --- sharing image work across a fan-out ---------------------------------------
#
# A transport is built per (target, version) pair, so two versions of one image
# would otherwise pull it twice and build the same prepared image twice.


def coordinator():
    from bigfix_remote_client_relevance.transports.coordination import ImageCoordinator

    return ImageCoordinator()


async def test_two_transports_sharing_a_coordinator_pull_once(extracted):
    import asyncio

    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])
    shared = coordinator()

    def transport():
        return TransportContainer(
            "ubuntu:22.04", engine=engine, extractor=extracted, coordinator=shared
        )

    await asyncio.gather(
        transport().evaluate_client_relevance("true"),
        transport().evaluate_client_relevance("true"),
    )

    assert engine.pulled == ["ubuntu:22.04"], "one image, one pull"


async def test_a_warm_prepared_image_still_dedupes_the_pull(resolved, extracted):
    """The pull happens on every path, cache hit or not."""
    import asyncio

    from bigfix_remote_client_relevance.transports.container import prepared_image_tag

    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])
    engine.existing_tags.add(prepared_image_tag(engine.digest, "11.0.6.137", "x86_64"))
    shared = coordinator()

    def transport():
        return TransportContainer(
            "ubuntu:22.04", engine=engine, extractor=extracted, coordinator=shared
        )

    await asyncio.gather(
        transport().evaluate_client_relevance("true", qna=resolved),
        transport().evaluate_client_relevance("true", qna=resolved),
    )

    assert engine.pulled == ["ubuntu:22.04"]


async def test_two_transports_build_one_prepared_image(resolved, extracted):
    import asyncio

    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])
    shared = coordinator()

    def transport():
        return TransportContainer(
            "ubuntu:22.04", engine=engine, extractor=extracted, coordinator=shared
        )

    await asyncio.gather(
        transport().evaluate_client_relevance("true", qna=resolved),
        transport().evaluate_client_relevance("true", qna=resolved),
    )

    assert len(engine.committed) == 1, "one prepared image, not one per transport"
    assert len(extracted.calls) == 1, "and one extraction"


async def test_different_versions_are_not_shared(resolved, extracted, tmp_path):
    """Different versions are genuinely different images."""
    import asyncio

    other_artifact = tmp_path / "cache" / "BESAgent-9.5.22.10-ubuntu18.amd64.deb"
    other_artifact.parent.mkdir(parents=True, exist_ok=True)
    other_artifact.write_bytes(b"fake deb")
    other = ResolvedQna(version="9.5.22.10", artifact_path=other_artifact)

    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])
    shared = coordinator()

    def transport():
        return TransportContainer(
            "ubuntu:22.04", engine=engine, extractor=extracted, coordinator=shared
        )

    await asyncio.gather(
        transport().evaluate_client_relevance("true", qna=resolved),
        transport().evaluate_client_relevance("true", qna=other),
    )

    assert len({tag for _cid, tag in engine.committed}) == 2


async def test_the_prepare_hook_carries_into_the_evaluation(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])
    transport = TransportContainer("ubuntu:22.04", engine=engine, extractor=extracted)

    await transport.prepare(qna=resolved, timeout_s=5.0)
    await transport.evaluate_client_relevance("true", qna=resolved)

    assert len(engine.committed) == 1, "prepare did the build, the evaluation reused it"
    assert str(engine.one_shots[-1]["image"]).startswith("bfrcr/prepared:")


async def test_preparing_one_version_is_not_reused_for_another(resolved, extracted, tmp_path):
    other_artifact = tmp_path / "cache" / "BESAgent-9.5.22.10-ubuntu18.amd64.deb"
    other_artifact.parent.mkdir(parents=True, exist_ok=True)
    other_artifact.write_bytes(b"fake deb")
    other = ResolvedQna(version="9.5.22.10", artifact_path=other_artifact)

    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])
    transport = TransportContainer("ubuntu:22.04", engine=engine, extractor=extracted)

    await transport.prepare(qna=resolved, timeout_s=5.0)
    await transport.evaluate_client_relevance("true", qna=other)

    assert len(engine.committed) == 2, "the other version needs its own build"


async def test_a_transport_without_a_coordinator_still_works(resolved, extracted):
    """Callers outside the orchestrator get correct behaviour, just no sharing."""
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])

    result = await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert result.error_kind is None
    assert len(engine.committed) == 1


# --- using the resolved (aliased) image, not the bare upstream tag ------------
#
# ensure_image returns the reference to actually use -- a local alias tag
# when DockerEngine had to disambiguate a platform (see _local_pull_tag) --
# and every later operation on this image must use it, or the whole point of
# aliasing (surviving a concurrent pull of the same tag at another platform)
# is lost the moment anything falls back to self.image.


async def test_one_shot_eval_runs_the_resolved_image_not_the_bare_tag():
    engine = FakeEngine(responses=[EVAL_OK])
    engine.resolved_image = "bfrcr/base:deadbeef"

    await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance("true")

    assert engine.one_shots[-1]["image"] == "bfrcr/base:deadbeef"
    assert engine.pulled == ["ubuntu:22.04"], "the upstream image is still what gets pulled"


async def test_keep_alive_container_starts_the_resolved_image():
    engine = FakeEngine(responses=[EVAL_OK])
    engine.resolved_image = "bfrcr/base:deadbeef"

    await TransportContainer(
        "ubuntu:22.04", engine=engine, keep_alive=True
    ).evaluate_client_relevance("true")

    assert engine.started[0]["image"] == "bfrcr/base:deadbeef"


async def test_platform_probe_runs_against_the_resolved_image(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])
    engine.resolved_image = "bfrcr/base:deadbeef"

    await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    probes = [c for c in engine.one_shots if "os-release" in str(c["command"])]
    assert probes and probes[0]["image"] == "bfrcr/base:deadbeef"


async def test_prepared_image_build_digests_and_starts_the_resolved_image(resolved, extracted):
    engine = FakeEngine(responses=[PROBE_UBUNTU, EVAL_OK])
    engine.resolved_image = "bfrcr/base:deadbeef"

    await TransportContainer(
        "ubuntu:22.04", engine=engine, extractor=extracted
    ).evaluate_client_relevance("true", qna=resolved)

    assert engine.started[0]["image"] == "bfrcr/base:deadbeef", (
        "the build container must start from the resolved image"
    )


# --- image architecture ------------------------------------------------------


@dataclass
class FakeImage:
    attrs: dict[str, object]
    id: str = "sha256:fakeimagedigest"
    tags_created: list[str] = field(default_factory=list)

    def tag(self, repository: str, tag: str | None = None) -> bool:
        """Overridden per-instance by FakeDockerClient.pull() so a tag
        actually registers under client._existing, the way the real SDK's
        Image.tag() registers a new local reference with the daemon."""
        raise AssertionError("tag() called on an image FakeDockerClient never pulled")


class FakeDockerClient:
    """Just enough of the docker SDK to drive ensure_image.

    Keyed by name (a real Docker daemon has exactly one image per tag), so a
    ``pull`` for one platform can be made to repoint the same upstream tag a
    concurrent ``pull`` for a different platform is also using -- the exact
    race ``ensure_image``'s local aliasing has to survive.
    """

    def __init__(
        self,
        existing: dict[str, FakeImage] | None = None,
        *,
        pull_result: FakeImage | None = None,
    ) -> None:
        self.images = self
        self._existing = dict(existing or {})
        self.pulled: list[dict[str, object]] = []
        self._pull_result = pull_result or FakeImage(attrs={"Os": "linux", "Architecture": "amd64"})

    def get(self, image: str) -> FakeImage:
        import docker.errors

        try:
            return self._existing[image]
        except KeyError:
            raise docker.errors.ImageNotFound(image) from None

    def pull(self, image: str, platform: str | None = None) -> FakeImage:
        self.pulled.append({"image": image, "platform": platform})
        # The real SDK returns the pulled Image directly -- independent of
        # whatever the upstream tag ends up pointing at afterward, which is
        # exactly what lets ensure_image alias it safely regardless of a
        # concurrent pull for another platform repointing the same tag.
        pulled = self._pull_result
        self._existing[image] = pulled

        client = self

        def _tag(repository: str, tag: str | None = None) -> bool:
            # The real Image.tag() registers a new local reference with the
            # daemon -- keyed by the pulled image's own identity, not by
            # re-reading `image` off self._existing (which may already have
            # been repointed by a concurrent pull for another platform).
            full = f"{repository}:{tag}" if tag else repository
            pulled.tags_created.append(full)
            client._existing[full] = pulled
            return True

        pulled.tag = _tag  # type: ignore[method-assign]
        return pulled

    def ping(self) -> bool:
        return True


def engine_with(client: FakeDockerClient):
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    return DockerEngine(client=client)


async def test_ensure_image_without_a_platform_behaves_as_before():
    """No platform means nothing to disambiguate -- no aliasing, plain pull."""
    client = FakeDockerClient()

    resolved = await engine_with(client).ensure_image("ubuntu:24.04")

    assert resolved == "ubuntu:24.04"
    assert client.pulled == [{"image": "ubuntu:24.04", "platform": None}]


async def test_ensure_image_does_not_pull_when_already_cached_for_that_platform():
    client = FakeDockerClient()
    engine = engine_with(client)
    first = await engine.ensure_image("ubuntu:24.04", platform="linux/amd64")
    client.pulled.clear()

    second = await engine.ensure_image("ubuntu:24.04", platform="linux/amd64")

    assert client.pulled == [], "the local alias from the first call must be reused"
    assert second == first


async def test_ensure_image_gives_each_platform_a_distinct_local_alias():
    """The whole point: two platforms of the same tag must not collide."""
    client = FakeDockerClient()
    engine = engine_with(client)

    amd64 = await engine.ensure_image("ubuntu:24.04", platform="linux/amd64")
    arm64 = await engine.ensure_image("ubuntu:24.04", platform="linux/arm64")

    assert amd64 != arm64
    assert amd64 != "ubuntu:24.04"
    assert arm64 != "ubuntu:24.04"


async def test_ensure_image_survives_a_concurrent_repoint_of_the_shared_tag():
    """The regression this exists for: pulling the same upstream tag at two
    platforms must not let whichever pull finishes last steal the other's
    alias, even though both share Docker's one mutable tag pointer."""
    amd64_image = FakeImage(attrs={"Os": "linux", "Architecture": "amd64"})
    arm64_image = FakeImage(attrs={"Os": "linux", "Architecture": "arm64"})
    client = FakeDockerClient(pull_result=amd64_image)
    engine = engine_with(client)

    amd64_tag = await engine.ensure_image("debian:12", platform="linux/amd64")

    # Simulates a concurrent pull for arm64 repointing the shared upstream
    # tag *after* the amd64 alias was already created above.
    client._pull_result = arm64_image
    arm64_tag = await engine.ensure_image("debian:12", platform="linux/arm64")

    # Both aliases must still resolve to the image actually pulled for them,
    # regardless of what the shared "debian:12" tag points to by now.
    assert _image_attrs(client.get(amd64_tag)) == {"Os": "linux", "Architecture": "amd64"}
    assert _image_attrs(client.get(arm64_tag)) == {"Os": "linux", "Architecture": "arm64"}


def _image_attrs(image: FakeImage) -> dict[str, object]:
    return image.attrs


async def test_ensure_image_repulls_when_the_local_alias_is_the_wrong_architecture():
    """Defensive: the alias is keyed deterministically by (image, platform),
    so this should never happen organically, but a corrupted or manually
    retagged local image must not be trusted silently."""
    client = FakeDockerClient(pull_result=FakeImage(attrs={"Os": "linux", "Architecture": "amd64"}))
    engine = engine_with(client)
    amd64_tag = await engine.ensure_image("ubuntu:24.04", platform="linux/amd64")
    # Corrupt it: the alias this next call looks up now claims the wrong arch.
    client._existing[amd64_tag] = FakeImage(attrs={"Os": "linux", "Architecture": "arm64"})
    client.pulled.clear()

    resolved = await engine.ensure_image("ubuntu:24.04", platform="linux/amd64")

    assert client.pulled, "a mismatched alias must be re-pulled, not trusted"
    assert _image_attrs(client.get(resolved)) == {"Os": "linux", "Architecture": "amd64"}


# --- transient daemon faults are retried, not fatal --------------------------
#
# Docker Desktop intermittently answers with a 5xx under concurrent container
# churn -- the identical call succeeds moments later (confirmed live: an
# inspect that 500'd mid-run succeeded immediately afterward). Treating those
# as fatal failed whole targets; treating a failed *removal* as fatal leaked
# the container, so every later run faced a busier daemon and failed more.


def api_error(status: int = 500) -> Exception:
    """A docker APIError carrying a real status code, as the daemon sends."""
    import docker.errors
    import requests

    response = requests.Response()
    response.status_code = status
    return docker.errors.APIError(f"{status} Server Error", response=response)


@pytest.fixture
def no_backoff(monkeypatch):
    """Retry without the real sleep, so these stay fast."""
    from bigfix_remote_client_relevance.transports import container as container_module

    monkeypatch.setattr(container_module, "_TRANSIENT_BACKOFF_S", 0)


async def test_a_transient_inspect_failure_is_retried_rather_than_failing(no_backoff):
    """A cached alias that 500s once must be found on the retry -- not
    re-pulled, which would spend minutes on a fault that clears in under a
    second."""
    from bigfix_remote_client_relevance.transports.container import _local_pull_tag

    alias = _local_pull_tag("ubuntu:24.04", "linux/amd64")
    assert alias is not None
    client = FakeDockerClient(
        existing={alias: FakeImage(attrs={"Os": "linux", "Architecture": "amd64"})}
    )
    calls = {"n": 0}
    real_get = client.get

    def flaky_get(image: str) -> FakeImage:
        calls["n"] += 1
        if calls["n"] == 1:
            raise api_error(500)
        return real_get(image)

    client.get = flaky_get  # type: ignore[method-assign]

    resolved = await engine_with(client).ensure_image("ubuntu:24.04", platform="linux/amd64")

    assert calls["n"] == 2, "the 500 must be retried, not surfaced"
    assert client.pulled == [], "a retry that succeeds must not also re-pull"
    assert resolved == alias


async def test_a_persistent_daemon_fault_still_fails_rather_than_retrying_forever(no_backoff):
    client = FakeDockerClient()

    def always_500(image: str) -> FakeImage:
        raise api_error(500)

    client.get = always_500  # type: ignore[method-assign]

    with pytest.raises(ContainerEngineError):
        await engine_with(client).ensure_image("ubuntu:24.04", platform="linux/amd64")


async def test_a_404_is_never_retried(no_backoff):
    """ImageNotFound is a real answer about what exists, not a fault -- so a
    legitimate cache miss must not pay the retry delay."""
    client = FakeDockerClient()
    calls = {"n": 0}
    real_get = client.get

    def counting_get(image: str) -> FakeImage:
        calls["n"] += 1
        return real_get(image)

    client.get = counting_get  # type: ignore[method-assign]

    await engine_with(client).ensure_image("ubuntu:24.04", platform="linux/amd64")

    assert calls["n"] == 1, "a 404 means absent; retrying it just wastes time"
    assert client.pulled, "and it must pull, as before"


async def test_image_digest_survives_a_transient_fault(no_backoff):
    client = FakeDockerClient(existing={"bfrcr/base:x": FakeImage(attrs={})})
    calls = {"n": 0}
    real_get = client.get

    def flaky_get(image: str) -> FakeImage:
        calls["n"] += 1
        if calls["n"] == 1:
            raise api_error(500)
        return real_get(image)

    client.get = flaky_get  # type: ignore[method-assign]

    digest = await engine_with(client).image_digest("bfrcr/base:x")

    assert digest, "a transient 500 inspecting the alias must not fail the target"


async def test_image_exists_survives_a_transient_fault(no_backoff):
    client = FakeDockerClient(existing={"bfrcr/base:x": FakeImage(attrs={})})
    calls = {"n": 0}
    real_get = client.get

    def flaky_get(image: str) -> FakeImage:
        calls["n"] += 1
        if calls["n"] == 1:
            raise api_error(500)
        return real_get(image)

    client.get = flaky_get  # type: ignore[method-assign]

    assert await engine_with(client).image_exists("bfrcr/base:x") is True


# --- a container that fails to remove is a leak, and leaks compound ----------


class FakeRemovableContainer:
    def __init__(self, container_id: str, *, fail_removes: int = 0) -> None:
        self.id = container_id
        self.removed = False
        self.remove_attempts = 0
        self._fail_removes = fail_removes

    def remove(self, force: bool = False) -> None:
        self.remove_attempts += 1
        if self.remove_attempts <= self._fail_removes:
            raise api_error(500)
        self.removed = True


class FakeContainerClient:
    """Just enough of the docker SDK to drive stop()."""

    def __init__(self, container: FakeRemovableContainer) -> None:
        self.containers = self
        self._container = container

    def get(self, container_id: str) -> FakeRemovableContainer:
        return self._container

    def ping(self) -> bool:
        return True


async def test_a_transient_removal_failure_is_retried_so_nothing_leaks(no_backoff):
    """The feedback loop this closes: a leaked container makes the daemon
    busier, which makes the next removal likelier to fail, and so on."""
    container = FakeRemovableContainer("abc123", fail_removes=1)

    await engine_with(FakeContainerClient(container)).stop("abc123")

    assert container.removed, "a transient 500 must not strand the container"
    assert container.remove_attempts == 2


async def test_a_container_that_truly_cannot_be_removed_is_still_reported(no_backoff, caplog):
    container = FakeRemovableContainer("abc123", fail_removes=99)

    with caplog.at_level(logging.WARNING, logger=CONTAINER_LOGGER):
        await engine_with(FakeContainerClient(container)).stop("abc123")

    assert not container.removed
    assert any("abc123" in record.message for record in caplog.records)


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


def test_candidate_sockets_cover_engines_the_sdk_would_miss(monkeypatch):
    """The SDK assumes /var/run/docker.sock, so Colima and Rancher Desktop
    need their own paths tried even when no context names them."""
    from bigfix_remote_client_relevance.transports.container import candidate_docker_sockets

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    # Pinned to a POSIX platform so the assertions hold wherever the suite runs.
    candidates = candidate_docker_sockets(endpoint_lookup=lambda: None, platform="linux")

    assert any("/var/run/docker.sock" in c for c in candidates)
    assert any(".docker/run/docker.sock" in c for c in candidates)
    assert any(".colima" in c for c in candidates)


def test_windows_candidates_are_the_named_pipe(monkeypatch):
    """Unix-socket paths are meaningless on Windows; the engine listens on a pipe."""
    from bigfix_remote_client_relevance.transports.container import candidate_docker_sockets

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    candidates = candidate_docker_sockets(endpoint_lookup=lambda: None, platform="win32")

    assert candidates == ["npipe:////./pipe/docker_engine"]


def test_docker_host_env_takes_precedence(monkeypatch):
    from bigfix_remote_client_relevance.transports.container import candidate_docker_sockets

    monkeypatch.setenv("DOCKER_HOST", "unix:///custom/docker.sock")

    assert candidate_docker_sockets(endpoint_lookup=lambda: None)[0] == "unix:///custom/docker.sock"


# --- starting an engine that is installed but stopped -------------------------
#
# "is Docker running?" is a question the tool can answer for itself. Nothing
# here launches anything: detection, starting and sleeping are all injected.


@dataclass
class FakeSetup:
    """Stands in for the machine: what is installed, and what we did to it."""

    starter: object = None
    started: list[str] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)

    def detect(self):
        return self.starter

    def start(self, starter) -> None:
        self.started.append(starter.name)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def hint(self) -> str:
        return "brew install --cask docker"


def starter(name="Docker Desktop", argv=("open", "-a", "Docker"), note=""):
    from bigfix_remote_client_relevance.transports.container_setup import EngineStarter

    return EngineStarter(name=name, argv=list(argv) if argv else None, note=note)


def engine_that_answers_after(attempts: int, setup):
    """A DockerEngine whose socket starts working only after N connect attempts."""
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    state = {"tries": 0}

    class LateEngine(DockerEngine):
        def _connect(self, urls, tried):
            state["tries"] += 1
            tried.extend(urls)
            return FakeDockerClient() if state["tries"] > attempts else None

    engine = LateEngine(socket_candidates=["unix:///nope.sock"], auto_setup=True, setup=setup)
    engine.tries = state
    return engine


async def test_a_stopped_engine_is_started_and_awaited():
    setup = FakeSetup(starter=starter())
    engine = engine_that_answers_after(1, setup)

    engine._get_client()

    assert setup.started == ["Docker Desktop"]


async def test_auto_setup_off_starts_nothing():
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    setup = FakeSetup(starter=starter())
    engine = DockerEngine(
        socket_candidates=["unix:///definitely/not/here.sock"],
        auto_setup=False,
        setup=setup,  # type: ignore[arg-type]
    )

    with pytest.raises(ContainerEngineError) as excinfo:
        engine._get_client()

    assert setup.started == []
    assert "not/here.sock" in str(excinfo.value), "it must still name what it tried"


async def test_the_wait_for_the_engine_is_bounded():
    setup = FakeSetup(starter=starter())
    engine = engine_that_answers_after(10_000, setup)  # never comes up

    with pytest.raises(ContainerEngineError) as excinfo:
        engine._get_client()

    assert sum(setup.slept) <= 120, "a stopped engine must not hang the run"
    assert "Docker Desktop" in str(excinfo.value)


async def test_nothing_installed_names_the_install_command():
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    setup = FakeSetup(starter=None)
    engine = DockerEngine(
        socket_candidates=["unix:///nope.sock"],
        auto_setup=True,
        setup=setup,  # type: ignore[arg-type]
    )

    with pytest.raises(ContainerEngineError) as excinfo:
        engine._get_client()

    assert "brew install" in str(excinfo.value)
    assert "nope.sock" in str(excinfo.value)


async def test_an_engine_we_must_not_start_reports_its_command():
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    setup = FakeSetup(
        starter=starter(name="Docker", argv=None, note="start it with: sudo systemctl start docker")
    )
    engine = DockerEngine(
        socket_candidates=["unix:///nope.sock"],
        auto_setup=True,
        setup=setup,  # type: ignore[arg-type]
    )

    with pytest.raises(ContainerEngineError) as excinfo:
        engine._get_client()

    assert setup.started == [], "a system daemon is not ours to start"
    assert "systemctl start docker" in str(excinfo.value)


async def test_an_injected_client_never_triggers_setup():
    """Tests and callers that supply a client must never launch anything."""
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    setup = FakeSetup(starter=starter())
    engine = DockerEngine(
        client=FakeDockerClient(),
        auto_setup=True,
        setup=setup,  # type: ignore[arg-type]
    )

    engine._get_client()

    assert setup.started == []


# --- the docker context, which the SDK does not read --------------------------


def test_the_context_endpoint_is_tried_before_the_hardcoded_paths(monkeypatch):
    from bigfix_remote_client_relevance.transports.container import candidate_docker_sockets

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    candidates = candidate_docker_sockets(
        endpoint_lookup=lambda: "tcp://10.0.0.5:2375", platform="linux"
    )

    assert candidates[0] == "tcp://10.0.0.5:2375"
    assert "unix:///var/run/docker.sock" in candidates, "the fallbacks must survive"


def test_docker_host_still_outranks_the_context(monkeypatch):
    """An explicit environment variable is a stronger statement than a context."""
    from bigfix_remote_client_relevance.transports.container import candidate_docker_sockets

    monkeypatch.setenv("DOCKER_HOST", "unix:///custom/docker.sock")
    candidates = candidate_docker_sockets(endpoint_lookup=lambda: "tcp://10.0.0.5:2375")

    assert candidates[0] == "unix:///custom/docker.sock"
    assert "tcp://10.0.0.5:2375" in candidates


def test_a_context_duplicating_a_hardcoded_path_is_not_listed_twice(monkeypatch):
    from bigfix_remote_client_relevance.transports.container import candidate_docker_sockets

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    candidates = candidate_docker_sockets(endpoint_lookup=lambda: "unix:///var/run/docker.sock")

    assert candidates.count("unix:///var/run/docker.sock") == 1


async def test_engine_error_names_the_sockets_it_tried():
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    # auto_setup=False so this asserts the error, not whatever engine happens
    # to be installed on the machine running the tests.
    engine = DockerEngine(socket_candidates=["unix:///definitely/not/here.sock"], auto_setup=False)

    with pytest.raises(ContainerEngineError) as excinfo:
        await engine.ensure_image("ubuntu:22.04")

    assert "not/here.sock" in str(excinfo.value)


async def test_writes_nothing_to_stdout(capsys):
    engine = FakeEngine(responses=[EVAL_OK])

    await TransportContainer("ubuntu:22.04", engine=engine).evaluate_client_relevance("true")

    assert capsys.readouterr().out == ""


# --- PodmanEngine: a docker-compatible socket, reached podman's own way ------


def test_podman_candidate_sockets_cover_rootless_and_machine_paths(monkeypatch):
    from bigfix_remote_client_relevance.transports.container import candidate_podman_sockets

    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    candidates = candidate_podman_sockets(endpoint_lookup=lambda: None, platform="linux")

    assert any("podman/podman.sock" in c and "run/user" not in c for c in candidates), (
        "rootful socket must be listed"
    )
    assert any("run/user" in c and "podman/podman.sock" in c for c in candidates), (
        "rootless XDG socket must be listed"
    )
    assert any("podman-machine-default" in c for c in candidates), "machine socket must be listed"


def test_podman_context_endpoint_is_tried_before_the_hardcoded_paths(monkeypatch):
    from bigfix_remote_client_relevance.transports.container import candidate_podman_sockets

    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    candidates = candidate_podman_sockets(
        endpoint_lookup=lambda: "unix:///run/user/1000/podman/podman.sock", platform="linux"
    )

    assert candidates[0] == "unix:///run/user/1000/podman/podman.sock"


def test_container_host_env_takes_precedence_for_podman(monkeypatch):
    from bigfix_remote_client_relevance.transports.container import candidate_podman_sockets

    monkeypatch.setenv("CONTAINER_HOST", "unix:///custom/podman.sock")

    assert candidate_podman_sockets(endpoint_lookup=lambda: None)[0] == "unix:///custom/podman.sock"


async def test_podman_engine_behaves_like_docker_engine_for_ensure_image():
    """Inheritance from DockerEngine, not reimplementation, is the whole point."""
    from bigfix_remote_client_relevance.transports.container import PodmanEngine

    client = FakeDockerClient()
    engine = PodmanEngine(client=client)

    await engine.ensure_image("ubuntu:24.04", platform="linux/amd64")

    assert client.pulled == [{"image": "ubuntu:24.04", "platform": "linux/amd64"}]


def test_podman_engine_uses_podman_sockets_not_docker_sockets(monkeypatch):
    from bigfix_remote_client_relevance.transports.container import PodmanEngine

    monkeypatch.delenv("CONTAINER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    engine = PodmanEngine()

    assert all(".docker" not in c and "docker.sock" not in c for c in engine._candidates())


def test_podman_engine_error_message_names_podman_not_docker():
    from bigfix_remote_client_relevance.transports.container import PodmanEngine

    engine = PodmanEngine(socket_candidates=["unix:///definitely/not/here.sock"], auto_setup=False)

    with pytest.raises(ContainerEngineError) as excinfo:
        engine._get_client()

    assert "podman" in str(excinfo.value)
    assert "Docker daemon" not in str(excinfo.value)


def test_podman_engine_setup_defaults_to_podman_engine_setup():
    from bigfix_remote_client_relevance.transports.container import PodmanEngine
    from bigfix_remote_client_relevance.transports.container_setup import PodmanEngineSetup

    engine = PodmanEngine()

    assert isinstance(engine._setup, PodmanEngineSetup)
