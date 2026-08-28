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


@pytest.mark.parametrize("target", ["ubuntu", "debian"])
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
