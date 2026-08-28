"""Per-platform bootstrap specs: where qna goes, what extracts it, what that needs.

Ported from the ``bigfix_run_qna_*`` scripts in ``jgstew/tools``, with the
pinned version turned into a parameter and the download step removed — the
controller fetches, the target only extracts.

Everything here is pure string building, so it is testable without a target.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# Written after a successful extraction. Its presence (not merely the binary's)
# is what marks a version as usable, so a half-extracted tree is never mistaken
# for a complete one.
MARKER_FILENAME = ".bfrcr-complete"


class UnknownTargetError(Exception):
    """No bootstrap spec exists for the requested platform."""


@dataclass(frozen=True)
class ExtractionPrereq:
    """A tool the target needs before an artifact is worth transferring."""

    tool: str
    install_hint: str


@dataclass(frozen=True)
class TargetSpec:
    """How to provision and run qna on one family of target."""

    name: str
    family: str
    """``"posix"`` or ``"windows"`` — decides shell quoting and path joining."""

    cache_root: str
    qna_relative_path: str
    """Where qna lands inside an extracted tree."""

    release_platform: str
    """Key passed to :func:`~...release_site.artifact_for`."""

    prereqs: tuple[ExtractionPrereq, ...]
    _extract: Callable[[str, str], tuple[str, ...]] = field(repr=False)
    fallback_cache_root: str = ""
    """Used when the primary root is not writable (Windows non-admin)."""

    def extract_commands(self, archive: str, dest: str) -> tuple[str, ...]:
        """Shell commands that unpack ``archive`` into ``dest`` on the target."""
        return self._extract(archive, dest)


def _macos_extract(archive: str, dest: str) -> tuple[str, ...]:
    # The agent .pkg is a xar archive whose besagent.pkg/Payload is a gzipped
    # cpio holding BESAgent.app. No installation required.
    return (
        f"mkdir -p {dest}",
        f"xar -xf {archive} -C {dest}",
        f"cd {dest} && cat besagent.pkg/Payload | gunzip -dc | cpio -i",
    )


def _deb_extract(archive: str, dest: str) -> tuple[str, ...]:
    # dpkg-deb is the clean path; ar + tar covers minimal images that ship
    # binutils but not dpkg. The payload is .tar.xz on recent builds and
    # .tar.gz on older ones, so try both.
    return (
        f"mkdir -p {dest}",
        (
            f"if command -v dpkg-deb >/dev/null 2>&1; then "
            f"dpkg-deb -x {archive} {dest}; "
            f"else cd {dest} && ar x {archive} && "
            f"{{ tar -xf data.tar.xz || tar -xf data.tar.gz; }} && "
            f"rm -f data.tar.* control.tar.* debian-binary; fi"
        ),
    )


def _rpm_extract(archive: str, dest: str) -> tuple[str, ...]:
    return (
        f"mkdir -p {dest}",
        f"cd {dest} && rpm2cpio {archive} | cpio -idm",
    )


def _windows_extract(archive: str, dest: str) -> tuple[str, ...]:
    # The default shell is switched to PowerShell during setup, so
    # Expand-Archive (built in since PS5) is available.
    return (
        f"New-Item -ItemType Directory -Force -Path '{dest}' | Out-Null",
        f"Expand-Archive -Path '{archive}' -DestinationPath '{dest}' -Force",
    )


_APT_HINT = "apt install binutils"
_DNF_HINT = "dnf install cpio"
_ZYPPER_HINT = "zypper install cpio"

_DEB_PREREQS = (
    ExtractionPrereq("dpkg-deb", "apt install dpkg (or use the ar fallback)"),
    ExtractionPrereq("ar", _APT_HINT),
    ExtractionPrereq("tar", "apt install tar"),
)

KNOWN_TARGETS: dict[str, TargetSpec] = {
    "macos": TargetSpec(
        name="macos",
        family="posix",
        cache_root="/tmp/bigfix_qna",
        qna_relative_path="BESAgent.app/Contents/MacOS/QnA",
        release_platform="macos",
        prereqs=(
            ExtractionPrereq("xar", "ships with macOS"),
            ExtractionPrereq("cpio", "ships with macOS"),
            ExtractionPrereq("gunzip", "ships with macOS"),
        ),
        _extract=_macos_extract,
    ),
    "ubuntu": TargetSpec(
        name="ubuntu",
        family="posix",
        cache_root="/tmp/bigfix_qna",
        qna_relative_path="opt/BESClient/bin/qna",
        release_platform="ubuntu",
        prereqs=_DEB_PREREQS,
        _extract=_deb_extract,
    ),
    "debian": TargetSpec(
        name="debian",
        family="posix",
        cache_root="/tmp/bigfix_qna",
        qna_relative_path="opt/BESClient/bin/qna",
        release_platform="debian",
        prereqs=_DEB_PREREQS,
        _extract=_deb_extract,
    ),
    "rhel": TargetSpec(
        name="rhel",
        family="posix",
        cache_root="/tmp/bigfix_qna",
        qna_relative_path="opt/BESClient/bin/qna",
        release_platform="rhel",
        prereqs=(
            # Frequently missing on minimal and container images.
            ExtractionPrereq("rpm2cpio", _DNF_HINT.replace("cpio", "rpm")),
            ExtractionPrereq("cpio", _DNF_HINT),
        ),
        _extract=_rpm_extract,
    ),
    "suse": TargetSpec(
        name="suse",
        family="posix",
        cache_root="/tmp/bigfix_qna",
        qna_relative_path="opt/BESClient/bin/qna",
        release_platform="suse",
        prereqs=(
            ExtractionPrereq("rpm2cpio", _ZYPPER_HINT.replace("cpio", "rpm")),
            ExtractionPrereq("cpio", _ZYPPER_HINT),
        ),
        _extract=_rpm_extract,
    ),
    "windows": TargetSpec(
        name="windows",
        family="windows",
        cache_root=r"\Windows\Temp\bigfix_qna",
        fallback_cache_root=r"$env:TEMP\bigfix_qna",
        qna_relative_path="QnA.exe",
        release_platform="windows",
        prereqs=(ExtractionPrereq("Expand-Archive", "built into PowerShell 5+"),),
        _extract=_windows_extract,
    ),
}


def spec_for(target: str) -> TargetSpec:
    try:
        return KNOWN_TARGETS[target]
    except KeyError:
        known = ", ".join(sorted(KNOWN_TARGETS))
        raise UnknownTargetError(f"no bootstrap spec for {target!r}; known: {known}") from None


# os-release ID / ID_LIKE tokens that select an extraction family.
_RPM_TOKENS = frozenset(
    {"rhel", "fedora", "centos", "almalinux", "rocky", "ol", "oracle", "amzn", "amazon"}
)
_SUSE_TOKENS = frozenset({"suse", "opensuse", "sles", "opensuse-leap", "opensuse-tumbleweed"})
_DEBIAN_TOKENS = frozenset({"debian", "raspbian"})


def classify_uname(probe_output: str, *, strict: bool = False) -> str:
    """Turn a target probe into a :data:`KNOWN_TARGETS` key.

    The probe emits ``uname -s`` on the first line and, on Linux, the
    ``ID``/``ID_LIKE`` fields from ``/etc/os-release`` on the second. Windows
    has no ``uname``, so empty output means Windows.

    With ``strict=True`` every guess raises :class:`UnknownTargetError`
    instead: SSH probes an unknown box where guessing beats refusing, but a
    container probe is fully controlled, and a wrong guess there silently
    hands an rpm-family image the Debian agent.
    """

    def _refuse(why: str) -> str:
        if strict:
            raise UnknownTargetError(
                f"cannot classify target from probe output {probe_output!r} ({why}); "
                "pass an explicit platform (e.g. --platform) from: "
                + ", ".join(sorted(KNOWN_TARGETS))
            )
        return "windows" if why != "unrecognized Linux distribution" else "ubuntu"

    lines = [line.strip() for line in probe_output.splitlines() if line.strip()]
    if not lines:
        return _refuse("empty probe output")

    kernel = lines[0].lower()
    if kernel.startswith("darwin"):
        return "macos"
    if not kernel.startswith("linux"):
        return _refuse(f"unsupported kernel {lines[0]!r}")

    tokens = {token.strip().strip('"') for line in lines[1:] for token in line.lower().split()}
    if tokens & _SUSE_TOKENS:
        return "suse"
    if tokens & _RPM_TOKENS:
        return "rhel"
    if tokens & _DEBIAN_TOKENS and "ubuntu" not in tokens:
        return "debian"
    if "ubuntu" in tokens:
        return "ubuntu"
    # Anything unrecognized: the deb family is by far the more common case,
    # so (non-strict) guessing it beats refusing to proceed.
    return _refuse("unrecognized Linux distribution")


__all__ = [
    "KNOWN_TARGETS",
    "MARKER_FILENAME",
    "ExtractionPrereq",
    "TargetSpec",
    "UnknownTargetError",
    "classify_uname",
    "spec_for",
]
