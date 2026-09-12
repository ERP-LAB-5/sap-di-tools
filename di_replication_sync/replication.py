#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ERP-LAB-5
"""
replication.py — read, compare and sync SAP Data Intelligence replication flows.

A DI replication flow is exported as a .tgz holding exactly one file,
``<FLOW_NAME>.replication``, which is compact JSON: the flow name, one source
space, one target space, and a list of one-source-one-target tasks — one per
table being replicated.

Promoting a flow between landscapes means moving the *task list* and nothing
else. The connections differ by design: acceptance reads a different ABAP client
and writes a different object store than production. So the target keeps its own
identity and receives the source's scope.

Three ideas carry most of the weight here.

**Scope versus deployment state.** ``loadType`` and ``targetDataset`` say what is
replicated; a gap in those is a real difference between two landscapes.
``truncate`` is different: it is a *deployment* setting, telling the flow to
clear the target of previously extracted content before it loads — on an object
store target, that means emptying the prefix so a re-initialisation does not land
beside stale files. It is set when a flow is deployed rather than in the
modeller, appears only as ``true`` (DI omits the key when it is off), and can
legitimately differ between two landscapes that replicate exactly the same
tables. So it is reported and can be set on a sync, but it never counts as a gap.

**Canonical space names.** DI names a space ``<flow>_<connectionId>_src`` or
``_tgt``. A hand-migrated flow tends to keep the *source* landscape's connection
id in that name while pointing at the right connection, which is invisible in the
UI and misleads the next person to read the export. :func:`normalise` fixes it.

**Always start from an empty template.** :func:`sync` blanks the target's task
list before filling it, so nothing from a previous scope can survive. Extract
that shell once with :func:`blank` and keep it as ``<FLOW>.tgz.template``.

Standard library only, so it runs as a CLI and under test without the web front
end::

    di-repl-sync check    SOURCE.tgz TARGET.tgz
    di-repl-sync template TARGET.tgz --out TARGET.tgz.template
    di-repl-sync apply    --source SOURCE.tgz --target TARGET.tgz.template \\
                          --out RESULT.tgz
    di-repl-sync normalise FLOW.tgz --out FLOW.tgz
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

BLOCK = 512
SUFFIX = ".replication"
TEMPLATE_SUFFIX = ".tgz.template"
ONE_TO_ONE = "ONE_SOURCE_ONE_TARGET"

# DI writes the payload with no whitespace and no trailing newline. Any deviation
# would show up as a spurious diff the next time someone re-exports the flow.
JSON_SEPARATORS = (",", ":")

# Task attributes that say *what* is replicated. A difference here is a gap.
SCOPE_FIELDS = ("targetDataset", "loadType")

# Deployment settings rather than scope: reported, settable, never a gap.
STATE_FIELDS = ("truncate",)

# What each of them looks like when switched on. DI omits the key entirely when
# a setting is off, so "off" means absent rather than ``false``.
STATE_ON = {"truncate": True}

# How a sync treats them. "copy" mirrors the source, which is right when both
# landscapes are deployed the same way; "on" and "off" set them outright, which
# is what you want when the target's deployment convention differs from the
# source's — a landscape that always clears its target before loading, say.
STATE_MODES = ("copy", "on", "off")

TASK_FIELDS = SCOPE_FIELDS + STATE_FIELDS

# Where a space points. These are what make a flow belong to one landscape, and
# a sync must never copy them across.
SPACE_IDENTITY_FIELDS = ("connectionId", "connectionType", "container")

# How the target is written: file format, compression, delta grouping. These
# describe the data rather than the landscape, so the same flow should write the
# same way wherever it runs — which makes them part of what a sync moves.
PROPERTY_KEY = "datasetProperties"

# What to do with them: take the source's, or leave the target's alone.
PROPERTY_MODES = ("source", "target")


class FlowError(Exception):
    """A .tgz that is not a replication-flow export, or a sync that cannot run."""


# ------------------------------------------------------------------ archive ---

@dataclass(frozen=True)
class Member:
    """The tar header of the single file inside the archive.

    Carried across a read/write cycle so a regenerated archive is
    indistinguishable from a DI export. The defaults are what DI itself writes:
    uid/gid 999, which resolves to the systemd-journal group in its container.
    """

    name: str
    mode: int = 0o644
    uid: int = 999
    gid: int = 999
    uname: str = ""
    gname: str = "systemd-journal"
    mtime: int = 0


def _octal(value: int, width: int) -> bytes:
    """A tar numeric field: zero-padded octal with a trailing NUL."""
    return ("%0*o" % (width - 1, value)).encode("ascii") + b"\0"


def _ustar_header(member: Member, size: int) -> bytes:
    """One 512-byte USTAR header.

    Written by hand rather than through :mod:`tarfile` because tarfile pads the
    archive out to its 10 KiB record size on close, and DI's archives are not
    padded — header, data, two zero blocks, done.
    """
    buf = bytearray(BLOCK)

    def put(offset: int, limit: int, raw: bytes) -> None:
        if len(raw) > limit:
            raise FlowError(f"tar field at offset {offset} overflows: {raw!r}")
        buf[offset:offset + len(raw)] = raw

    put(0, 100, member.name.encode("utf-8"))
    put(100, 8, _octal(member.mode, 8))
    put(108, 8, _octal(member.uid, 8))
    put(116, 8, _octal(member.gid, 8))
    put(124, 12, _octal(size, 12))
    put(136, 12, _octal(member.mtime, 12))
    buf[148:156] = b" " * 8                 # checksum is summed over spaces
    buf[156:157] = b"0"                     # typeflag: regular file
    put(257, 6, b"ustar\0")
    put(263, 2, b"00")
    put(265, 32, member.uname.encode("utf-8"))
    put(297, 32, member.gname.encode("utf-8"))
    put(329, 8, _octal(0, 8))
    put(337, 8, _octal(0, 8))
    buf[148:156] = ("%06o" % sum(buf)).encode("ascii") + b"\0 "
    return bytes(buf)


def _pad(raw: bytes) -> bytes:
    """Round up to a whole number of tar blocks."""
    return raw + b"\0" * (-len(raw) % BLOCK)


BACKUP_SUFFIX = ".bk"


def backup(path: Union[str, Path]) -> Optional[Path]:
    """Copy an existing file aside before it is overwritten.

    Returns where it went, or None when there was nothing to keep. A sync
    replaces every task in the target, so the version being replaced is the only
    record of what the flow looked like before — worth a byte copy.
    """
    path = Path(path)
    if not path.exists():
        return None
    kept = path.with_name(path.name + BACKUP_SUFFIX)
    kept.write_bytes(path.read_bytes())
    return kept


# --------------------------------------------------------------------- flow ---

@dataclass
class Flow:
    """One replication flow, plus where it came from and how it was packed."""

    spec: Dict[str, Any]
    member: Member
    origin: str = ""

    @property
    def name(self) -> str:
        return self.spec.get("name", "")

    @property
    def version(self) -> str:
        return self.spec.get("version", "")

    @property
    def tasks(self) -> List[Dict[str, Any]]:
        return self.spec.setdefault("oneSourceOneTargetTasks", [])

    @property
    def source_space(self) -> Dict[str, Any]:
        return _only(self.spec.get("sourceSpaces") or [], "source space", self.name)

    @property
    def target_space(self) -> Dict[str, Any]:
        return _only(self.spec.get("targetSpaces") or [], "target space", self.name)

    @property
    def tables(self) -> Dict[str, Dict[str, Any]]:
        """Tasks keyed by source dataset — the table scope of the flow."""
        return {task["sourceDataset"]: task for task in self.tasks}

    def copy(self) -> "Flow":
        return Flow(spec=json.loads(json.dumps(self.spec)),
                    member=self.member, origin=self.origin)


def _only(items: Sequence[Dict[str, Any]], what: str, flow: str) -> Dict[str, Any]:
    if len(items) != 1:
        raise FlowError(
            f"{flow or 'flow'} declares {len(items)} {what}s; this tool handles "
            f"{ONE_TO_ONE} flows, which have exactly one")
    return items[0]


def read_flow(src: Union[str, Path, bytes], origin: str = "") -> Flow:
    """Parse a .tgz export into a :class:`Flow`."""
    if isinstance(src, (str, Path)):
        origin = origin or str(src)
        try:
            data = Path(src).read_bytes()
        except OSError as exc:
            raise FlowError(f"cannot read {src}: {exc}") from exc
    else:
        data = src
        origin = origin or "<upload>"

    try:
        tar = gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise FlowError(f"{origin} is not gzip-compressed: {exc}") from exc

    member, payload = _extract(tar, origin)

    if not member.name.endswith(SUFFIX):
        raise FlowError(
            f"{origin} holds {member.name!r}, not a *{SUFFIX} file — this does "
            f"not look like a replication-flow export")
    return _flow_from_json(payload, member, origin)


def read_spec(text: Union[str, bytes], origin: str = "<pasted>") -> Flow:
    """A flow from bare ``.replication`` JSON, with no archive around it.

    The archive is only a wrapper; the JSON inside is the flow. Someone who can
    copy that text out of a system they cannot download from — or who was sent it
    in a message — has everything needed, so accept it directly rather than
    making them rebuild a .tgz by hand. The member header is reconstructed from
    the flow name, which is where DI takes it from anyway.
    """
    if isinstance(text, str):
        text = text.strip().encode("utf-8")
    if not text:
        raise FlowError("nothing pasted")
    flow = _flow_from_json(text, Member(name="pasted" + SUFFIX), origin)
    return replace_member_name(flow)


def replace_member_name(flow: Flow) -> Flow:
    """Name the archive member after the flow, as DI does."""
    if flow.name:
        flow.member = replace(flow.member, name=f"{flow.name}{SUFFIX}")
    return flow


def _flow_from_json(payload: bytes, member: Member, origin: str) -> Flow:
    try:
        spec = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FlowError(f"{origin} is not valid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise FlowError(f"{origin} is not a JSON object")
    if "oneSourceOneTargetTasks" not in spec or "name" not in spec:
        raise FlowError(
            f"{origin} has no 'name' or 'oneSourceOneTargetTasks' — this does not "
            f"look like a replication flow")

    flow = Flow(spec=spec, member=member, origin=origin)
    if flow.version and flow.version != ONE_TO_ONE:
        raise FlowError(
            f"{origin} is a {flow.version!r} flow; this tool handles {ONE_TO_ONE}")
    return flow


def _extract(tar: bytes, origin: str) -> Tuple[Member, bytes]:
    """The single regular file in an uncompressed tar stream."""
    found: List[Tuple[Member, bytes]] = []
    offset = 0
    while offset + BLOCK <= len(tar):
        header = tar[offset:offset + BLOCK]
        if header == b"\0" * BLOCK:                 # end-of-archive marker
            break
        name = header[0:100].rstrip(b"\0").decode("utf-8")
        size = int(header[124:136].rstrip(b"\0 ").decode("ascii") or "0", 8)
        typeflag = header[156:157]
        offset += BLOCK
        if typeflag in (b"0", b"\0"):               # regular file
            found.append((
                Member(
                    name=name,
                    mode=int(header[100:108].rstrip(b"\0 ").decode() or "644", 8),
                    uid=int(header[108:116].rstrip(b"\0 ").decode() or "0", 8),
                    gid=int(header[116:124].rstrip(b"\0 ").decode() or "0", 8),
                    uname=header[265:297].rstrip(b"\0").decode("utf-8"),
                    gname=header[297:329].rstrip(b"\0").decode("utf-8"),
                    mtime=int(header[136:148].rstrip(b"\0 ").decode() or "0", 8),
                ),
                tar[offset:offset + size],
            ))
        offset += -(-size // BLOCK) * BLOCK

    if len(found) != 1:
        raise FlowError(
            f"{origin} holds {len(found)} files; a replication-flow export holds "
            f"exactly one")
    return found[0]


def write_flow(flow: Flow) -> bytes:
    """Pack a :class:`Flow` back into an uploadable .tgz.

    The gzip container is written the way DI's own exports are — no stored file
    name, ``mtime`` zero, OS byte 0xff — but the deflate stream itself will not
    be byte-identical, because DI does not compress with zlib. What round-trips
    exactly is the tar inside, which is the only part DI reads back.
    """
    payload = json.dumps(flow.spec, separators=JSON_SEPARATORS).encode("utf-8")
    tar = _ustar_header(flow.member, len(payload)) + _pad(payload) + b"\0" * (2 * BLOCK)

    buf = io.BytesIO()
    # compresslevel 6 keeps the XFL header byte at 0, as DI's exports have it;
    # 9 would write 2 and 1 would write 4.
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(tar)
    return buf.getvalue()


# ------------------------------------------------------------ space naming ---

def canonical_space_names(flow: Flow) -> Tuple[str, str]:
    """What DI would call this flow's two spaces: ``<flow>_<connectionId>_src/tgt``."""
    return (f"{flow.name}_{flow.source_space.get('connectionId', '')}_src",
            f"{flow.name}_{flow.target_space.get('connectionId', '')}_tgt")


def space_name_problems(flow: Flow) -> List[str]:
    """Space names that do not follow DI's convention, with the fix.

    A flow migrated by replacing the name prefix keeps the *old* landscape's
    connection id in the space name — pointing at the right connection under a
    name that says otherwise.
    """
    try:
        want_src, want_tgt = canonical_space_names(flow)
    except FlowError as exc:
        return [str(exc)]

    problems = []
    for label, space, want in (("source", flow.source_space, want_src),
                               ("target", flow.target_space, want_tgt)):
        have = space.get("name", "")
        if have != want:
            problems.append(
                f"{label} space is named {have!r} but connects to "
                f"{space.get('connectionId')!r}; DI's convention is {want!r}")
    return problems


def normalise(flow: Flow) -> Flow:
    """Rename both spaces to the canonical form and repoint every task at them.

    Only names change. The connections, the table scope and every task attribute
    are left exactly as they were.
    """
    out = flow.copy()
    want_src, want_tgt = canonical_space_names(out)
    was_src = out.source_space.get("name")
    was_tgt = out.target_space.get("name")

    out.source_space["name"] = want_src
    out.target_space["name"] = want_tgt
    for task in out.tasks:
        if task.get("sourceSpace") in (was_src, None):
            task["sourceSpace"] = want_src
        if task.get("targetSpace") in (was_tgt, None):
            task["targetSpace"] = want_tgt
    return out


def blank(flow: Flow) -> Flow:
    """The flow with no tasks: connections, name and description, nothing else.

    This is the shell a sync fills. Keeping one as ``<FLOW>.tgz.template`` means
    a promotion always starts from a known-empty target, so no table from a
    previous scope can survive one.
    """
    out = normalise(flow)
    out.spec["oneSourceOneTargetTasks"] = []
    return out


# --------------------------------------------------------------------- diff ---

@dataclass
class Change:
    """One table present on both sides but configured differently."""

    table: str
    field: str
    source: Any
    target: Any


@dataclass
class SpaceDiff:
    """A space attribute that differs between the two flows.

    ``synced`` separates the two kinds: a write setting travels with the flow, a
    connection is landscape identity and stays put however different it looks.
    """

    side: str
    field: str
    source: Any
    target: Any
    synced: bool = False


@dataclass
class Diff:
    """The gap check: what the target is missing relative to the source."""

    added: List[str] = field(default_factory=list)      # in source, not target
    removed: List[str] = field(default_factory=list)    # in target, not source
    changed: List[Change] = field(default_factory=list)  # scope, on both sides
    state: List[Change] = field(default_factory=list)   # deployment state only
    common: List[str] = field(default_factory=list)
    spaces: List[SpaceDiff] = field(default_factory=list)
    target_issues: List[str] = field(default_factory=list)

    # How each affected table would be configured after the sync, so a caller
    # can show real values rather than assuming the defaults.
    detail: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def has_gap(self) -> bool:
        """True when the two landscapes replicate different things.

        Deployment state is excluded on purpose: a flow can have identical
        scope across two landscapes and still differ on ``truncate`` for every
        single task, which is not a gap anyone can act on.
        """
        return bool(self.added or self.removed or self.changed)

    @property
    def properties(self) -> List[SpaceDiff]:
        """Write settings that differ — the part of the spaces a sync moves."""
        return [d for d in self.spaces if d.synced]

    @property
    def identical(self) -> bool:
        """True when a sync would change nothing at all, state included."""
        return not (self.has_gap or self.state or self.properties)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "added": self.added,
            "removed": self.removed,
            "changed": [vars(c) for c in self.changed],
            "state": [vars(c) for c in self.state],
            "common": self.common,
            "spaces": [vars(s) for s in self.spaces],
            "properties": [vars(s) for s in self.properties],
            "targetIssues": self.target_issues,
            "detail": self.detail,
            "hasGap": self.has_gap,
            "identical": self.identical,
            "stateFields": list(STATE_FIELDS),
        }


def diff(source: Flow, target: Flow) -> Diff:
    """Compare two flows: what would change in *target* if synced from *source*."""
    src, tgt = source.tables, target.tables
    out = Diff(
        added=sorted(set(src) - set(tgt)),
        removed=sorted(set(tgt) - set(src)),
        common=sorted(set(src) & set(tgt)),
        target_issues=space_name_problems(target),
    )
    for table in out.common:
        for name in SCOPE_FIELDS:
            a, b = src[table].get(name), tgt[table].get(name)
            if a != b:
                out.changed.append(Change(table=table, field=name, source=a, target=b))
        for name in STATE_FIELDS:
            a, b = src[table].get(name), tgt[table].get(name)
            if a != b:
                out.state.append(Change(table=table, field=name, source=a, target=b))

    # Added and changed tables end up configured the way the source has them;
    # removed ones are described as the target has them today.
    touched = {c.table for c in out.changed}
    for table in out.added + sorted(touched):
        out.detail[table] = {name: src[table].get(name) for name in TASK_FIELDS}
    for table in out.removed:
        out.detail[table] = {name: tgt[table].get(name) for name in TASK_FIELDS}

    for side, getter in (("source", "source_space"), ("target", "target_space")):
        try:
            a, b = getattr(source, getter), getattr(target, getter)
        except FlowError:
            continue
        for name in SPACE_IDENTITY_FIELDS:
            if a.get(name) != b.get(name):
                out.spaces.append(
                    SpaceDiff(side=side, field=name,
                              source=a.get(name), target=b.get(name)))
        # Key by key, so one differing property does not read as "all the write
        # settings differ".
        props_a, props_b = a.get(PROPERTY_KEY) or {}, b.get(PROPERTY_KEY) or {}
        for name in sorted(set(props_a) | set(props_b)):
            if props_a.get(name) != props_b.get(name):
                out.spaces.append(
                    SpaceDiff(side=side, field=name, source=props_a.get(name),
                              target=props_b.get(name), synced=True))
    return out


def connection_rows(source: Flow, target: Flow,
                    properties: str = "source") -> List[Dict[str, Any]]:
    """Both flows' connections, field by field, with what a sync does to each.

    The point of showing the source's side is that the connection rows *should*
    differ — a landscape reads a different client and writes a different bucket,
    by design — while the write settings should not. So a row says three things:
    the two values, whether they match, and whether the sync touches the
    target's. Only the space name and the write settings are ever rewritten.
    """
    rows: List[Dict[str, Any]] = []
    try:
        canon_src, canon_tgt = canonical_space_names(target)
        pairs = [("reads from", source.source_space, target.source_space, canon_src),
                 ("writes to", source.target_space, target.target_space, canon_tgt)]
    except FlowError as exc:
        return [{"side": "", "field": "spaces", "source": str(exc), "target": "",
                 "match": False, "afterSync": "", "changes": False}]

    def row(side, field, a, b, after=None, changes=False):
        rows.append({"side": side, "field": field,
                     "source": a, "target": b,
                     "match": a == b,
                     "afterSync": after if after is not None else b,
                     "changes": changes})

    for side, src, tgt, canonical in pairs:
        # Rewritten, but only when it is not already right.
        row(side, "space name", src.get("name"), tgt.get("name"),
            after=canonical, changes=tgt.get("name") != canonical)
        # Landscape identity: shown, never touched, however different.
        row(side, "connection", src.get("connectionId"), tgt.get("connectionId"))
        row(side, "type", src.get("connectionType"), tgt.get("connectionType"))
        row(side, "container", src.get("container"), tgt.get("container"))
        # Dataset properties are the target's write settings; compare key by key
        # so a single differing one does not read as "everything differs".
        props_src = src.get(PROPERTY_KEY) or {}
        props_tgt = tgt.get(PROPERTY_KEY) or {}
        for key in sorted(set(props_src) | set(props_tgt)):
            a, b = props_src.get(key), props_tgt.get(key)
            takes_source = properties == "source" and key in props_src
            row(side, key, a, b,
                after=a if takes_source else b,
                changes=takes_source and a != b)
    return rows


# --------------------------------------------------------------------- sync ---

def _swap_prefix(name: str, old: str, new: str) -> str:
    """Rename a task from one flow to another, keeping DI's random suffix.

    Keeping the suffix is what makes a sync idempotent: run it twice and you get
    the same bytes, so a re-sync reads as an empty gap rather than every task
    renamed.
    """
    if old and name.startswith(old):
        return new + name[len(old):]
    return f"{new}_{name}"


def sync(source: Flow, target: Flow, state: str = "copy",
         properties: str = "source") -> Flow:
    """Return *target* with *source*'s table scope, starting from an empty shell.

    The target keeps its own name, description and both connections; its space
    names are normalised on the way through.

    *state* decides what happens to the deployment settings in
    :data:`STATE_FIELDS`: ``"copy"`` mirrors the source, ``"on"`` sets them on
    every task, ``"off"`` leaves them off every task. Set them outright whenever
    the target landscape's deployment convention is its own — a production
    system that always clears the target before loading does not want
    acceptance's partially-set flags.

    *properties* decides the write settings under :data:`PROPERTY_KEY` — format,
    compression, delta grouping. ``"source"`` moves them with the flow, which is
    the default because the same flow should write the same way in every
    landscape; ``"target"`` leaves the target's own alone.
    """
    if state not in STATE_MODES:
        raise FlowError(
            f"state must be one of {', '.join(STATE_MODES)}, not {state!r}")
    if properties not in PROPERTY_MODES:
        raise FlowError(
            f"properties must be one of {', '.join(PROPERTY_MODES)}, "
            f"not {properties!r}")
    if source.version and target.version and source.version != target.version:
        raise FlowError(
            f"cannot sync a {source.version!r} flow into a {target.version!r} one")

    out = blank(target)                       # always start from empty
    src_space, tgt_space = canonical_space_names(out)

    if properties == "source":
        # How the data is written travels with the flow; where it is written
        # does not. Only this key moves — the connection is untouched.
        for getter in ("source_space", "target_space"):
            incoming = getattr(source, getter).get(PROPERTY_KEY)
            if incoming is not None:
                getattr(out, getter)[PROPERTY_KEY] = json.loads(json.dumps(incoming))
    old, new = source.name, out.name

    tasks = []
    for task in source.tasks:
        if "sourceDataset" not in task:
            raise FlowError(f"task {task.get('name', '?')!r} has no sourceDataset")
        rebuilt = {}
        for key, value in task.items():       # preserve DI's key order
            if key in STATE_FIELDS and state != "copy":
                continue                      # re-added below when state is "on"
            if key == "name":
                rebuilt[key] = _swap_prefix(value, old, new)
            elif key == "sourceSpace":
                rebuilt[key] = src_space
            elif key == "targetSpace":
                rebuilt[key] = tgt_space
            else:
                rebuilt[key] = value
        rebuilt.setdefault("sourceSpace", src_space)
        rebuilt.setdefault("targetSpace", tgt_space)
        if state == "on":
            rebuilt.update(STATE_ON)
        tasks.append(rebuilt)

    out.spec["oneSourceOneTargetTasks"] = tasks
    out.member = replace(out.member, name=f"{new}{SUFFIX}")
    return out


# ------------------------------------------------------------------- verify ---

def _identity_tokens(flow: Flow) -> List[str]:
    """The strings that make a flow belong to one landscape."""
    tokens = [flow.name]
    for spaces in ("sourceSpaces", "targetSpaces"):
        for space in flow.spec.get(spaces) or []:
            for key in ("name", "connectionId", "technicalName",
                        "ccmConnectionId", "container"):
                value = space.get(key)
                if isinstance(value, str) and value not in ("", "/"):
                    tokens.append(value)
    return tokens


def verify(flow: Flow, source: Optional[Flow] = None) -> List[str]:
    """Post-conditions for a synced flow. Returns the problems, empty if sound."""
    problems: List[str] = []

    try:
        spaces = {flow.source_space["name"], flow.target_space["name"]}
    except FlowError as exc:
        return [str(exc)]

    problems.extend(space_name_problems(flow))

    seen = set()
    for task in flow.tasks:
        label = task.get("name", "<unnamed>")
        for key in ("sourceSpace", "targetSpace"):
            if task.get(key) not in spaces:
                problems.append(
                    f"task {label!r} points {key} at {task.get(key)!r}, which the "
                    f"flow does not declare")
        if not label.startswith(flow.name):
            problems.append(f"task {label!r} is not named for flow {flow.name!r}")
        if label in seen:
            problems.append(f"duplicate task name {label!r}")
        seen.add(label)

    if source is not None:
        if set(flow.tables) != set(source.tables):
            missing = sorted(set(source.tables) - set(flow.tables))
            extra = sorted(set(flow.tables) - set(source.tables))
            if missing:
                problems.append(f"missing {len(missing)} table(s): {', '.join(missing)}")
            if extra:
                problems.append(f"unexpected table(s): {', '.join(extra)}")
        if len(flow.tasks) != len(source.tasks):
            problems.append(f"{len(flow.tasks)} tasks, expected {len(source.tasks)}")

        # The check that catches a half-finished hand migration: no string
        # identifying the source landscape may survive in the output.
        leaked = set(_identity_tokens(source)) - set(_identity_tokens(flow))
        payload = json.dumps(flow.spec, separators=JSON_SEPARATORS)
        for token in sorted(leaked):
            if token in payload:
                problems.append(
                    f"source identifier {token!r} still appears in the output")
    return problems


# ---------------------------------------------------------------- summarise ---

def summarise(flow: Flow) -> Dict[str, Any]:
    """The one-line facts about a flow, for the folder listing and the UI."""
    try:
        src, tgt = flow.source_space, flow.target_space
    except FlowError:
        src = tgt = {}
    return {
        "flowName": flow.name,
        "member": flow.member.name,
        "version": flow.version,
        "taskCount": len(flow.tasks),
        "stateCount": sum(1 for t in flow.tasks
                          if any(t.get(f) for f in STATE_FIELDS)),
        "tables": sorted(flow.tables),
        "isTemplate": not flow.tasks,
        "issues": space_name_problems(flow) if src and tgt else [],
        "sourceSpace": {"name": src.get("name"), "connectionId": src.get("connectionId"),
                        "connectionType": src.get("connectionType"),
                        "container": src.get("container")},
        "targetSpace": {"name": tgt.get("name"), "connectionId": tgt.get("connectionId"),
                        "connectionType": tgt.get("connectionType"),
                        "container": tgt.get("container"),
                        "datasetProperties": tgt.get("datasetProperties")},
    }


# ---------------------------------------------------------------------- cli ---

def _report(source: Flow, target: Flow, d: Diff) -> None:
    """The gap check, as text."""
    print(f"  source  {source.name}  ({len(source.tasks)} tables)  {source.origin}")
    print(f"  target  {target.name}  ({len(target.tasks)} tables)  {target.origin}")
    print()

    if d.identical:
        print(f"  no gap — {len(d.common)} tables, identical on both sides")
    elif not d.has_gap:
        fields = ", ".join(sorted({c.field for c in d.state}))
        print(f"  no gap — the same {len(d.common)} tables on both sides")
        print(f"           {len(d.state)} task(s) differ on {fields}, which is "
              f"deployment state, not scope")
    else:
        print(f"  GAP: {len(d.added)} to add · {len(d.removed)} to remove · "
              f"{len(d.changed)} changed · {len(d.common)} in common")
        for table in d.added:
            print(f"    + {table}")
        for table in d.removed:
            print(f"    - {table}")
        for c in d.changed:
            print(f"    ~ {c.table}: {c.field} {c.target!r} -> {c.source!r}")
        if d.state:
            fields = ", ".join(sorted({c.field for c in d.state}))
            print(f"    ({len(d.state)} further difference(s) in {fields} — "
                  f"deployment state, not counted as a gap)")

    if d.target_issues:
        print("\n  target needs normalising:")
        for line in d.target_issues:
            print(f"    ! {line}")

    identity = [x for x in d.spaces if not x.synced]
    if identity:
        print("\n  connections differ, and stay as they are:")
        for x in identity:
            print(f"    {x.side}.{x.field}: target {x.target!r}, source {x.source!r}")
    if d.properties:
        print("\n  write settings differ, and travel with the flow:")
        for x in d.properties:
            print(f"    {x.side}.{x.field}: target {x.target!r}, source {x.source!r}")


def _confirm(prompt: str, skip: bool) -> bool:
    if skip:
        return True
    return input(f"  {prompt} [y/N] ").strip().lower() in ("y", "yes")


def _write(flow: Flow, out: Path, label: str) -> None:
    out.write_bytes(write_flow(flow))
    print(f"  written: {out} ({out.stat().st_size} bytes) — {label}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="di-repl-sync",
        description="Compare and sync SAP Data Intelligence replication flows.")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="gap check between two flows (exits 1 on a gap)")
    p.add_argument("source")
    p.add_argument("target")

    p = sub.add_parser("template", help="write the empty shell of a flow")
    p.add_argument("flow")
    p.add_argument("--out", help=f"default: <flow>{TEMPLATE_SUFFIX}")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("normalise", help="fix space names that do not match the connection")
    p.add_argument("flow")
    p.add_argument("--out", required=True)
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("apply", help="sync the source's scope into the target")
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True, help="ideally a .tgz.template shell")
    p.add_argument("--out", required=True)
    p.add_argument("--state", choices=STATE_MODES, default="copy",
                   help=f"what to do with {', '.join(STATE_FIELDS)}: copy the "
                        f"source (default), or set it on or off for every task")
    p.add_argument("--properties", choices=PROPERTY_MODES, default="source",
                   help="write settings (format, compression, delta grouping): "
                        "take the source's (default) or keep the target's")
    p.add_argument("--no-backup", action="store_true",
                   help=f"do not keep the overwritten file as *{BACKUP_SUFFIX}")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    args = ap.parse_args(argv)

    try:
        if args.command == "template":
            flow = read_flow(args.flow)
            out = Path(args.out or (str(args.flow).removesuffix(".tgz") + TEMPLATE_SUFFIX))
            shell = blank(flow)
            print(f"  {flow.name}: {len(flow.tasks)} tables -> empty shell")
            for line in space_name_problems(flow):
                print(f"    fixing: {line}")
            if not _confirm(f"write {out}?", args.yes):
                print("  cancelled")
                return 1
            _write(shell, out, "empty template")
            return 0

        if args.command == "normalise":
            flow = read_flow(args.flow)
            problems = space_name_problems(flow)
            if not problems:
                print(f"  {flow.name}: space names already canonical, nothing to do")
                return 0
            for line in problems:
                print(f"  ! {line}")
            fixed = normalise(flow)
            print(f"  {len(fixed.tasks)} task reference(s) follow the rename")
            if not _confirm(f"write {args.out}?", args.yes):
                print("  cancelled")
                return 1
            kept = backup(args.out)
            if kept:
                print(f"  kept the previous file as {kept.name}")
            _write(fixed, Path(args.out), "space names normalised")
            return 0

        source = read_flow(args.source)
        target = read_flow(args.target)
        d = diff(source, target)
        _report(source, target, d)

        if args.command == "check":
            return 1 if d.has_gap else 0

        out = Path(args.out)
        if target.tasks:
            print(f"\n  ! {args.target} is not an empty template — its "
                  f"{len(target.tasks)} tasks will be discarded and replaced")
        print(f"\n  will write {out} — flow {target.name}, {len(source.tasks)} tables,"
              f" tasks pointing at {' → '.join(canonical_space_names(target))}")
        moved = [d for d in d.properties] if args.properties == "source" else []
        if moved:
            print(f"  {len(moved)} write setting(s) will be taken from the source: "
                  + ", ".join(f"{m.field} {m.target!r} -> {m.source!r}" for m in moved))
        elif args.properties == "target":
            print("  write settings stay as the target has them")
        if args.state == "on":
            print(f"  {', '.join(STATE_FIELDS)} will be set on every task — the "
                  f"target is cleared of extracted content before it loads")
        elif args.state == "off":
            print(f"  {', '.join(STATE_FIELDS)} will be left off every task")
        if out.exists():
            print(f"  ! {out} exists and will be overwritten"
                  + ("" if args.no_backup
                     else f", keeping the current one as {out.name}{BACKUP_SUFFIX}"))
        if not _confirm("proceed?", args.yes):
            print("  cancelled")
            return 1

        synced = sync(source, target, state=args.state,
                      properties=args.properties)
        problems = verify(synced, source)
        if problems:
            print("\n  verification failed, nothing written:")
            for line in problems:
                print(f"    ! {line}")
            return 2
        # Verification passed, so the write is going ahead — keep the version it
        # replaces before touching it.
        kept = None if args.no_backup else backup(out)
        if kept:
            print(f"  kept the previous {out.name} as {kept.name}")
        _write(synced, out, "verification clean")
        return 0
    except FlowError as exc:
        print(f"  ! {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
