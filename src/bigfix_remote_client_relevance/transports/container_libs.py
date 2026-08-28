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
    raise NotImplementedError


__all__ = ["missing_shared_library"]
