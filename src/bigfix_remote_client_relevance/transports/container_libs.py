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
        "libdbus-1.so.3": "dbus-libs",  # confirmed
        "libstdc++.so.6": "libstdc++",
        "libgcc_s.so.1": "libgcc",
        "libz.so.1": "zlib",
        "libcrypt.so.1": "libxcrypt-compat",
        "libnsl.so.1": "libnsl",
        "libuuid.so.1": "libuuid",
        "libselinux.so.1": "libselinux",
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
    """
    mapped = PACKAGE_FOR_SONAME.get(family or "", {}).get(soname)
    if mapped is not None:
        return mapped
    if manager in _RPM_MANAGERS:
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


__all__ = [
    "INDEX_REFRESH_COMMAND",
    "PACKAGE_FOR_SONAME",
    "PACKAGE_MANAGER_PROBE_COMMAND",
    "install_command",
    "missing_shared_library",
    "needs_index_refresh",
    "package_for_soname",
    "package_manager_from",
]
