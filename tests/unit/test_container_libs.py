"""Tests for telling a missing qna apart from a qna that cannot link.

The distinction is not academic: the dynamic linker's message ends in "No such
file or directory", which the missing-binary heuristic matches, so a present
qna on rockylinux:9 was reported as absent. The ROCKY_STDERR constant below is
the real message captured from that image.
"""

from __future__ import annotations

import pytest

from bigfix_remote_client_relevance.transports.container_libs import missing_shared_library

pytestmark = pytest.mark.xfail(
    strict=True, reason="M15: shared-library detection not implemented"
)

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
