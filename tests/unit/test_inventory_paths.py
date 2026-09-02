"""Tests for locating the default remote_clients.toml inventory.

Search order: current directory -> ~/.bigfix (per-user) -> the platform's
all-users config directory. Mirrors qna_paths.py's precedent: a
default_candidates() builder plus a find_*_path(candidates=None) that takes
the first existing file, with keyword overrides on the builder as the
hermetic seam for tests (no monkeypatching Path.home/Path.cwd needed).
"""

from __future__ import annotations

from bigfix_remote_client_relevance.inventory_paths import (
    DEFAULT_INVENTORY_FILENAME,
    SHARED_CONFIG_DIR_NAME,
    default_candidates,
    find_inventory_path,
)


def test_default_candidates_order(tmp_path):
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    site_dir = tmp_path / "site"

    candidates = default_candidates(cwd=cwd, home=home, site_dir=site_dir)

    assert candidates == (
        cwd / DEFAULT_INVENTORY_FILENAME,
        home / f".{SHARED_CONFIG_DIR_NAME}" / DEFAULT_INVENTORY_FILENAME,
        site_dir / DEFAULT_INVENTORY_FILENAME,
    )


def test_default_filename_is_remote_clients_toml():
    assert DEFAULT_INVENTORY_FILENAME == "remote_clients.toml"


def test_shared_config_dir_name_is_bigfix_not_the_package_name():
    """Deliberately the shared cross-project name, not this package's own name."""
    assert SHARED_CONFIG_DIR_NAME == "bigfix"


def test_cwd_takes_precedence_over_user_and_site(tmp_path):
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    site_dir = tmp_path / "site"
    for d in (cwd, home / f".{SHARED_CONFIG_DIR_NAME}", site_dir):
        d.mkdir(parents=True)
    (cwd / DEFAULT_INVENTORY_FILENAME).write_text("[hosts.a]\n")
    (home / f".{SHARED_CONFIG_DIR_NAME}" / DEFAULT_INVENTORY_FILENAME).write_text("[hosts.b]\n")
    (site_dir / DEFAULT_INVENTORY_FILENAME).write_text("[hosts.c]\n")

    found = find_inventory_path(default_candidates(cwd=cwd, home=home, site_dir=site_dir))

    assert found == cwd / DEFAULT_INVENTORY_FILENAME


def test_user_folder_wins_when_cwd_has_none(tmp_path):
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    site_dir = tmp_path / "site"
    cwd.mkdir()
    (home / f".{SHARED_CONFIG_DIR_NAME}").mkdir(parents=True)
    site_dir.mkdir()
    (home / f".{SHARED_CONFIG_DIR_NAME}" / DEFAULT_INVENTORY_FILENAME).write_text("[hosts.b]\n")
    (site_dir / DEFAULT_INVENTORY_FILENAME).write_text("[hosts.c]\n")

    found = find_inventory_path(default_candidates(cwd=cwd, home=home, site_dir=site_dir))

    assert found == home / f".{SHARED_CONFIG_DIR_NAME}" / DEFAULT_INVENTORY_FILENAME


def test_site_folder_wins_when_cwd_and_user_have_none(tmp_path):
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    site_dir = tmp_path / "site"
    cwd.mkdir()
    (home / f".{SHARED_CONFIG_DIR_NAME}").mkdir(parents=True)
    site_dir.mkdir()
    (site_dir / DEFAULT_INVENTORY_FILENAME).write_text("[hosts.c]\n")

    found = find_inventory_path(default_candidates(cwd=cwd, home=home, site_dir=site_dir))

    assert found == site_dir / DEFAULT_INVENTORY_FILENAME


def test_returns_none_when_nothing_exists(tmp_path):
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    site_dir = tmp_path / "site"

    found = find_inventory_path(default_candidates(cwd=cwd, home=home, site_dir=site_dir))

    assert found is None


def test_find_inventory_path_defaults_to_default_candidates(tmp_path, monkeypatch):
    """With no explicit candidates, it falls through to the real search order."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / DEFAULT_INVENTORY_FILENAME).write_text("[hosts.a]\n")

    found = find_inventory_path()

    assert found == tmp_path / DEFAULT_INVENTORY_FILENAME


def test_a_directory_named_like_the_file_is_not_a_match(tmp_path):
    """is_file(), not exists() -- a same-named directory must not be mistaken."""
    cwd = tmp_path / "cwd"
    (cwd / DEFAULT_INVENTORY_FILENAME).mkdir(parents=True)
    home = tmp_path / "home"
    site_dir = tmp_path / "site"

    found = find_inventory_path(default_candidates(cwd=cwd, home=home, site_dir=site_dir))

    assert found is None
