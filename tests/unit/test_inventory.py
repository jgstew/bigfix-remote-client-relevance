"""Tests for the hosts.toml inventory loader."""

from __future__ import annotations

import pytest

from bigfix_remote_client_relevance.inventory import (
    InventoryError,
    load_inventory,
    update_inventory_platform,
)

SAMPLE = """
[defaults]
qna_version = "11.0"

[hosts.mac-test]
transport = "ssh"
become = true

[hosts.win11-lab]
transport = "ssh"
user = "labadmin"

[hosts.ubuntu-22]
transport = "container"
image = "ubuntu:22.04"
qna_version = "10.0"

[hosts.this-machine]
transport = "local"
"""


@pytest.fixture
def inventory_file(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_loads_every_host(inventory_file):
    targets = load_inventory(inventory_file)

    assert {t.name for t in targets} == {"mac-test", "win11-lab", "ubuntu-22", "this-machine"}


def test_table_name_is_the_ssh_alias(inventory_file):
    targets = {t.name: t for t in load_inventory(inventory_file)}

    assert targets["mac-test"].kind == "ssh"
    assert targets["mac-test"].become is True


def test_user_is_carried_through(inventory_file):
    targets = {t.name: t for t in load_inventory(inventory_file)}

    assert targets["win11-lab"].user == "labadmin"


def test_unset_become_is_left_unspecified_not_forced_false(inventory_file):
    """None, not False, so default_transport_factory can still imply --become
    for a `local` host on a macOS controller."""
    targets = {t.name: t for t in load_inventory(inventory_file)}

    assert targets["win11-lab"].become is None


def test_inventory_local_host_implies_become_on_macos(inventory_file, monkeypatch):
    """The feature this was all for: a local host in hosts.toml with no
    `become` line still gets sudo on a macOS controller, same as --local."""
    import sys

    from bigfix_remote_client_relevance.orchestrate import default_transport_factory

    monkeypatch.setattr(sys, "platform", "darwin")
    targets = {t.name: t for t in load_inventory(inventory_file)}
    assert targets["this-machine"].become is None  # unresolved at load time

    transport = default_transport_factory(targets["this-machine"])

    assert transport._become is True


def test_container_host_carries_its_image(inventory_file):
    targets = {t.name: t for t in load_inventory(inventory_file)}

    assert targets["ubuntu-22"].kind == "container"
    assert targets["ubuntu-22"].image == "ubuntu:22.04"


def test_defaults_are_inherited(inventory_file):
    targets = {t.name: t for t in load_inventory(inventory_file)}

    assert targets["mac-test"].qna_version == "11.0"


def test_per_host_value_overrides_the_default(inventory_file):
    targets = {t.name: t for t in load_inventory(inventory_file)}

    assert targets["ubuntu-22"].qna_version == "10.0"


def test_missing_file_reports_the_path(tmp_path):
    with pytest.raises(InventoryError) as excinfo:
        load_inventory(tmp_path / "absent.toml")

    assert "absent.toml" in str(excinfo.value)


def test_malformed_toml_reports_the_path(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text("[hosts.oops\n", encoding="utf-8")

    with pytest.raises(InventoryError) as excinfo:
        load_inventory(path)

    assert "broken.toml" in str(excinfo.value)


def test_unknown_transport_is_rejected(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text('[hosts.weird]\ntransport = "telepathy"\n', encoding="utf-8")

    with pytest.raises(InventoryError) as excinfo:
        load_inventory(path)

    assert "telepathy" in str(excinfo.value)


def test_container_host_without_an_image_is_rejected(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text('[hosts.nope]\ntransport = "container"\n', encoding="utf-8")

    with pytest.raises(InventoryError) as excinfo:
        load_inventory(path)

    assert "image" in str(excinfo.value)


def test_inventory_without_hosts_is_rejected(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text('[defaults]\nqna_version = "11.0"\n', encoding="utf-8")

    with pytest.raises(InventoryError):
        load_inventory(path)


def test_transport_defaults_to_ssh(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text("[hosts.plain]\n", encoding="utf-8")

    targets = load_inventory(path)

    assert targets[0].kind == "ssh"


# --- writing a probed/corrected platform back ------------------------------
#
# tomlkit, not tomllib+dict, because a naive rewrite would silently drop
# every comment and reorder every table -- and this file is exactly the kind
# a person hand-edits and keeps under version control.


def test_update_inventory_platform_adds_the_key_when_absent(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(
        '[hosts.win-box]  # a comment worth keeping\ntransport = "ssh"\n', encoding="utf-8"
    )

    update_inventory_platform(path, "win-box", "windows")

    text = path.read_text(encoding="utf-8")
    assert "# a comment worth keeping" in text, "tomlkit must preserve comments"
    targets = {t.name: t for t in load_inventory(path)}
    assert targets["win-box"].platform == "windows"


def test_update_inventory_platform_overwrites_a_wrong_value(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(
        '[hosts.win-box]\ntransport = "ssh"\nplatform = "ubuntu"\n', encoding="utf-8"
    )

    update_inventory_platform(path, "win-box", "windows")

    targets = {t.name: t for t in load_inventory(path)}
    assert targets["win-box"].platform == "windows"


def test_update_inventory_platform_leaves_other_hosts_untouched(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(
        '[hosts.win-box]\ntransport = "ssh"\n\n[hosts.other]\ntransport = "ssh"\n',
        encoding="utf-8",
    )

    update_inventory_platform(path, "win-box", "windows")

    targets = {t.name: t for t in load_inventory(path)}
    assert targets["other"].platform is None


def test_update_inventory_platform_keeps_crlf_line_endings(tmp_path):
    """A CRLF file must survive the rewrite unchanged.

    Text-mode writing would translate tomlkit's reproduced CRLFs a second time,
    leaving \r\r\n behind -- which tomllib then refuses to read back.
    """
    path = tmp_path / "hosts.toml"
    path.write_bytes(b'[hosts.win-box]  # kept\r\ntransport = "ssh"\r\n')

    update_inventory_platform(path, "win-box", "windows")

    raw = path.read_bytes()
    assert b"\r\r\n" not in raw
    assert b'transport = "ssh"\r\n' in raw, "the existing lines keep their CRLFs"
    # tomlkit ends the key it appends with a plain \n; a mixed-ending file is
    # still valid TOML, so the round-trip below is what has to hold.
    targets = {t.name: t for t in load_inventory(path)}
    assert targets["win-box"].platform == "windows"


def test_update_inventory_platform_unknown_host_raises(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text('[hosts.win-box]\ntransport = "ssh"\n', encoding="utf-8")

    with pytest.raises(InventoryError) as excinfo:
        update_inventory_platform(path, "nope", "windows")

    assert "nope" in str(excinfo.value)
