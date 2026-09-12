# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
The core's promises, checked in this tool.

Core-owned (D-LAB-5 tool template): `copier update` rewrites this file. Tool
tests go in their own files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from di_replication_sync import app as web
from di_replication_sync import replication as cli
from di_replication_sync.core import identity, skill_install, version

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / identity.PACKAGE
FAR_AWAY = {"REMOTE_ADDR": "10.11.12.13"}
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(version, "UPDATE_CHECK", False)   # never the network in tests
    web.app.config["TESTING"] = True
    return web.app.test_client()


# ---------------------------------------------------------------- server ----

def test_health_names_this_tool(client):
    got = client.get("/api/health").get_json()
    assert got["tool"] == identity.TOOL_NAME
    assert got["version"] == version.__version__


def test_version_answers_offline(client):
    got = client.get("/api/version").get_json()
    assert got["installed"] == version.__version__
    assert got["disabled"] is True and got["update_available"] is False
    assert got["repo"] == identity.REPO_URL


@pytest.mark.parametrize("path", ["/api/shutdown", "/api/restart", "/api/update"])
def test_lifecycle_is_refused_from_off_the_machine(client, path):
    assert client.post(path, environ_base=FAR_AWAY).status_code == 403


def test_lifecycle_is_post_only(client):
    # a GET from a link in another tab must not stop the server
    assert client.get("/api/shutdown", environ_base=LOCAL).status_code == 405


def test_update_from_a_checkout_says_git_pull(client):
    if version.install_kind() != "checkout":
        pytest.skip("installed copy")
    got = client.post("/api/update", environ_base=LOCAL)
    assert got.status_code == 400 and "git pull" in got.get_json()["output"]


def test_errors_come_back_as_json(client):
    got = client.get("/api/no-such-route")
    assert got.status_code == 404 and got.get_json()["errors"]


def test_the_page_extends_the_core_shell(client):
    html = client.get("/").get_data(as_text=True)
    assert "/core-static/core.js" in html and 'id="core-about"' in html


@pytest.mark.parametrize("asset", ["core.js", "core.css", "dlab5.png"])
def test_core_static_is_served(client, asset):
    assert client.get(f"/core-static/{asset}").status_code == 200


def test_network_detection():
    from di_replication_sync.core.server import on_a_network
    assert not on_a_network("127.0.0.1") and not on_a_network("localhost")
    assert on_a_network("0.0.0.0") and on_a_network("192.168.1.5")


# ------------------------------------------------------------- packaging ----

def test_package_data_ships_what_the_server_reads():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for needed in ('"VERSION"', '"templates/*.html"', '"static/*"', '"skill/*.md"',
                   '"templates/core/*.html"'):
        assert needed in text, f"pyproject package-data is missing {needed}"
    assert f'"{identity.PACKAGE}.core"' in text


def test_version_is_one_number_everywhere():
    wanted = (PKG / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", wanted)
    manifest = ROOT / "plugin" / ".claude-plugin" / "plugin.json"
    if manifest.exists():
        assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == wanted
    pinned = ROOT / "plugin" / "bin" / "VERSION"
    if pinned.exists():
        assert pinned.read_text(encoding="utf-8").strip() == wanted


# ----------------------------------------------------------------- skill ----

def test_the_skill_ships_with_the_package():
    assert skill_install.packaged().is_file()


def test_every_skill_copy_is_in_step():
    copies = skill_install.checkout_copies()
    assert copies, "a checkout should hold .claude/skills and plugin copies"
    assert skill_install.main(["--check"]) == 0


def test_skill_frontmatter_names_this_tool():
    text = skill_install.packaged().read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must open with a YAML frontmatter block"
    front = match.group(1)
    assert re.search(r"^name: (.+)$", front, re.M).group(1) == identity.SKILL_NAME
    assert re.search(r"^description: .{40,}$", front, re.M), "describe when to use it"


def test_every_command_the_skill_names_exists():
    """The skill tells an agent what to run; the parser has to accept it."""
    text = skill_install.packaged().read_text(encoding="utf-8")
    documented = set(re.findall(rf"^{re.escape(identity.CLI_COMMAND)} ([a-z][\w-]*)", text, re.M))
    for command in documented:
        with pytest.raises(SystemExit) as done:          # --help exits 0 after printing
            cli.main([command, "--help"])
        assert done.value.code == 0, f"`{identity.CLI_COMMAND} {command}` is not a command"
