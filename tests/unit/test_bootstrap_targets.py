"""Tests for per-platform bootstrap specs: paths, prereqs, extract commands.

These are pure string builders, so they are tested directly rather than through
a transport.
"""

from __future__ import annotations

import pytest

from bigfix_remote_client_relevance.bootstrap.targets import (
    KNOWN_TARGETS,
    UnknownTargetError,
    classify_uname,
    spec_for,
)


def test_every_known_target_is_self_consistent():
    for name, spec in KNOWN_TARGETS.items():
        assert spec.name == name
        assert spec.family in {"posix", "windows"}
        assert spec.cache_root
        assert spec.qna_relative_path
        assert spec.release_platform
        assert spec.prereqs, f"{name} declares no extraction prereqs"


def test_unknown_target_raises():
    with pytest.raises(UnknownTargetError):
        spec_for("plan9")


# --- macOS -----------------------------------------------------------------


def test_macos_extracts_qna_from_the_agent_pkg():
    spec = spec_for("macos")
    commands = spec.extract_commands("/tmp/a.pkg", "/tmp/dest")
    joined = " ; ".join(commands)

    assert "/tmp/a.pkg" in joined
    assert "Payload" in joined, "qna lives inside besagent.pkg/Payload"
    assert "cpio" in joined
    assert spec.qna_relative_path == "BESAgent.app/Contents/MacOS/QnA"
    assert spec.cache_root == "/tmp/bigfix_qna"


def test_macos_prereqs_are_stock_tools():
    tools = {p.tool for p in spec_for("macos").prereqs}

    assert {"xar", "cpio"} <= tools


# --- Debian family ---------------------------------------------------------


@pytest.mark.parametrize("target", ["ubuntu", "debian", "raspbian"])
def test_deb_family_extracts_with_dpkg_deb(target):
    spec = spec_for(target)
    joined = " ; ".join(spec.extract_commands("/tmp/a.deb", "/tmp/dest"))

    assert "dpkg-deb" in joined
    assert spec.qna_relative_path == "opt/BESClient/bin/qna"


def test_deb_family_falls_back_to_ar_and_tar():
    """Minimal images often lack dpkg-deb but have binutils."""
    joined = " ; ".join(spec_for("ubuntu").extract_commands("/tmp/a.deb", "/tmp/dest"))

    assert "ar x" in joined
    assert "tar" in joined


def test_deb_prereq_hint_names_a_package():
    hints = {p.tool: p.install_hint for p in spec_for("ubuntu").prereqs}

    assert "dpkg-deb" in hints
    assert "apt" in " ".join(hints.values())


# --- RPM family ------------------------------------------------------------


@pytest.mark.parametrize("target", ["rhel", "suse"])
def test_rpm_family_extracts_with_rpm2cpio(target):
    spec = spec_for(target)
    joined = " ; ".join(spec.extract_commands("/tmp/a.rpm", "/tmp/dest"))

    assert "rpm2cpio" in joined
    assert "cpio" in joined
    assert spec.qna_relative_path == "opt/BESClient/bin/qna"


def test_rpm_prereqs_flag_the_commonly_missing_tools():
    """rpm2cpio and cpio are frequently absent on minimal/container images."""
    hints = {p.tool: p.install_hint for p in spec_for("rhel").prereqs}

    assert "cpio" in hints
    assert "install" in hints["cpio"]


# --- Windows ---------------------------------------------------------------


def test_windows_extracts_the_standalone_qna_zip():
    spec = spec_for("windows")
    joined = " ; ".join(spec.extract_commands(r"C:\tmp\QNA.zip", r"C:\tmp\dest"))

    assert "Expand-Archive" in joined
    assert spec.family == "windows"
    assert spec.qna_relative_path == "QnA.exe"


def test_windows_cache_root_is_under_windows_temp():
    assert spec_for("windows").cache_root == r"\Windows\Temp\bigfix_qna"


def test_windows_non_admin_root_is_under_user_temp():
    """\\Windows\\Temp needs elevation; non-admin SSH users fall back to %TEMP%."""
    spec = spec_for("windows")

    assert "TEMP" in spec.fallback_cache_root
    assert spec.fallback_cache_root != spec.cache_root


# --- platform classification ----------------------------------------------


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ("Darwin", "macos"),
        ("Linux\nubuntu debian", "ubuntu"),
        ("Linux\ndebian", "debian"),
        # Raspberry Pi OS reports ID=raspbian, ID_LIKE=debian -- must not
        # fold into plain "debian", since raspbian has its own bootstrap
        # spec (and its own agent build, matched regardless of arch).
        ("Linux\nraspbian debian", "raspbian"),
        ("Linux\nrhel fedora", "rhel"),
        ("Linux\ncentos rhel fedora", "rhel"),
        ("Linux\nopensuse-leap suse", "suse"),
        ("Linux\namzn", "rhel"),
        ("", "windows"),
    ],
)
def test_classify_uname(probe, expected):
    assert classify_uname(probe) == expected


def test_classify_unrecognized_linux_defaults_to_deb_family():
    """Better to guess the more common family than to refuse outright."""
    assert classify_uname("Linux\nsomething-exotic") in {"ubuntu", "debian"}


# --- strict classification (containers control their probe) -----------------
#
# SSH probes an unknown box, where guessing beats refusing. A container probe
# is fully controlled, so a guess would fabricate data (issue #1): strict mode
# refuses instead.


def test_strict_mode_refuses_unrecognized_linux():
    with pytest.raises(UnknownTargetError):
        classify_uname("Linux\nsomething-exotic", strict=True)


def test_strict_mode_refuses_empty_probe_output():
    """Empty output means Windows over SSH, but 'probe failed' in a container."""
    with pytest.raises(UnknownTargetError):
        classify_uname("", strict=True)


def test_strict_mode_refuses_a_non_linux_non_darwin_kernel():
    with pytest.raises(UnknownTargetError):
        classify_uname("FreeBSD 14", strict=True)


def test_strict_mode_error_names_the_probe_and_the_fix():
    with pytest.raises(UnknownTargetError) as excinfo:
        classify_uname("Linux\nsomething-exotic", strict=True)

    message = str(excinfo.value)
    assert "something-exotic" in message, "error must show what the probe saw"
    assert "platform" in message.lower(), "error must point at the explicit-platform escape hatch"


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ("Linux\nubuntu debian", "ubuntu"),
        ("Linux\ndebian", "debian"),
        ("Linux\nalmalinux rhel fedora centos", "rhel"),
        ("Linux\nsles suse", "suse"),
        ("Darwin", "macos"),
    ],
)
def test_strict_mode_still_classifies_known_families(probe, expected):
    assert classify_uname(probe, strict=True) == expected


def test_default_mode_still_guesses_deb_family_for_ssh():
    """SSH keeps the guess: an unknown Unix is more likely deb than anything else."""
    assert classify_uname("Linux\nsomething-exotic") == "ubuntu"


# --- architecture names -------------------------------------------------------
#
# The same architecture goes by several names depending on who is asking:
# uname says aarch64, macOS says arm64, Docker says linux/arm64, and the
# release site's rpm builds say aarch64 while its deb builds say arm64.


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("arm64", "arm64"),
        ("aarch64", "arm64"),
        ("x86_64", "x86_64"),
        ("amd64", "x86_64"),
        ("AMD64", "x86_64"),  # what Windows reports
    ],
)
def test_normalize_arch_collapses_the_spellings(machine, expected):
    from bigfix_remote_client_relevance.bootstrap.targets import normalize_arch

    assert normalize_arch(machine) == expected


def test_an_unrecognized_machine_passes_through():
    """Better a lookup that fails by name than one silently retargeted."""
    from bigfix_remote_client_relevance.bootstrap.targets import normalize_arch

    assert normalize_arch("riscv64") == "riscv64"


def test_host_arch_reports_this_machine(monkeypatch):
    import platform as platform_module

    from bigfix_remote_client_relevance.bootstrap.targets import host_arch

    monkeypatch.setattr(platform_module, "machine", lambda: "aarch64")

    assert host_arch() == "arm64"


# --- target_platform: a target's KNOWN_TARGETS key without connecting ---------
#
# Ported from bigfix-relevance-debugger's upstream/remote_client_relevance.py
# (issue #54), which a front end uses to show each target's platform -- and
# what can run there -- before evaluating anything.


def _container(image, **kwargs):
    from bigfix_remote_client_relevance.orchestrate import Target

    return Target(kind="container", name=image, image=image, **kwargs)


def _ssh(name, **kwargs):
    from bigfix_remote_client_relevance.orchestrate import Target

    return Target(kind="ssh", name=name, **kwargs)


@pytest.mark.parametrize(
    ("target", "platform"),
    [
        (_ssh("win", platform="windows"), "windows"),
        (_ssh("pi", platform="raspbian"), "raspbian"),
        (_ssh("sles", platform="suse"), "suse"),
        (_ssh("unknown"), None),
        (_container("debian:11"), "debian"),
        (_container("ubuntu:24.04"), "ubuntu"),
        (_container("almalinux:9"), "rhel"),
        (_container("rockylinux:9"), "rhel"),
        (_container("amazonlinux:2023"), "rhel"),
        (_container("oraclelinux:9"), "rhel"),
        (_container("fedora:41"), "rhel"),
        (_container("centos:7"), "rhel"),
        (_container("registry.access.redhat.com/ubi9/ubi"), "rhel"),
        (_container("registry.access.redhat.com/ubi10/ubi:latest"), "rhel"),
        (_container("opensuse/leap:15"), "suse"),
        (_container("mystery:1"), None),
        # An explicit platform beats the image name.
        (_container("debian:11", platform="ubuntu"), "ubuntu"),
    ],
)
def test_target_platform(target, platform):
    from bigfix_remote_client_relevance.bootstrap.targets import target_platform

    assert target_platform(target) == platform


def test_target_platform_reads_the_name_when_no_image_is_set():
    from bigfix_remote_client_relevance.bootstrap.targets import target_platform
    from bigfix_remote_client_relevance.orchestrate import Target

    assert target_platform(Target(kind="container", name="debian:12")) == "debian"


def test_target_platform_only_returns_known_bootstrap_keys():
    from bigfix_remote_client_relevance.bootstrap.targets import target_platform

    images = ["debian:11", "ubuntu:24.04", "almalinux:9", "opensuse/leap:15", "ubi9/ubi"]

    for image in images:
        assert target_platform(_container(image)) in KNOWN_TARGETS


def test_target_platform_agrees_with_discovery_on_shared_distros():
    """Two tables name the same distros: discovery's (distro -> deb/rpm family,
    for picking one host per family) and this one (image token -> bootstrap
    key). A distro discovery tries must land in the family it was tried for,
    or a "debian-family" candidate would be provisioned with an rpm."""
    from bigfix_remote_client_relevance import discovery
    from bigfix_remote_client_relevance.bootstrap.targets import target_platform

    family_of_key = {"ubuntu": "debian", "debian": "debian", "raspbian": "debian", "rhel": "rhel"}
    for distro, family in discovery._FAMILY_OF_DISTRO.items():
        platform = target_platform(_container(f"{distro}:1"))
        assert platform is not None, distro
        assert family_of_key[platform] == family, distro


@pytest.mark.parametrize(
    ("sys_platform", "platform"),
    [("darwin", "macos"), ("win32", "windows"), ("linux", None), ("ios", None)],
)
def test_local_target_platform_is_this_machine(monkeypatch, sys_platform, platform):
    # Linux needs the os-release probe the transport does anyway; iOS has no
    # bootstrap key at all (no local qna there).
    from bigfix_remote_client_relevance.bootstrap.targets import target_platform
    from bigfix_remote_client_relevance.orchestrate import Target

    monkeypatch.setattr("sys.platform", sys_platform)

    assert target_platform(Target(kind="local", name="local")) == platform


@pytest.mark.parametrize("kind", ["fastquery", "online_evaluator"])
def test_target_platform_is_unknown_for_remote_services(kind):
    from bigfix_remote_client_relevance.bootstrap.targets import target_platform
    from bigfix_remote_client_relevance.orchestrate import Target

    assert target_platform(Target(kind=kind, name="svc")) is None
