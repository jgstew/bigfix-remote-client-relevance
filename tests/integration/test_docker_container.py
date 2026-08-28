"""Exercise DockerEngine against a real daemon.

The unit tests drive TransportContainer through a fake engine, so nothing else
covers DockerEngine itself — image pull, container lifecycle, stdin delivery,
and the read-only artifact mount. Auto-skipped when no daemon is reachable::

    uv run pytest -m docker

The provisioning test additionally needs a cached qna artifact; it downloads
one only when network tests are opted into (BFRCR_NETWORK_TESTS=1).
"""

from __future__ import annotations

import os
import textwrap

import pytest

from bigfix_remote_client_relevance.results import ERROR_KIND_BOOTSTRAP
from bigfix_remote_client_relevance.transports.container import (
    DockerEngine,
    TransportContainer,
)

pytestmark = pytest.mark.docker

IMAGE = "ubuntu:22.04"


@pytest.fixture
def stub_qna_image(tmp_path):
    """Build a tiny image whose `qna` is a shell script emitting a transcript.

    Keeps the container tests fast and offline: the real agent artifact is only
    needed by the provisioning test below.
    """
    import docker

    context = tmp_path / "ctx"
    context.mkdir()
    (context / "qna").write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            cat > /dev/null
            echo "A: Ubuntu 22.04 (stub)"
            echo "I: singular string"
            echo "T: 0.1 ms"
            """
        )
    )
    (context / "Dockerfile").write_text(
        textwrap.dedent(
            f"""\
            FROM {IMAGE}
            COPY qna /usr/local/bin/qna
            RUN chmod +x /usr/local/bin/qna
            """
        )
    )
    client = docker.from_env()
    image, _logs = client.images.build(path=str(context), tag="bfrcr-test-qna:latest", rm=True)
    yield "bfrcr-test-qna:latest"
    try:
        client.images.remove(image.id, force=True)
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        print(f"could not remove test image: {exc}")  # noqa: T201 - test cleanup diagnostic


async def test_evaluates_in_a_real_container(stub_qna_image):
    transport = TransportContainer(stub_qna_image, engine=DockerEngine())

    result = await transport.evaluate_client_relevance("name of operating system")

    assert result.error_kind is None, result.error
    assert result.answers == ["Ubuntu 22.04 (stub)"]
    assert result.answer_types == ["singular string"]
    assert result.transport == "container"
    assert result.host.startswith("container:bfrcr-test-qna")


async def test_client_relevance_reaches_container_stdin(tmp_path):
    """The expression must arrive on the container process's stdin intact."""
    import docker

    context = tmp_path / "echo_ctx"
    context.mkdir()
    (context / "qna").write_text("#!/bin/sh\nprintf 'A: '\ncat\necho 'T: 0.1 ms'\n")
    (context / "Dockerfile").write_text(
        f"FROM {IMAGE}\nCOPY qna /usr/local/bin/qna\nRUN chmod +x /usr/local/bin/qna\n"
    )
    client = docker.from_env()
    image, _ = client.images.build(path=str(context), tag="bfrcr-test-echo:latest", rm=True)
    try:
        transport = TransportContainer("bfrcr-test-echo:latest", engine=DockerEngine())
        result = await transport.evaluate_client_relevance("Q: version of client")
    finally:
        client.images.remove(image.id, force=True)

    assert result.answers == ["version of client"], "Q: prefix stripped, text intact"


async def test_missing_qna_in_a_plain_image_maps_to_bootstrap():
    transport = TransportContainer(IMAGE, engine=DockerEngine())

    result = await transport.evaluate_client_relevance("true")

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "qna" in (result.error or "").lower()


async def test_keep_alive_container_is_reused(stub_qna_image):
    transport = TransportContainer(stub_qna_image, engine=DockerEngine(), keep_alive=True)
    try:
        first = await transport.evaluate_client_relevance("TRUE")
        second = await transport.evaluate_client_relevance("TRUE")
    finally:
        await transport.aclose()

    assert first.error_kind is None
    assert second.error_kind is None


@pytest.mark.skipif(
    os.environ.get("BFRCR_NETWORK_TESTS") != "1",
    reason="needs a real qna artifact; set BFRCR_NETWORK_TESTS=1 to download one",
)
async def test_provisions_a_real_qna_version_into_ubuntu(tmp_path):
    """Full resolve -> fetch -> extract on the controller -> mount -> eval."""
    from functools import partial

    from bigfix_remote_client_relevance.bootstrap.cache import ensure_artifact
    from bigfix_remote_client_relevance.bootstrap.extract_local import ensure_extracted
    from bigfix_remote_client_relevance.bootstrap.release_site import (
        artifact_for,
        resolve_version_spec,
    )

    version = resolve_version_spec("11.0")
    ref = artifact_for(version, platform="ubuntu", arch="x86_64")
    resolved = await ensure_artifact(version, ref, cache_dir=tmp_path / "cache")

    transport = TransportContainer(
        IMAGE,
        engine=DockerEngine(),
        target="ubuntu",
        extractor=partial(ensure_extracted, cache_dir=tmp_path / "cache"),
    )
    result = await transport.evaluate_client_relevance(
        "name of operating system", qna=resolved, timeout_s=300.0
    )

    assert result.error_kind is None, result.error
    assert result.qna_version == version
    assert result.answers, "a real qna should answer"
    assert "linux" in result.answers[0].lower()
