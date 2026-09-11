# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
Tests for replication.py, against the synthetic flows in conftest.py.

They pin down three things: that a regenerated archive is one DI will accept,
that a gap check distinguishes scope from deployment state, and that a sync
always starts from an empty shell and leaves the target's identity alone.
"""

from __future__ import annotations

import gzip
import json

import pytest

from di_replication_sync import replication as r

from conftest import ACC, PRD, SUFFIXES, TABLES, archive, flow, spec


# ----------------------------------------------------------------- format ---

@pytest.mark.parametrize("kwargs", [
    {}, {"tables": []}, {"truncate": TABLES}, {"description": ""},
], ids=["full", "empty", "all-truncate", "with-description"])
def test_tar_round_trips_byte_for_byte(kwargs):
    """Repacking an untouched flow reproduces the tar it came from.

    The reference archive is built with tarfile and the result with the hand
    written USTAR header, so this compares two independent implementations. The
    gzip stream is deliberately not compared: DI does not compress with zlib, so
    the deflate bytes differ while the archive is identical.
    """
    original = archive(spec(ACC, **kwargs))
    repacked = r.write_flow(r.read_flow(original))
    assert gzip.decompress(repacked) == gzip.decompress(original)


def test_member_header_matches_di(acc):
    assert acc.member.name == f"{ACC['flow']}{r.SUFFIX}"
    assert (acc.member.uid, acc.member.gid) == (999, 999)
    assert acc.member.gname == "systemd-journal"
    assert acc.member.uname == ""
    assert acc.member.mode == 0o644


def test_payload_is_compact_json_without_trailing_newline(acc):
    tar = gzip.decompress(r.write_flow(acc))
    payload = json.dumps(acc.spec, separators=r.JSON_SEPARATORS).encode()
    assert tar[r.BLOCK:r.BLOCK + len(payload)] == payload
    assert not payload.endswith(b"\n")
    assert len(tar) % r.BLOCK == 0


def test_gzip_container_matches_di(acc):
    """No stored file name, mtime zero, XFL 0 — as DI writes it."""
    produced = r.write_flow(acc)
    assert produced[:3] == b"\x1f\x8b\x08"
    assert produced[3] == 0                     # no FNAME flag
    assert produced[4:8] == b"\0\0\0\0"         # mtime
    assert produced[8] == 0                     # XFL


def test_rejects_a_non_flow_archive():
    with pytest.raises(r.FlowError, match="holds 0 files"):
        r.read_flow(gzip.compress(b"not a tar at all"))


def test_rejects_an_unsupported_flow_version():
    broken = spec(ACC)
    broken["version"] = "MANY_SOURCE_MANY_TARGET"
    with pytest.raises(r.FlowError, match="this tool handles"):
        r.read_flow(archive(broken))


# -------------------------------------------------------------- gap check ---

def test_identical_scope_with_differing_state_is_not_a_gap(acc, prd_deployed):
    """The case that prompted the split.

    Both landscapes replicate the same tables, but the deployed one carries
    truncate on every task. That is state the runtime wrote, not a gap anyone
    can act on, so it must not be reported as one.
    """
    d = r.diff(acc, prd_deployed)
    assert d.added == [] and d.removed == [] and d.changed == []
    assert not d.has_gap
    assert not d.identical                       # something differs, just not scope
    assert {c.field for c in d.state} == {"truncate"}


def test_a_missing_table_is_a_gap(acc):
    short = flow(PRD, tables=TABLES[:-1])
    d = r.diff(acc, short)
    assert d.added == [TABLES[-1]]
    assert d.has_gap


def test_an_extra_table_in_the_target_is_a_gap(acc):
    extra = flow(PRD, tables=TABLES + ["DEMO_STALE"])
    d = r.diff(acc, extra)
    assert d.removed == ["DEMO_STALE"]
    assert d.has_gap


def test_a_load_type_difference_is_a_gap(acc):
    changed = spec(PRD)
    changed["oneSourceOneTargetTasks"][0]["loadType"] = "INITIAL"
    d = r.diff(acc, r.read_flow(archive(changed)))
    assert d.has_gap
    assert [(c.table, c.field) for c in d.changed] == [(TABLES[0], "loadType")]


def test_two_copies_of_the_same_flow_are_identical(acc):
    assert r.diff(acc, acc).identical


def test_gap_check_reports_how_each_affected_table_ends_up(acc, prd_template):
    """The UI shows these values, so they must be real rather than assumed."""
    d = r.diff(acc, prd_template)
    assert d.detail["DEMO_BETA"]["truncate"] is True
    assert d.detail["DEMO_ALPHA"]["truncate"] is None
    assert d.detail["DEMO_ALPHA"]["loadType"] == "REPLICATE"
    assert set(d.detail) == set(d.added) == set(TABLES)


def test_gap_check_reports_connection_differences_without_syncing_them(acc, prd_template):
    d = r.diff(acc, prd_template)
    reported = {(s.side, s.field) for s in d.spaces}
    assert ("source", "connectionId") in reported
    assert ("source", "container") in reported
    assert ("target", "connectionId") in reported


# ---------------------------------------------------------- space naming ---

def test_canonical_names_follow_the_connection(acc):
    assert r.canonical_space_names(acc) == (
        f"{ACC['flow']}_{ACC['abap']}_src", f"{ACC['flow']}_{ACC['store']}_tgt")
    assert r.space_name_problems(acc) == []


def test_a_hand_migrated_flow_is_flagged(prd_hand_migrated):
    """Right connection, wrong name — invisible in the DI UI, caught here."""
    problems = r.space_name_problems(prd_hand_migrated)
    assert len(problems) == 1
    assert ACC["store"] in problems[0]           # the stale name
    assert PRD["store"] in problems[0]           # the connection it really uses


def test_normalise_renames_the_space_and_every_reference(prd_hand_migrated):
    fixed = r.normalise(prd_hand_migrated)
    want = f"{PRD['flow']}_{PRD['store']}_tgt"
    assert fixed.target_space["name"] == want
    assert {t["targetSpace"] for t in fixed.tasks} == {want}
    assert r.space_name_problems(fixed) == []


def test_normalise_changes_nothing_else(prd_hand_migrated):
    fixed = r.normalise(prd_hand_migrated)
    assert r.diff(prd_hand_migrated, fixed).identical
    for before, after in zip(prd_hand_migrated.tasks, fixed.tasks):
        assert before["name"] == after["name"]
        assert before.get("truncate") == after.get("truncate")
    for key in ("connectionId", "connectionType", "container", "datasetProperties"):
        assert (prd_hand_migrated.target_space[key] == fixed.target_space[key])


def test_normalise_is_a_no_op_on_a_sound_flow(acc):
    assert r.write_flow(r.normalise(acc)) == r.write_flow(acc)


def test_a_gap_check_surfaces_a_target_that_needs_normalising(acc, prd_hand_migrated):
    assert r.diff(acc, prd_hand_migrated).target_issues


# ---------------------------------------------------------------- blank ---

def test_blank_keeps_identity_and_drops_scope(acc):
    shell = r.blank(acc)
    assert shell.tasks == []
    assert shell.name == acc.name
    assert shell.spec["sourceSpaces"] == acc.spec["sourceSpaces"]
    assert shell.spec["targetSpaces"] == acc.spec["targetSpaces"]


def test_blank_normalises_on_the_way_through(prd_hand_migrated):
    """A template is the clean starting point, so it must not carry the defect."""
    assert r.space_name_problems(r.blank(prd_hand_migrated)) == []


# ----------------------------------------------------------------- sync ---

def test_sync_carries_the_full_source_scope(migrated, acc):
    assert set(migrated.tables) == set(acc.tables)
    assert len(migrated.tasks) == len(acc.tasks)
    assert all(t["loadType"] == "REPLICATE" for t in migrated.tasks)
    assert all(t["sourceDataset"] == t["targetDataset"] for t in migrated.tasks)


def test_sync_leaves_the_target_identity_untouched(migrated, prd_template):
    assert migrated.name == PRD["flow"]
    assert migrated.spec["description"] == ""
    assert migrated.spec["sourceSpaces"] == prd_template.spec["sourceSpaces"]
    assert migrated.spec["targetSpaces"] == prd_template.spec["targetSpaces"]
    assert migrated.member.name == f"{PRD['flow']}{r.SUFFIX}"


def test_sync_points_every_task_at_the_target_spaces(migrated):
    assert {t["sourceSpace"] for t in migrated.tasks} == {
        f"{PRD['flow']}_{PRD['abap']}_src"}
    assert {t["targetSpace"] for t in migrated.tasks} == {
        f"{PRD['flow']}_{PRD['store']}_tgt"}


def test_sync_preserves_task_name_suffixes(migrated):
    by_table = {t["sourceDataset"]: t["name"] for t in migrated.tasks}
    for table, suffix in SUFFIXES.items():
        assert by_table[table] == f"{PRD['flow']}_{table}_{suffix}"


def test_sync_copies_state_verbatim_by_default(migrated):
    assert {t["sourceDataset"] for t in migrated.tasks if t.get("truncate")} == {
        "DEMO_BETA"}


def test_sync_can_set_state_on_every_task(acc, prd_template):
    """A target landscape whose convention is to clear before every load."""
    every = r.sync(acc, prd_template, state="on")
    assert all(t["truncate"] is True for t in every.tasks)
    assert not r.diff(acc, every).has_gap                  # scope is untouched


def test_sync_can_leave_state_off(acc, prd_template):
    stripped = r.sync(acc, prd_template, state="off")
    assert all("truncate" not in t for t in stripped.tasks)
    assert not r.diff(acc, stripped).identical             # state differs
    assert not r.diff(acc, stripped).has_gap               # scope does not


def test_state_mode_never_touches_scope(acc, prd_template):
    scopes = {mode: sorted(r.sync(acc, prd_template, state=mode).tables)
              for mode in r.STATE_MODES}
    assert len(set(map(tuple, scopes.values()))) == 1


def test_setting_state_on_keeps_dis_key_order(acc, prd_template):
    """truncate goes last, where DI writes it, so a re-export shows no churn."""
    task = r.sync(acc, prd_template, state="on").tasks[0]
    assert list(task)[-1] == "truncate"


def test_sync_rejects_an_unknown_state_mode(acc, prd_template):
    with pytest.raises(r.FlowError, match="state must be one of"):
        r.sync(acc, prd_template, state="maybe")


def test_sync_always_starts_from_an_empty_shell(acc, prd_deployed):
    """A target with stale tables must not keep them.

    prd_deployed carries a table the source has dropped; syncing has to leave no
    trace of it, which is only guaranteed by blanking first.
    """
    stale = flow(PRD, tables=TABLES + ["DEMO_RETIRED"], truncate={"DEMO_RETIRED"})
    out = r.sync(acc, stale)
    assert "DEMO_RETIRED" not in out.tables
    assert "DEMO_RETIRED" not in json.dumps(out.spec)
    assert r.diff(acc, out).identical


def test_sync_normalises_a_hand_migrated_target(acc, prd_hand_migrated):
    out = r.sync(acc, prd_hand_migrated)
    assert r.space_name_problems(out) == []
    assert ACC["store"] not in json.dumps(out.spec)


@pytest.mark.parametrize("token", ["ACC100", ACC["abap"], ACC["store"], "/DEMO/ACC"])
def test_no_source_landscape_token_survives(migrated, token):
    assert token not in json.dumps(migrated.spec, separators=r.JSON_SEPARATORS)


def test_sync_is_idempotent(migrated, acc):
    once = r.write_flow(migrated)
    twice = r.write_flow(r.sync(acc, r.read_flow(once)))
    assert twice == once


def test_syncing_an_already_synced_flow_shows_no_gap(acc, migrated):
    assert r.diff(acc, migrated).identical


def test_sync_refuses_a_mismatched_flow_version(acc, prd_template):
    other = prd_template.copy()
    other.spec["version"] = "MANY_SOURCE_MANY_TARGET"
    with pytest.raises(r.FlowError, match="cannot sync"):
        r.sync(acc, other)


# ----------------------------------------------------------------- verify ---

def test_verify_is_clean_for_a_sync(migrated, acc):
    assert r.verify(migrated, acc) == []


def test_verify_catches_a_task_left_on_the_source_space(migrated, acc):
    broken = migrated.copy()
    broken.tasks[0]["targetSpace"] = f"{ACC['flow']}_{ACC['store']}_tgt"
    problems = r.verify(broken, acc)
    assert any("does not declare" in p for p in problems)


def test_verify_catches_a_dropped_table(migrated, acc):
    broken = migrated.copy()
    broken.spec["oneSourceOneTargetTasks"].pop()
    assert any("missing 1 table" in p for p in r.verify(broken, acc))


def test_verify_catches_a_stale_flow_prefix(migrated, acc):
    broken = migrated.copy()
    broken.tasks[0]["name"] = f"{ACC['flow']}_{TABLES[0]}_{SUFFIXES[TABLES[0]]}"
    assert any("not named for flow" in p for p in r.verify(broken, acc))


def test_verify_catches_a_non_canonical_space_name(prd_hand_migrated, acc):
    """The hand-migration signature, rejected rather than shipped."""
    assert any("DI's convention" in p for p in r.verify(prd_hand_migrated, acc))


def test_summarise_flags_a_template_and_its_issues(prd_template, prd_hand_migrated):
    assert r.summarise(prd_template)["isTemplate"] is True
    assert r.summarise(prd_template)["issues"] == []
    assert r.summarise(prd_hand_migrated)["isTemplate"] is False
    assert r.summarise(prd_hand_migrated)["issues"]


# -------------------------------------------------------------------- cli ---

def test_check_exits_nonzero_on_a_gap(tmp_path, capsys):
    source = tmp_path / "src.tgz"
    target = tmp_path / "tgt.tgz"
    source.write_bytes(archive(spec(ACC)))
    target.write_bytes(archive(spec(PRD, tables=TABLES[:2])))
    assert r.main(["check", str(source), str(target)]) == 1
    assert "GAP" in capsys.readouterr().out


def test_check_exits_zero_when_only_state_differs(tmp_path, capsys):
    source = tmp_path / "src.tgz"
    target = tmp_path / "tgt.tgz"
    source.write_bytes(archive(spec(ACC)))
    target.write_bytes(archive(spec(PRD, truncate=TABLES)))
    assert r.main(["check", str(source), str(target)]) == 0
    out = capsys.readouterr().out
    assert "no gap" in out and "deployment state" in out


def test_template_writes_an_empty_shell(tmp_path):
    source = tmp_path / "flow.tgz"
    source.write_bytes(archive(spec(ACC)))
    out = tmp_path / f"flow{r.TEMPLATE_SUFFIX}"
    assert r.main(["template", str(source), "--out", str(out), "--yes"]) == 0
    assert r.read_flow(out).tasks == []


def test_normalise_command_fixes_a_hand_migrated_flow(tmp_path):
    source = tmp_path / "flow.tgz"
    source.write_bytes(archive(spec(
        PRD, target_space_name=f"{PRD['flow']}_{ACC['store']}_tgt")))
    out = tmp_path / "fixed.tgz"
    assert r.main(["normalise", str(source), "--out", str(out), "--yes"]) == 0
    assert r.space_name_problems(r.read_flow(out)) == []


def test_apply_writes_a_verified_archive(tmp_path):
    source = tmp_path / "src.tgz"
    target = tmp_path / f"tgt{r.TEMPLATE_SUFFIX}"
    out = tmp_path / "out.tgz"
    source.write_bytes(archive(spec(ACC, truncate={"DEMO_BETA"})))
    target.write_bytes(archive(spec(PRD, tables=[])))
    assert r.main(["apply", "--source", str(source), "--target", str(target),
                   "--out", str(out), "--yes"]) == 0
    assert set(r.read_flow(out).tables) == set(TABLES)


@pytest.mark.parametrize("mode,expected", [("copy", 1), ("on", len(TABLES)), ("off", 0)])
def test_apply_honours_the_state_mode(tmp_path, mode, expected):
    source = tmp_path / "src.tgz"
    target = tmp_path / f"tgt{r.TEMPLATE_SUFFIX}"
    out = tmp_path / "out.tgz"
    source.write_bytes(archive(spec(ACC, truncate={"DEMO_BETA"})))
    target.write_bytes(archive(spec(PRD, tables=[])))
    assert r.main(["apply", "--source", str(source), "--target", str(target),
                   "--out", str(out), "--state", mode, "--yes"]) == 0
    assert sum(1 for t in r.read_flow(out).tasks if t.get("truncate")) == expected


def test_apply_says_what_setting_state_on_will_do(tmp_path, capsys):
    source = tmp_path / "src.tgz"
    target = tmp_path / f"tgt{r.TEMPLATE_SUFFIX}"
    source.write_bytes(archive(spec(ACC)))
    target.write_bytes(archive(spec(PRD, tables=[])))
    r.main(["apply", "--source", str(source), "--target", str(target),
            "--out", str(tmp_path / "out.tgz"), "--state", "on", "--yes"])
    assert "cleared of extracted content" in capsys.readouterr().out


def test_summarise_counts_tasks_carrying_state(acc):
    assert r.summarise(acc)["stateCount"] == 1
    assert r.summarise(r.sync(acc, acc, state="on"))["stateCount"] == len(TABLES)


# ------------------------------------------------------------- connections ---

def test_connection_rows_show_both_sides(acc, prd_template):
    rows = r.connection_rows(acc, prd_template)
    sides = {row["side"] for row in rows}
    assert sides == {"reads from", "writes to"}
    fields = {row["field"] for row in rows}
    assert {"space name", "connection", "type", "container"} <= fields
    assert "format" in fields                       # dataset properties, expanded


def test_connection_rows_mark_matches_and_deltas(acc, prd_template):
    by = {(row["side"], row["field"]): row for row in r.connection_rows(acc, prd_template)}
    # Different landscapes: connections differ by design.
    assert by[("reads from", "connection")]["match"] is False
    assert by[("reads from", "connection")]["source"] == ACC["abap"]
    assert by[("reads from", "connection")]["target"] == PRD["abap"]
    # Same write settings on both: a match.
    assert by[("writes to", "format")]["match"] is True
    assert by[("writes to", "type")]["match"] is True


def test_a_differing_connection_is_not_marked_as_changed(acc, prd_template):
    """Red means 'these differ', never 'the sync will update it'.

    Getting this wrong would tell someone their production connection is about
    to be overwritten with acceptance's.
    """
    for row in r.connection_rows(acc, prd_template):
        if row["field"] != "space name":
            assert row["changes"] is False
            assert row["afterSync"] == row["target"]


def test_only_a_non_canonical_space_name_is_marked_as_changed(acc, prd_hand_migrated):
    rows = {(x["side"], x["field"]): x for x in r.connection_rows(acc, prd_hand_migrated)}
    writes = rows[("writes to", "space name")]
    assert writes["changes"] is True
    assert writes["afterSync"] == f"{PRD['flow']}_{PRD['store']}_tgt"
    # and the sound one is not
    assert rows[("reads from", "space name")]["changes"] is False


def test_dataset_properties_are_compared_key_by_key(acc):
    """One differing property must not read as 'all settings differ'."""
    other = spec(PRD)
    other["targetSpaces"][0]["datasetProperties"]["compression"] = "GZIP"
    rows = {x["field"]: x for x in r.connection_rows(acc, r.read_flow(archive(other)))
            if x["side"] == "writes to"}
    assert rows["compression"]["match"] is False
    assert rows["format"]["match"] is True
    assert rows["groupDeltaFilesBy"]["match"] is True


# ----------------------------------------------------------------- backup ---

def test_backup_keeps_the_file_being_replaced(tmp_path):
    target = tmp_path / "flow.tgz"
    target.write_bytes(b"the old one")
    kept = r.backup(target)
    assert kept == tmp_path / f"flow.tgz{r.BACKUP_SUFFIX}"
    assert kept.read_bytes() == b"the old one"


def test_backup_of_a_missing_file_is_a_no_op(tmp_path):
    assert r.backup(tmp_path / "nothing.tgz") is None


def test_apply_backs_up_the_file_it_overwrites(tmp_path):
    source = tmp_path / "src.tgz"
    target = tmp_path / f"tgt{r.TEMPLATE_SUFFIX}"
    out = tmp_path / "out.tgz"
    source.write_bytes(archive(spec(ACC)))
    target.write_bytes(archive(spec(PRD, tables=[])))
    previous = archive(spec(PRD, tables=TABLES[:1]))
    out.write_bytes(previous)

    assert r.main(["apply", "--source", str(source), "--target", str(target),
                   "--out", str(out), "--yes"]) == 0
    kept = out.with_name(out.name + r.BACKUP_SUFFIX)
    assert kept.read_bytes() == previous          # byte-for-byte, not re-packed
    assert len(r.read_flow(out).tables) == len(TABLES)


def test_apply_can_be_told_not_to_back_up(tmp_path):
    source = tmp_path / "src.tgz"
    target = tmp_path / f"tgt{r.TEMPLATE_SUFFIX}"
    out = tmp_path / "out.tgz"
    source.write_bytes(archive(spec(ACC)))
    target.write_bytes(archive(spec(PRD, tables=[])))
    out.write_bytes(b"whatever")
    assert r.main(["apply", "--source", str(source), "--target", str(target),
                   "--out", str(out), "--no-backup", "--yes"]) == 0
    assert not out.with_name(out.name + r.BACKUP_SUFFIX).exists()


def test_apply_writes_no_backup_when_there_was_no_file(tmp_path):
    source = tmp_path / "src.tgz"
    target = tmp_path / f"tgt{r.TEMPLATE_SUFFIX}"
    out = tmp_path / "fresh.tgz"
    source.write_bytes(archive(spec(ACC)))
    target.write_bytes(archive(spec(PRD, tables=[])))
    assert r.main(["apply", "--source", str(source), "--target", str(target),
                   "--out", str(out), "--yes"]) == 0
    assert not out.with_name(out.name + r.BACKUP_SUFFIX).exists()


def test_a_failed_verification_leaves_the_target_and_backup_alone(tmp_path, monkeypatch):
    """Nothing is touched unless the result is sound — not even the backup."""
    source = tmp_path / "src.tgz"
    target = tmp_path / f"tgt{r.TEMPLATE_SUFFIX}"
    out = tmp_path / "out.tgz"
    source.write_bytes(archive(spec(ACC)))
    target.write_bytes(archive(spec(PRD, tables=[])))
    out.write_bytes(b"untouched")
    monkeypatch.setattr(r, "verify", lambda *a, **k: ["something is wrong"])
    assert r.main(["apply", "--source", str(source), "--target", str(target),
                   "--out", str(out), "--yes"]) == 2
    assert out.read_bytes() == b"untouched"
    assert not out.with_name(out.name + r.BACKUP_SUFFIX).exists()


def test_normalise_backs_up_when_writing_over_the_original(tmp_path):
    flow_file = tmp_path / "flow.tgz"
    original = archive(spec(PRD, target_space_name=f"{PRD['flow']}_{ACC['store']}_tgt"))
    flow_file.write_bytes(original)
    assert r.main(["normalise", str(flow_file), "--out", str(flow_file), "--yes"]) == 0
    assert flow_file.with_name(flow_file.name + r.BACKUP_SUFFIX).read_bytes() == original
    assert r.space_name_problems(r.read_flow(flow_file)) == []


# ------------------------------------------------------- write settings ---

def props(flow):
    return flow.target_space[r.PROPERTY_KEY]


@pytest.fixture
def prd_other_settings():
    """Production, writing in a different format from acceptance."""
    other = spec(PRD, tables=[])
    other["targetSpaces"][0][r.PROPERTY_KEY] = {
        "compression": "GZIP", "format": "CSV", "groupDeltaFilesBy": "DAY",
        "objectstore.write.useDuplicateSuppressionInitialLoad": "false"}
    return r.read_flow(archive(other))


def test_write_settings_travel_with_the_flow_by_default(acc, prd_other_settings):
    out = r.sync(acc, prd_other_settings)
    assert props(out) == props(acc)
    assert props(out)["format"] == "PARQUET"
    assert props(out)["groupDeltaFilesBy"] == "HOUR"
    assert props(out)["compression"] == "SNAPPY"


def test_write_settings_can_be_left_alone(acc, prd_other_settings):
    out = r.sync(acc, prd_other_settings, properties="target")
    assert props(out) == props(prd_other_settings)
    assert props(out)["format"] == "CSV"


def test_syncing_write_settings_never_touches_the_connection(acc, prd_other_settings):
    """The whole point of the split: how it writes moves, where it writes does not."""
    out = r.sync(acc, prd_other_settings)
    for field in r.SPACE_IDENTITY_FIELDS:
        assert out.target_space[field] == prd_other_settings.target_space[field]
        assert out.source_space[field] == prd_other_settings.source_space[field]
    assert out.target_space["connectionId"] == PRD["store"]
    assert ACC["store"] not in json.dumps(out.spec)


def test_synced_write_settings_are_a_copy_not_a_reference(acc, prd_other_settings):
    out = r.sync(acc, prd_other_settings)
    props(out)["format"] = "AVRO"
    assert props(acc)["format"] == "PARQUET"       # the source is not mutated


def test_sync_rejects_an_unknown_properties_mode(acc, prd_template):
    with pytest.raises(r.FlowError, match="properties must be one of"):
        r.sync(acc, prd_template, properties="both")


def test_differing_write_settings_are_reported_apart_from_connections(
        acc, prd_other_settings):
    d = r.diff(acc, prd_other_settings)
    assert {x.field for x in d.properties} == {"compression", "format",
                                               "groupDeltaFilesBy"}
    assert all(x.synced for x in d.properties)
    identity = [x for x in d.spaces if not x.synced]
    assert {x.field for x in identity} <= set(r.SPACE_IDENTITY_FIELDS)
    assert all(not x.synced for x in identity)


def test_write_settings_count_towards_identical(acc, prd_other_settings):
    """Two flows writing differently are not identical, even with equal scope."""
    same_scope = r.sync(acc, prd_other_settings, properties="target")
    d = r.diff(acc, same_scope)
    assert not d.has_gap                       # scope matches
    assert d.properties                        # but the settings do not
    assert not d.identical


def test_connection_rows_mark_write_settings_as_rewritten(acc, prd_other_settings):
    rows = {x["field"]: x for x in r.connection_rows(acc, prd_other_settings)
            if x["side"] == "writes to"}
    assert rows["format"]["changes"] is True
    assert rows["format"]["afterSync"] == "PARQUET"
    assert rows["connection"]["changes"] is False       # identity, never


def test_connection_rows_follow_the_properties_mode(acc, prd_other_settings):
    rows = {x["field"]: x for x in
            r.connection_rows(acc, prd_other_settings, properties="target")}
    assert rows["format"]["changes"] is False
    assert rows["format"]["afterSync"] == "CSV"


@pytest.mark.parametrize("mode,expected", [("source", "PARQUET"), ("target", "CSV")])
def test_apply_honours_the_properties_mode(tmp_path, mode, expected):
    source = tmp_path / "src.tgz"
    target = tmp_path / f"tgt{r.TEMPLATE_SUFFIX}"
    out = tmp_path / "out.tgz"
    source.write_bytes(archive(spec(ACC)))
    other = spec(PRD, tables=[])
    other["targetSpaces"][0][r.PROPERTY_KEY]["format"] = "CSV"
    target.write_bytes(archive(other))
    assert r.main(["apply", "--source", str(source), "--target", str(target),
                   "--out", str(out), "--properties", mode, "--yes"]) == 0
    assert r.read_flow(out).target_space[r.PROPERTY_KEY]["format"] == expected


# ------------------------------------------------------------- pasted json ---

def test_a_flow_can_arrive_as_bare_json(acc):
    text = json.dumps(acc.spec, separators=r.JSON_SEPARATORS)
    pasted = r.read_spec(text)
    assert pasted.name == acc.name
    assert set(pasted.tables) == set(acc.tables)
    assert pasted.member.name == f"{ACC['flow']}{r.SUFFIX}"


def test_pasted_json_repacks_into_a_usable_archive(acc):
    """The payload round-trips; the tar header cannot, and should not.

    A pasted flow has no original archive, so its member carries this tool's
    defaults rather than the timestamp of a file that was never there.
    """
    pasted = r.read_spec(json.dumps(acc.spec, separators=r.JSON_SEPARATORS))
    rebuilt = r.read_flow(r.write_flow(pasted))
    assert rebuilt.spec == acc.spec
    assert rebuilt.member.name == acc.member.name
    assert (rebuilt.member.uid, rebuilt.member.gid) == (999, 999)
    assert rebuilt.member.mtime == 0


def test_pasted_json_tolerates_surrounding_whitespace(acc):
    text = "\n\n  " + json.dumps(acc.spec) + "  \n"
    assert r.read_spec(text).name == acc.name


@pytest.mark.parametrize("text,match", [
    ("", "nothing pasted"),
    ("   ", "nothing pasted"),
    ("not json at all", "not valid JSON"),
    ("[]", "not a JSON object"),
    ('{"hello": 1}', "does not look like a replication flow"),
])
def test_pasted_rubbish_is_refused_with_a_reason(text, match):
    with pytest.raises(r.FlowError, match=match):
        r.read_spec(text)
