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
            if self.install_exit_code == 0 and "update" not in command and self.missing_libs:
                self.missing_libs.pop(0)
            return (
                "",
                "" if self.install_exit_code == 0 else "No match for argument",
                self.install_exit_code,
            )
        # Any attempt to run qna fails while a library is missing — the build's
        # link probe and the evaluation alike. Must precede the regex table,
        # since both carry -showtypes and would otherwise match EVAL_OK.
        if "-showtypes" in command and self.missing_libs:
            return ("", link_failure(self.missing_libs[0]), 127)
        if "< /dev/null" in command:
            return ("", "", 0)
        for pattern, response in self.responses:
            if re.search(pattern, command):
                return response
        return self.default

    async def ensure_image(self, image: str, *, platform: str | None = None) -> None:
        self.pulled.append(image)

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
        return [c.command for c in self.execs] + [
            str(s["command"]) for s in self.one_shots
        ]


QNA_OUT = "A: Ubuntu 22.04.3 LTS\nI: singular string\nT: 0.2 ms\n"
EVAL_OK = (r"-showtypes", (QNA_OUT, "", 0))
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

    engine = NeverFixedEngine(
        responses=[PROBE_ALMA, EVAL_OK], missing_libs=["libdbus-1.so.3"]
    )

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
        def __init__(self, **kwargs):
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


# --- image architecture ------------------------------------------------------


@dataclass
class FakeImage:
    attrs: dict[str, object]


class FakeDockerClient:
    """Just enough of the docker SDK to drive ensure_image."""

    def __init__(self, existing: FakeImage | None = None) -> None:
        self.images = self
        self.existing = existing
        self.pulled: list[dict[str, object]] = []

    def get(self, image: str) -> FakeImage:
        import docker.errors

        if self.existing is None:
            raise docker.errors.ImageNotFound(image)
        return self.existing

    def pull(self, image: str, platform: str | None = None) -> None:
        self.pulled.append({"image": image, "platform": platform})

    def ping(self) -> bool:
        return True


def engine_with(client: FakeDockerClient):
    from bigfix_remote_client_relevance.transports.container import DockerEngine

    return DockerEngine(client=client)


async def test_ensure_image_repulls_on_architecture_mismatch():
    """A cached arm64 image satisfies images.get, then creation 404s on amd64."""
    client = FakeDockerClient(FakeImage(attrs={"Architecture": "arm64", "Os": "linux"}))

    await engine_with(client).ensure_image("ubuntu:24.04", platform="linux/amd64")

    assert client.pulled == [{"image": "ubuntu:24.04", "platform": "linux/amd64"}]


async def test_ensure_image_does_not_pull_when_the_architecture_matches():
    client = FakeDockerClient(FakeImage(attrs={"Architecture": "amd64", "Os": "linux"}))

    await engine_with(client).ensure_image("ubuntu:24.04", platform="linux/amd64")

    assert client.pulled == []


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
