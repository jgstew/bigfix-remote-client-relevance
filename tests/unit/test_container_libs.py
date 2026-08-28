"""Tests for telling a missing qna apart from a qna that cannot link.

The distinction is not academic: the dynamic linker's message ends in "No such
file or directory", which the missing-binary heuristic matches, so a present
qna on rockylinux:9 was reported as absent. The ROCKY_STDERR constant below is
the real message captured from that image.
"""

from __future__ import annotations

import pytest

from bigfix_remote_client_relevance.transports.container_libs import missing_shared_library

ROCKY_STDERR = (
    "/opt/bigfix_qna/opt/BESClient/bin/qna: error while loading shared libraries: "
    "libdbus-1.so.3: cannot open shared object file: No such file or directory"
)


def test_parses_the_soname_from_a_linker_failure():
    assert missing_shared_library(ROCKY_STDERR) == "libdbus-1.so.3"


def test_a_missing_binary_is_not_a_linker_failure():
    assert missing_shared_library("qna: command not found") is None


def test_a_plain_no_such_file_is_not_a_linker_failure():
    """The overlap that caused the misreport in the first place."""
    assert missing_shared_library("sh: /opt/qna: No such file or directory") is None


def test_a_symbol_version_mismatch_is_not_a_missing_library():
    """No package install fixes this, so it must not look fixable."""
    stderr = "qna: /lib64/libc.so.6: version `GLIBC_2.34' not found (required by qna)"

    assert missing_shared_library(stderr) is None


def test_a_symbol_lookup_error_is_not_a_missing_library():
    stderr = "qna: symbol lookup error: qna: undefined symbol: dbus_message_new"

    assert missing_shared_library(stderr) is None


def test_a_filename_containing_so_is_not_matched():
    assert missing_shared_library("E: could not read /data/parse.so.notes") is None


def test_empty_stderr_is_not_a_linker_failure():
    assert missing_shared_library("") is None


def test_the_first_missing_library_is_reported():
    """Fixing one library often reveals the next; report them one at a time."""
    stderr = (
        "qna: error while loading shared libraries: libdbus-1.so.3: "
        "cannot open shared object file: No such file or directory"
    )

    assert missing_shared_library(stderr) == "libdbus-1.so.3"


# --- choosing what to install -------------------------------------------------

def test_the_package_manager_probe_prefers_dnf_over_yum():
    """A dnf image usually also has yum; dnf's transactions are smaller."""
    from bigfix_remote_client_relevance.transports.container_libs import package_manager_from

    assert package_manager_from("dnf\nyum\n") == "dnf"


def test_an_image_with_no_package_manager_reports_none():
    from bigfix_remote_client_relevance.transports.container_libs import package_manager_from

    assert package_manager_from("") is None


@pytest.mark.parametrize(
    ("family", "manager", "expected"),
    [("rpm", "dnf", "dbus-libs"), ("deb", "apt-get", "libdbus-1-3")],
)
def test_dbus_maps_to_the_right_package_per_family(family, manager, expected):
    """The one mapping confirmed against real images."""
    from bigfix_remote_client_relevance.transports.container_libs import package_for_soname

    assert package_for_soname("libdbus-1.so.3", family=family, manager=manager) == expected


def test_an_unmapped_rpm_soname_falls_back_to_the_provides_name():
    """rpm packages carry virtual Provides for their sonames, so dnf can resolve it."""
    from bigfix_remote_client_relevance.transports.container_libs import package_for_soname

    package = package_for_soname("libfoo.so.9", family="rpm", manager="dnf")

    assert package == "libfoo.so.9()(64bit)"


def test_an_unmapped_deb_soname_has_no_package():
    """dpkg has no offline soname lookup, so this must be reported, not guessed."""
    from bigfix_remote_client_relevance.transports.container_libs import package_for_soname

    assert package_for_soname("libfoo.so.9", family="deb", manager="apt-get") is None


def test_dbus_maps_to_the_suse_specific_package_under_zypper():
    """SUSE's own naming differs from the RHEL/Fedora-confirmed "rpm" rows --
    zypper's own package for this soname is libdbus-1-3, not dbus-libs.
    Confirmed live against opensuse/leap:15: the "rpm" family's dbus-libs
    does not exist there and fails loudly ("No provider of 'dbus-libs'
    found"), while libdbus-1-3 installs cleanly."""
    from bigfix_remote_client_relevance.transports.container_libs import package_for_soname

    # family is still the coarse "rpm" bucket -- manager="zypper" alone must
    # be enough to prefer the SUSE-specific row over it.
    assert package_for_soname("libdbus-1.so.3", family="rpm", manager="zypper") == "libdbus-1-3"


def test_an_unmapped_suse_soname_falls_back_to_the_provides_name():
    """zypper resolves rpm virtual Provides too, confirmed live: `zypper install
    'libdbus-1.so.3()(64bit)'` correctly resolved and installed libdbus-1-3."""
    from bigfix_remote_client_relevance.transports.container_libs import package_for_soname

    package = package_for_soname("libfoo.so.9", family="rpm", manager="zypper")

    assert package == "libfoo.so.9()(64bit)"


def test_the_provides_name_is_shell_quoted():
    """Bare parentheses would be shell metacharacters."""
    from bigfix_remote_client_relevance.transports.container_libs import (
        install_command,
        package_for_soname,
    )

    package = package_for_soname("libfoo.so.9", family="rpm", manager="dnf")
    command = install_command("dnf", package)

    assert "'libfoo.so.9()(64bit)'" in command


@pytest.mark.parametrize(
    ("manager", "fragment"),
    [
        ("dnf", "dnf install -y"),
        ("microdnf", "microdnf install -y"),
        ("yum", "yum install -y"),
        ("apt-get", "apt-get install -y"),
        ("zypper", "zypper --non-interactive install"),
        ("apk", "apk add --no-cache"),
    ],
)
def test_each_manager_gets_a_noninteractive_install(manager, fragment):
    from bigfix_remote_client_relevance.transports.container_libs import install_command

    assert fragment in install_command(manager, "dbus-libs")


def test_installs_clean_up_after_themselves():
    """Whatever the install downloads is committed into the image otherwise."""
    from bigfix_remote_client_relevance.transports.container_libs import install_command

    assert "clean all" in install_command("dnf", "dbus-libs")
    assert "rm -rf /var/lib/apt/lists" in install_command("apt-get", "libdbus-1-3")


def test_only_apt_needs_an_index_refresh():
    """Debian images ship no package lists; rpm images do."""
    from bigfix_remote_client_relevance.transports.container_libs import needs_index_refresh

    assert needs_index_refresh("apt-get") is True
    assert needs_index_refresh("dnf") is False


def test_an_unknown_manager_is_an_error_not_a_guess():
    from bigfix_remote_client_relevance.transports.container_libs import install_command

    with pytest.raises(ValueError, match="brew"):
        install_command("brew", "dbus-libs")
