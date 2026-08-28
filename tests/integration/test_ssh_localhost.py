"""Exercise the real asyncssh adapter against sshd on localhost.

The unit tests drive TransportSSH through a fake runner, so nothing else
covers _AsyncsshRunner itself — connect, run with stdin, and SFTP put. Enable
Remote Login and set up key auth for the current user, then::

    uv run pytest -m ssh_localhost

Auto-skipped when `ssh -o BatchMode=yes localhost true` does not succeed.
"""

from __future__ import annotations

import pytest

from bigfix_remote_client_relevance.results import (
    ERROR_KIND_RELEVANCE,
    ERROR_KIND_TRANSPORT,
)
from bigfix_remote_client_relevance.transports.ssh import TransportSSH

pytestmark = pytest.mark.ssh_localhost


@pytest.fixture
def stub_qna_on_disk(fake_qna, qna_output):
    """A stub qna the SSH transport can be pointed at by absolute path."""
    return fake_qna(stdout=qna_output("multi_answer"))


async def test_evaluates_over_a_real_ssh_connection(stub_qna_on_disk):
    transport = TransportSSH("localhost", target="macos")
    try:
        result = await transport.evaluate_client_relevance(
            "names of folders of folder \"/tmp\"", qna_path=stub_qna_on_disk.path
        )
    finally:
        await transport.aclose()

    assert result.error_kind is None
    assert result.answers == ["alpha", "beta", "gamma"]
    assert result.host == "localhost"
    assert result.transport == "ssh"


async def test_client_relevance_reaches_the_remote_stdin(stub_qna_on_disk):
    transport = TransportSSH("localhost", target="macos")
    try:
        await transport.evaluate_client_relevance(
            "Q: version of client", qna_path=stub_qna_on_disk.path
        )
    finally:
        await transport.aclose()

    assert stub_qna_on_disk.stdin_text == "version of client\n"


async def test_remote_relevance_error_is_reported(fake_qna, qna_output):
    stub = fake_qna(stdout=qna_output("relevance_error"))
    transport = TransportSSH("localhost", target="macos")
    try:
        result = await transport.evaluate_client_relevance("namez of it", qna_path=stub.path)
    finally:
        await transport.aclose()

    assert result.error_kind == ERROR_KIND_RELEVANCE


async def test_sftp_push_and_extract_cycle(tmp_path, fake_qna, qna_output):
    """Push a real archive over SFTP and extract it on the far side."""
    import shutil
    import zipfile

    from bigfix_remote_client_relevance.bootstrap import targets
    from bigfix_remote_client_relevance.results import ResolvedQna

    stub = fake_qna(stdout=qna_output("single_answer"))
    payload = tmp_path / "payload"
    shutil.copytree(stub.directory, payload)

    archive = tmp_path / "QNA11.0.6.137.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for item in payload.iterdir():
            zf.write(item, item.name)

    # A synthetic target: unzip into place, with qna at the archive root.
    spec = targets.TargetSpec(
        name="ziptest",
        family="posix",
        cache_root=str(tmp_path / "remote_cache"),
        qna_relative_path="qna",
        release_platform="ubuntu",
        prereqs=(targets.ExtractionPrereq("unzip", "apt install unzip"),),
        _extract=lambda archive, dest: (
            f"mkdir -p {dest}",
            f"cd {dest} && unzip -o {archive} && chmod +x qna",
        ),
    )
    targets.KNOWN_TARGETS["ziptest"] = spec
    try:
        transport = TransportSSH("localhost", target="ziptest", state_dir=tmp_path / "state")
        try:
            result = await transport.evaluate_client_relevance(
                "name of operating system",
                qna=ResolvedQna(version="11.0.6.137", artifact_path=archive),
            )
        finally:
            await transport.aclose()
    finally:
        del targets.KNOWN_TARGETS["ziptest"]

    assert result.error_kind is None, result.error
    assert result.answers == ["Mac OS 15.5"]
    assert result.qna_version == "11.0.6.137"
    assert (tmp_path / "remote_cache" / "11.0.6.137" / "qna").is_file()


async def test_unreachable_port_maps_to_transport():
    transport = TransportSSH("localhost", port=1, target="macos")

    result = await transport.evaluate_client_relevance("true", qna_path="/bin/true")

    assert result.error_kind == ERROR_KIND_TRANSPORT
