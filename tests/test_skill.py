# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
The packaged skill and the checkout's copy have to stay in step.

A symlink between them does not survive a clone on Windows, so there are two
real files. This is what stops them drifting.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from di_replication_sync import replication as r
from di_replication_sync import skill_install

CHECKOUT = (Path(__file__).resolve().parent.parent
            / ".claude" / "skills" / skill_install.SKILL_NAME / "SKILL.md")


def test_the_skill_ships_with_the_package():
    assert skill_install.packaged().is_file()


def test_the_checkout_copy_has_not_drifted():
    assert CHECKOUT.is_file(), f"missing {CHECKOUT}"
    assert skill_install.main(["--check", str(CHECKOUT)]) == 0


def test_frontmatter_is_well_formed():
    text = skill_install.packaged().read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must open with a YAML frontmatter block"
    front = match.group(1)
    assert re.search(r"^name: (.+)$", front, re.M).group(1) == skill_install.SKILL_NAME
    assert re.search(r"^description: (.+)$", front, re.M)


def test_every_documented_command_exists():
    """The skill tells an agent what to run; the parser has to accept it."""
    text = skill_install.packaged().read_text(encoding="utf-8")
    documented = set(re.findall(r"^di-repl-sync (\w+)", text, re.M))
    assert documented == {"check", "template", "normalise", "apply"}
    for command in documented:
        with pytest.raises(SystemExit):        # --help exits 0 after printing
            r.main([command, "--help"])


def test_every_documented_state_mode_exists():
    text = skill_install.packaged().read_text(encoding="utf-8")
    assert set(re.findall(r"`(copy|on|off)`", text)) == set(r.STATE_MODES)


# An SAP system identifier followed by a client: three characters, then C, then
# three digits — the prefix a replication flow is normally named for. Matched by
# shape rather than against a list, because a list of the systems to keep out
# would itself be the leak.
#
# Lookarounds rather than \b on the right: a real flow name continues into
# "_SOME_TABLE", and underscore is a word character, so \b never fires there and
# the guard would silently match nothing.
SYSTEM_SHAPED = re.compile(r"(?<![A-Z0-9])[A-Z][A-Z0-9]{2}C\d{3}(?!\d)")

REPO = Path(__file__).resolve().parent.parent

# Everything published, which is everything except the runtime folder, the
# licence text and whatever the tooling leaves behind.
PUBLISHED = [
    path for path in REPO.rglob("*")
    if path.is_file()
    and path.suffix in {".py", ".md", ".html", ".css", ".js", ".toml", ".sh", ".txt"}
    and not any(part in {".venv", ".git", ".idea", "flows", "__pycache__",
                         ".pytest_cache"} for part in path.parts)
    and path.name != "LICENSE"
]


@pytest.mark.parametrize("path", PUBLISHED, ids=lambda p: str(p.relative_to(REPO)))
def test_no_real_system_identifier_is_published(path):
    """This repository is public. Nobody's landscape may appear in it.

    Flow names, SIDs, clients, connection ids and table lists all live in the
    exports under ./flows, which is git-ignored. Nothing should copy one out.
    """
    found = SYSTEM_SHAPED.findall(path.read_text(encoding="utf-8"))
    assert not found, f"{path.name} names {', '.join(sorted(set(found)))}"


def test_the_guard_would_catch_a_leak():
    """A test that always passes is worth nothing — prove this one can fail.

    The probe is assembled at runtime rather than written out, because a
    SID-shaped literal here would be caught by the check above. This file is
    inside the scan, and should be.
    """
    probe = "ZZZ" + "C" + "999"
    assert SYSTEM_SHAPED.search(f"promote {probe}_SOME_FLOW to production")
    assert SYSTEM_SHAPED.search(f"{probe}.replication")
    assert not SYSTEM_SHAPED.search("promote ACC_FLOW to production")
    assert not SYSTEM_SHAPED.search("ACC100_DEMO_FLOW")     # the fixtures stay legal
