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


@pytest.mark.skipif(
    os.environ.get("BFRCR_NETWORK_TESTS") != "1",
    reason="needs a real qna artifact and package downloads; set BFRCR_NETWORK_TESTS=1",
)
async def test_a_minimal_rpm_image_gets_its_missing_library_installed(tmp_path):
    """rockylinux:9 ships no libdbus-1.so.3, so qna cannot start without help.

    The whole point of installing during the prepared-image build is that the
    fix is committed once, so the second run does no work at all.
    """
    from functools import partial

    from bigfix_remote_client_relevance.bootstrap.cache import ensure_artifact
    from bigfix_remote_client_relevance.bootstrap.extract_local import ensure_extracted
    from bigfix_remote_client_relevance.bootstrap.release_site import (
        artifact_for,
        resolve_version_spec,
    )
    from bigfix_remote_client_relevance.transports.container import prepared_image_tag

    version = resolve_version_spec("11.0")
    ref = artifact_for(version, platform="rhel", arch="x86_64")
    resolved = await ensure_artifact(version, ref, cache_dir=tmp_path / "cache")

    engine = DockerEngine()
    extractor = partial(ensure_extracted, cache_dir=tmp_path / "cache")

    def transport(**kwargs):
        return TransportContainer(
            "rockylinux:9", engine=engine, target="rhel", extractor=extractor, **kwargs
        )

    tag = prepared_image_tag(await engine.image_digest("rockylinux:9"), version, "x86_64")
    try:
        first = await transport(rebuild_image=True).evaluate_client_relevance(
            "number of properties", qna=resolved, timeout_s=600.0
        )
        assert first.error_kind is None, first.error
        assert first.answers

        second = await transport().evaluate_client_relevance(
            "number of properties", qna=resolved, timeout_s=600.0
        )
        assert second.answers == first.answers, "the cached image must answer identically"
    finally:
        import docker.errors

        try:
            engine._get_client().images.remove(tag, force=True)  # type: ignore[attr-defined]
        except (docker.errors.ImageNotFound, docker.errors.APIError):
            pass


@pytest.mark.skipif(
    os.environ.get("BFRCR_NETWORK_TESTS") != "1",
    reason="needs a real qna artifact; set BFRCR_NETWORK_TESTS=1",
)
async def test_without_auto_setup_a_missing_library_is_reported_not_installed(tmp_path):
    """The air-gapped path must fail loudly and name the library, never silently."""
    from functools import partial

    from bigfix_remote_client_relevance.bootstrap.cache import ensure_artifact
    from bigfix_remote_client_relevance.bootstrap.extract_local import ensure_extracted
    from bigfix_remote_client_relevance.bootstrap.release_site import (
        artifact_for,
        resolve_version_spec,
    )
    from bigfix_remote_client_relevance.results import ERROR_KIND_BOOTSTRAP

    version = resolve_version_spec("11.0")
    ref = artifact_for(version, platform="rhel", arch="x86_64")
    resolved = await ensure_artifact(version, ref, cache_dir=tmp_path / "cache")

    result = await TransportContainer(
        "rockylinux:9",
        engine=DockerEngine(),
        target="rhel",
        extractor=partial(ensure_extracted, cache_dir=tmp_path / "cache"),
        auto_setup=False,
        rebuild_image=True,
    ).evaluate_client_relevance("number of properties", qna=resolved, timeout_s=600.0)

    assert result.error_kind == ERROR_KIND_BOOTSTRAP
    assert "libdbus-1.so.3" in (result.error or "")
    assert "no qna in image" not in (result.error or "")


# --- warm containers, against a real daemon ---------------------------------


async def test_a_warm_container_survives_the_transport_that_started_it(stub_qna_image):
    """The cross-process reuse case, in one process: the first transport
    leaves the container running, the second finds and adopts it."""
    from bigfix_remote_client_relevance.transports.container import stop_warm_containers

    first = TransportContainer(stub_qna_image, engine=DockerEngine(), keep_alive=True)
    await first.evaluate_client_relevance("TRUE")
    warmed = first._container_id
    await first.aclose()

    second = TransportContainer(stub_qna_image, engine=DockerEngine(), keep_alive=True)
    try:
        result = await second.evaluate_client_relevance("TRUE")
        adopted = second._container_id
    finally:
        await second.aclose()
        await stop_warm_containers(DockerEngine())

    assert result.error_kind is None
    assert warmed is not None
    assert adopted == warmed, "the second transport started its own container"


async def test_a_container_removes_itself_once_its_deadline_passes(stub_qna_image):
    """The only cleanup that survives losing the host process."""
    import asyncio

    import docker

    engine = DockerEngine(idle_ttl_s=3)
    container_id = await engine.start(stub_qna_image)
    client = docker.from_env()
    try:
        assert client.containers.get(container_id).status == "running"

        for _ in range(30):
            await asyncio.sleep(1)
            if client.containers.get(container_id).status != "running":
                break

        assert client.containers.get(container_id).status != "running", (
            "the deadline loop never exited"
        )
    finally:
        await engine.stop(container_id)


async def test_use_keeps_a_container_past_its_original_deadline(stub_qna_image):
    """Extending the window is what makes the periodic serial case work."""
    import asyncio

    import docker

    engine = DockerEngine(idle_ttl_s=6)
    container_id = await engine.start(stub_qna_image)
    client = docker.from_env()
    try:
        for _ in range(4):
            await asyncio.sleep(2)
            assert await engine.renew(container_id)

        assert client.containers.get(container_id).status == "running", (
            "renewing must push the deadline out, not merely reset a fixed one"
        )
    finally:
        await engine.stop(container_id)
