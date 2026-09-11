# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
Format checks against whatever real exports happen to be in ./flows.

The folder is git-ignored, so these skip in a clean checkout and in CI. On a
machine that has actual DI exports they are the strongest evidence the tool
has: they prove the archive writer reproduces the real thing, which synthetic
fixtures can only approximate.

Nothing here asserts anything about a particular system — no flow name, table or
connection is named — so the tests stay valid whatever landscape you point them
at.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from di_replication_sync import replication as r

FLOWS = Path(__file__).resolve().parent.parent / "flows"
ARCHIVES = sorted(FLOWS.glob("*.tgz")) + sorted(FLOWS.glob("*.tgz.template"))

pytestmark = pytest.mark.skipif(
    not ARCHIVES, reason="no exports in ./flows — nothing local to check against")


@pytest.mark.parametrize("path", ARCHIVES, ids=lambda p: p.name)
def test_real_export_round_trips_byte_for_byte(path):
    """Repacking a real DI export reproduces its tar exactly."""
    assert gzip.decompress(r.write_flow(r.read_flow(path))) == \
        gzip.decompress(path.read_bytes())


@pytest.mark.parametrize("path", ARCHIVES, ids=lambda p: p.name)
def test_real_export_parses_and_summarises(path):
    flow = r.read_flow(path)
    summary = r.summarise(flow)
    assert summary["member"].endswith(r.SUFFIX)
    assert summary["taskCount"] == len(flow.tasks)
    assert flow.source_space["connectionId"]
    assert flow.target_space["connectionId"]


@pytest.mark.parametrize("path", ARCHIVES, ids=lambda p: p.name)
def test_real_export_uses_canonical_space_names(path):
    """Fails loudly on a flow migrated by hand — run `di-repl-sync normalise`."""
    problems = r.space_name_problems(r.read_flow(path))
    assert problems == [], "; ".join(problems)


@pytest.mark.parametrize("path", ARCHIVES, ids=lambda p: p.name)
def test_a_real_export_syncs_into_its_own_template_unchanged(path):
    """blank() then sync() from the same flow must reproduce it."""
    flow = r.read_flow(path)
    if not flow.tasks:
        pytest.skip("already a template")
    rebuilt = r.sync(flow, r.blank(flow))
    assert r.diff(flow, rebuilt).identical
    assert r.verify(rebuilt, flow) == []
