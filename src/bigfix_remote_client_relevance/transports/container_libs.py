"""Reading, and repairing, a qna binary that is present but cannot link.

Minimal images ship no more than they must, so an extracted qna often lands on
a host missing a shared library it needs — `libdbus-1.so.3` on rockylinux:9 and
amazonlinux:2023, for instance. The binary is there; the dynamic linker refuses
to start it.

That failure is easy to misread. The linker's message ends in "No such file or
directory", which a naive "is qna missing?" check matches, turning "install one
package" into the wrong advice entirely. Everything here exists to tell the two
cases apart and, where possible, to fix the second one.
"""

from __future__ import annotations

import re
import shlex

# Anchored on both halves of the linker's sentence so it cannot fire on a
# filename that merely happens to contain ".so".
_SHARED_LIB_RE = re.compile(
    r"error while loading shared libraries:\s*(?P<soname>[^\s:]+):\s*cannot open shared object file"
)


def missing_shared_library(stderr: str) -> str | None:
    """The soname a binary could not load, or ``None``.

    Deliberately does not match ``symbol lookup error`` or ``version
    'GLIBC_2.34' not found``: those are genuine incompatibilities between the
    binary and the image, and installing a package will not fix them.
    """
    match = _SHARED_LIB_RE.search(stderr)
    return match.group("soname") if match else None


PACKAGE_MANAGER_PROBE_COMMAND = (
    "command -v dnf >/dev/null 2>&1 && echo dnf; "
    "command -v microdnf >/dev/null 2>&1 && echo microdnf; "
    "command -v yum >/dev/null 2>&1 && echo yum; "
    "command -v apt-get >/dev/null 2>&1 && echo apt-get; "
    "command -v zypper >/dev/null 2>&1 && echo zypper; "
    "command -v apk >/dev/null 2>&1 && echo apk; true"
)

# Most specific first: a dnf image usually also has yum, and answering "yum"
# there would work but skip dnf's smaller transactions.
_MANAGER_PRIORITY = ("dnf", "microdnf", "yum", "apt-get", "zypper", "apk")

# Only the libdbus-1.so.3 rows are confirmed against real images (rockylinux:9
# and amazonlinux:2023, which is what motivated this). The rest are best-effort:
# a wrong or missing row degrades to a loud error naming the soname, never to a
# wrong answer, so adding one is cheap and low-risk.
PACKAGE_FOR_SONAME: dict[str, dict[str, str]] = {
    "rpm": {
        "libdbus-1.so.3": "dbus-libs",  # confirmed on rockylinux:9/amazonlinux:2023 (dnf/yum)
        "libstdc++.so.6": "libstdc++",
        "libgcc_s.so.1": "libgcc",
        "libz.so.1": "zlib",
        "libcrypt.so.1": "libxcrypt-compat",
        "libnsl.so.1": "libnsl",
        "libuuid.so.1": "libuuid",
        "libselinux.so.1": "libselinux",
    },
    # SUSE is rpm-format but not dnf/yum-family naming: its own package for
    # this soname is "libdbus-1-3", not "dbus-libs" -- confirmed live against
    # opensuse/leap:15, where "dbus-libs" doesn't exist at all ("No provider
    # of 'dbus-libs' found"). Checked ahead of the generic "rpm" table for any
    # zypper manager, so SUSE never silently gets the RHEL/Fedora answer.
    "suse": {
        "libdbus-1.so.3": "libdbus-1-3",  # confirmed
    },
    "deb": {
        "libdbus-1.so.3": "libdbus-1-3",  # confirmed
        "libstdc++.so.6": "libstdc++6",
        "libgcc_s.so.1": "libgcc-s1",
        "libz.so.1": "zlib1g",
        "libcrypt.so.1": "libcrypt1",
        "libuuid.so.1": "libuuid1",
        "libselinux.so.1": "libselinux1",
    },
}

_RPM_MANAGERS = frozenset({"dnf", "microdnf", "yum"})
# zypper packages are real rpms too and understand the same virtual Provides
# capability string ("libfoo.so.N()(64bit)") dnf/yum resolve directly --
# confirmed live: `zypper install 'libdbus-1.so.3()(64bit)'` correctly
# resolved and installed libdbus-1-3. It gets its own set rather than joining
# _RPM_MANAGERS because the "suse" table lookup above must run first, ahead
# of the RHEL/Fedora-only "rpm" table.
_ZYPPER_MANAGERS = frozenset({"zypper"})


def package_manager_from(probe_output: str) -> str | None:
    """Which package manager the image carries, most capable first."""
    found = set(probe_output.split())
    for manager in _MANAGER_PRIORITY:
        if manager in found:
            return manager
    return None


def package_for_soname(soname: str, *, family: str | None, manager: str) -> str | None:
    """The package to install for ``soname``, or ``None`` if unknown.

    rpm packages carry virtual ``Provides:`` entries for their sonames, so the
    rpm managers can resolve one directly and need no table entry. dpkg has no
    offline equivalent — ``apt-file`` would need its own install and index
    download — so an unmapped soname there is reported rather than guessed at.

    ``manager`` alone decides the SUSE case, ahead of ``family``: SUSE is
    still the coarse "rpm" family for extraction-tool purposes (it really is
    rpm-format), but zypper is unique to SUSE among supported managers, and
    its own package names differ from the RHEL/Fedora ones the "rpm" table
    was confirmed against. Checking it first means a SUSE target can never
    silently get a RHEL answer just because both share one family bucket.
    """
    if manager in _ZYPPER_MANAGERS:
        mapped = PACKAGE_FOR_SONAME.get("suse", {}).get(soname)
        if mapped is not None:
            return mapped
    else:
        mapped = PACKAGE_FOR_SONAME.get(family or "", {}).get(soname)
        if mapped is not None:
            return mapped
    if manager in _RPM_MANAGERS or manager in _ZYPPER_MANAGERS:
        return f"{soname}()(64bit)"
    return None


def install_command(manager: str, package: str) -> str:
    """Install one package, keeping the committed layer as small as possible.

    Whatever the install downloads ends up in the committed image, so each
    variant drops its caches in the same command rather than a later one.
    """
    quoted = shlex.quote(package)
    if manager == "dnf":
        return f"dnf install -y --setopt=install_weak_deps=False --nodocs {quoted} && dnf clean all"
    if manager == "microdnf":
        return f"microdnf install -y --nodocs {quoted} && microdnf clean all"
    if manager == "yum":
        return f"yum install -y {quoted} && yum clean all"
    if manager == "apt-get":
        return (
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {quoted} "
            "&& rm -rf /var/lib/apt/lists/*"
        )
    if manager == "zypper":
        return f"zypper --non-interactive install {quoted} && zypper clean -a"
    if manager == "apk":
        return f"apk add --no-cache {quoted}"
    raise ValueError(f"unknown package manager {manager!r}")


def needs_index_refresh(manager: str) -> bool:
    """Debian images ship no package lists, so apt-get must fetch them first."""
    return manager == "apt-get"


INDEX_REFRESH_COMMAND = "apt-get update"


# qemu-user's own message when the target's ELF interpreter itself (not one
# of its shared library dependencies) is missing -- meaning the image has no
# userspace for that foreign architecture at all, not just one package away.
# Distinct from missing_shared_library's "error while loading shared
# libraries: ... cannot open shared object file", which names a dependency of
# a program that otherwise runs; this fires before the program ever starts.
# Confirmed live: running the raspbian armhf (32-bit ARM) agent under an
# arm64 debian:12 container reports
# "qemu-arm: Could not open '/lib/ld-linux-armhf.so.3': No such file or directory".
_ARM_INTERPRETER_RE = re.compile(
    r"qemu-\S+: Could not open '(?P<path>/lib/ld-linux-\S+\.so\.\d+)': "
    r"No such file or directory"
)


def missing_arm_interpreter(stderr: str) -> str | None:
    """The missing foreign-arch ELF interpreter path, or ``None``.

    Fixing this needs a whole architecture enabled (``dpkg
    --add-architecture`` plus an install), not just one package -- see
    :func:`foreign_arch_package_for_interpreter` and
    :func:`enable_foreign_arch_command`.
    """
    match = _ARM_INTERPRETER_RE.search(stderr)
    return match.group("path") if match else None


# Interpreter basename -> (dpkg foreign architecture, package providing it).
# armhf is the only case this tool exercises today -- the raspbian-on-arm64
# fallback for Debian/Ubuntu (see bootstrap/release_site.py's
# _ARM64_RASPBIAN_FALLBACK_PLATFORMS); add rows as new cross-arch cases arise.
_ARM_INTERPRETER_PACKAGES: dict[str, tuple[str, str]] = {
    "ld-linux-armhf.so.3": ("armhf", "libc6:armhf"),
}


def foreign_arch_package_for_interpreter(interpreter_path: str) -> tuple[str, str] | None:
    """``(dpkg foreign arch, package)`` that provides ``interpreter_path``.

    ``None`` if unknown -- reported rather than guessed at, the same policy
    :func:`package_for_soname` follows for an unmapped deb soname.
    """
    basename = interpreter_path.rsplit("/", 1)[-1]
    return _ARM_INTERPRETER_PACKAGES.get(basename)


def qualify_for_foreign_arch(package: str, dpkg_arch: str) -> str:
    """Name ``package`` for a foreign dpkg architecture (``libstdc++6:armhf``).

    Once the binary turns out to be a foreign architecture, so is every
    library it links against: the native ``libstdc++6`` does nothing for an
    armhf binary, and installing it leaves the loader reporting the very same
    missing soname on the next probe.
    """
    return f"{package}:{dpkg_arch}"


def enable_foreign_arch_command(dpkg_arch: str) -> str:
    """Register ``dpkg_arch`` (e.g. ``"armhf"``) and refresh the package index.

    dpkg refuses a foreign-arch package (``libc6:armhf``) until its
    architecture is registered, and any previously-fetched index predates
    that registration -- so this always re-refreshes, regardless of whether
    an index refresh already ran for the native architecture via
    :func:`needs_index_refresh`/:data:`INDEX_REFRESH_COMMAND`.
    """
    return f"dpkg --add-architecture {shlex.quote(dpkg_arch)} && apt-get update"


__all__ = [
    "INDEX_REFRESH_COMMAND",
    "PACKAGE_FOR_SONAME",
    "PACKAGE_MANAGER_PROBE_COMMAND",
    "enable_foreign_arch_command",
    "foreign_arch_package_for_interpreter",
    "install_command",
    "missing_arm_interpreter",
    "missing_shared_library",
    "needs_index_refresh",
    "package_for_soname",
    "package_manager_from",
    "qualify_for_foreign_arch",
]
