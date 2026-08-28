"""Tests for the hosts.toml inventory loader."""

from __future__ import annotations

import pytest

from bigfix_remote_client_relevance.inventory import InventoryError, load_inventory

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
"""


@pytest.fixture
def inventory_file(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_loads_every_host(inventory_file):
    targets = load_inventory(inventory_file)

    assert {t.name for t in targets} == {"mac-test", "win11-lab", "ubuntu-22"}


def test_table_name_is_the_ssh_alias(inventory_file):
    targets = {t.name: t for t in load_inventory(inventory_file)}

    assert targets["mac-test"].kind == "ssh"
    assert targets["mac-test"].become is True


def test_user_is_carried_through(inventory_file):
    targets = {t.name: t for t in load_inventory(inventory_file)}

    assert targets["win11-lab"].user == "labadmin"
    assert targets["win11-lab"].become is False


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
