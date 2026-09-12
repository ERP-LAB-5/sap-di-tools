#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 ERP-LAB-5
"""
app.py — browser front end for replication.py.

Pick a source flow and a target flow, see exactly which tables would move and
which connections would stay put, confirm, and get back an uploadable .tgz.

Flows come from two places, and the UI treats them alike: the folder the server
was started in, and drag-and-drop upload. The folder is what you want on the box
that holds the exports; upload is what you want when they are on your laptop.

    di-repl-sync-web                       # http://127.0.0.1:8766
    di-repl-sync-web --port 9000 --flows-dir ~/flows
    python3 -m di_replication_sync.app     # the same, from a checkout

All comparing, syncing and verifying lives in replication.py; this module only
moves flows between the browser and disk. Nothing here talks to a DI tenant —
the generated archive is uploaded by hand, deliberately.

The page shell, About (with the update check), Restart, Stop and the loopback
guard come from core/, which the D-LAB-5 tool template maintains.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from flask import Response, abort, jsonify, render_template, request

from . import replication as rp
from .core import server
from .core.server import only_local

app = server.create_app(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024      # a flow is a few KB

HERE = Path(__file__).resolve().parent

# Where the exports live. Git-ignored by default, because a replication-flow
# export carries system ids, connection names and a full table list. Resolved
# once at startup so a later chdir cannot move it.
FLOWS_DIR = Path("flows").resolve()

# What a flow file may be called. A DI export is <FLOW>.tgz; .tgz.template is
# this tool's own convention for the empty shell a landscape starts from.
FLOW_GLOBS = ("*.tgz", "*.tgz.template", "*.tgz.bk", "*.tgz.template.bk")
NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,128}$")

# Uploads and generated archives, held in memory for the life of the process.
# They are a few kilobytes each and belong to one browser session; writing them
# to disk would only leave litter behind.
_STORE: Dict[str, Tuple[str, bytes]] = {}
_STORE_LIMIT = 64

# The About box shows where this server reads flows from.
server.about_extras(lambda: {"Flows folder": str(FLOWS_DIR)})


# ----------------------------------------------------------------- helpers ---
#
# only_local (from core.server) guards every write into the flows folder, as
# well as shutdown, restart and update: a page in another tab cannot navigate
# the server into changing files.

def flow_path(name: str) -> Path:
    """Resolve a file name to a path inside the flows folder, or refuse it."""
    if not NAME_RE.match(name or ""):
        abort(400, "file name may only contain letters, digits, dot, - and _")
    path = (FLOWS_DIR / name).resolve()
    if path.parent != FLOWS_DIR:            # belt and braces after the name check
        abort(400, "file name escapes the flows directory")
    return path


def remember(name: str, data: bytes) -> str:
    """Park an archive in the session store and hand back its handle."""
    if len(_STORE) >= _STORE_LIMIT:         # oldest first; dicts keep insertion order
        _STORE.pop(next(iter(_STORE)))
    handle = secrets.token_urlsafe(12)
    _STORE[handle] = (name, data)
    return handle


def resolve(ref: Any, what: str) -> Tuple[rp.Flow, Optional[str]]:
    """Turn a {kind, name|handle} reference into a flow, and its folder name."""
    if not isinstance(ref, dict):
        abort(400, f"{what} must be an object with a 'kind'")
    kind = ref.get("kind")
    try:
        if kind == "folder":
            name = ref.get("name") or ""
            path = flow_path(name)
            if not path.is_file():
                abort(404, f"no such flow in the folder: {name}")
            return rp.read_flow(path, origin=name), name
        if kind == "upload":
            entry = _STORE.get(ref.get("handle") or "")
            if entry is None:
                abort(404, f"that upload is no longer held — send the {what} again")
            return rp.read_flow(entry[1], origin=entry[0]), None
    except rp.FlowError as exc:
        abort(400, str(exc))
    abort(400, f"{what} kind must be 'folder' or 'upload', not {kind!r}")


def describe(flow: rp.Flow, source_name: Optional[str]) -> Dict[str, Any]:
    out = rp.summarise(flow)
    out["file"] = source_name or flow.origin
    out["fromFolder"] = source_name is not None
    return out


# ------------------------------------------------------------------ routes ---

@app.get("/")
def index() -> str:
    return render_template("index.html", flows_dir=str(FLOWS_DIR))


@app.get("/favicon.ico")
def favicon():
    """Browsers ask for this by name whatever the page links to."""
    return app.send_static_file("favicon.svg")


@app.get("/api/flows")
def list_flows() -> Response:
    """Every readable flow in the folder, with enough detail to choose one."""
    found = []
    for pattern in FLOW_GLOBS:
        found.extend(FLOWS_DIR.glob(pattern))

    listing = []
    for path in sorted(set(found)):
        entry: Dict[str, Any] = {
            "file": path.name,
            "bytes": path.stat().st_size,
            "modified": int(path.stat().st_mtime),
            "isBackup": path.name.endswith(rp.BACKUP_SUFFIX),
        }
        try:
            entry.update(rp.summarise(rp.read_flow(path, origin=path.name)))
        except rp.FlowError as exc:
            entry["error"] = str(exc)       # listed but unselectable, with the reason
        listing.append(entry)
    return jsonify({"dir": str(FLOWS_DIR), "flows": listing})


@app.post("/api/upload")
def upload() -> Response:
    """Take a .tgz from the browser and hold it for this session."""
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        abort(400, "no file in the request")
    data = uploaded.read()
    try:
        flow = rp.read_flow(data, origin=uploaded.filename)
    except rp.FlowError as exc:
        abort(400, str(exc))
    handle = remember(uploaded.filename, data)
    return jsonify({"handle": handle, **describe(flow, None),
                    "file": uploaded.filename})


@app.post("/api/paste")
def paste() -> Response:
    """Take .replication JSON pasted into the browser and hold it like an upload.

    Repacked into an archive straight away, so everything downstream sees the
    same thing whether a flow arrived as a file or as text.
    """
    body = request.get_json(silent=True) or {}
    try:
        flow = rp.read_spec(body.get("text") or "", origin="<pasted>")
    except rp.FlowError as exc:
        abort(400, str(exc))
    name = f"{flow.name}.tgz"
    handle = remember(name, rp.write_flow(flow))
    return jsonify({"handle": handle, **describe(flow, None), "file": name})


@app.post("/api/diff")
def compare() -> Response:
    """The gap check: what the target is missing relative to the source."""
    body = request.get_json(silent=True) or {}
    source, source_name = resolve(body.get("source"), "source")
    target, target_name = resolve(body.get("target"), "target")
    d = rp.diff(source, target)
    canonical = rp.canonical_space_names(target)
    return jsonify({
        "source": describe(source, source_name),
        "target": describe(target, target_name),
        "diff": d.as_dict(),
        "connections": rp.connection_rows(
            source, target, properties=body.get("properties", "source")),
        # What Apply would produce, restated so the confirm step can show it
        # without guessing at the naming rules.
        "outcome": {
            "flowName": target.name,
            "member": f"{target.name}{rp.SUFFIX}",
            "taskCount": len(source.tasks),
            # A sync blanks the target first, so the result always uses the
            # canonical space names even when the target's are stale.
            "sourceSpace": canonical[0],
            "targetSpace": canonical[1],
            "suggestedFile": f"{target.name}.tgz",
            "targetHasTasks": len(target.tasks),
            "stateFields": list(rp.STATE_FIELDS),
            "stateModes": list(rp.STATE_MODES),
            "propertyModes": list(rp.PROPERTY_MODES),
            "sourceStateCount": sum(
                1 for t in source.tasks
                if any(t.get(f) for f in rp.STATE_FIELDS)),
        },
    })


@app.post("/api/apply")
def apply() -> Response:
    """Generate the synced archive. Writes to the folder only when asked to."""
    body = request.get_json(silent=True) or {}
    source, source_name = resolve(body.get("source"), "source")
    target, target_name = resolve(body.get("target"), "target")

    try:
        synced = rp.sync(source, target, state=body.get("state", "copy"),
                         properties=body.get("properties", "source"))
    except rp.FlowError as exc:
        abort(400, str(exc))

    problems = rp.verify(synced, source)
    if problems:
        # A failed sync is a bug in this tool, not a user error — say so loudly
        # and hand back nothing, rather than a file someone might upload.
        return jsonify({"ok": False, "problems": problems}), 422

    data = rp.write_flow(synced)
    out_name = body.get("outName") or f"{synced.name}.tgz"
    written = kept = None
    if body.get("writeBack"):
        only_local("writing into the flows folder")
        path = flow_path(out_name)
        # A sync replaces every task, so the file being overwritten is the only
        # record of what the flow was. Keep it unless asked not to.
        if not body.get("noBackup"):
            saved = rp.backup(path)
            kept = saved.name if saved else None
        path.write_bytes(data)
        written = str(path)

    return jsonify({
        "ok": True,
        "problems": [],
        "handle": remember(out_name, data),
        "file": out_name,
        "bytes": len(data),
        "written": written,
        "backup": kept,
        "result": describe(synced, None),
    })


def _write_folder_copy(flow: rp.Flow, out_name: str, what: str) -> Response:
    """Shared tail of the template and normalise actions."""
    only_local(what)
    path = flow_path(out_name)
    saved = rp.backup(path)
    path.write_bytes(rp.write_flow(flow))
    return jsonify({"ok": True, "written": str(path), "file": out_name,
                    "backup": saved.name if saved else None,
                    **describe(flow, out_name)})


@app.post("/api/template")
def make_template() -> Response:
    """Write the empty shell of a flow, which is what a sync should target."""
    body = request.get_json(silent=True) or {}
    flow, name = resolve(body.get("flow"), "flow")
    out_name = body.get("outName") or f"{flow.name}{rp.TEMPLATE_SUFFIX}"
    return _write_folder_copy(rp.blank(flow), out_name, "writing a template")


@app.post("/api/normalise")
def fix_space_names() -> Response:
    """Rename spaces that carry another landscape's connection id."""
    body = request.get_json(silent=True) or {}
    flow, name = resolve(body.get("flow"), "flow")
    problems = rp.space_name_problems(flow)
    if not problems:
        return jsonify({"ok": True, "written": None, "problems": [],
                        "message": "space names are already canonical"})
    out_name = body.get("outName") or name or f"{flow.name}.tgz"
    return _write_folder_copy(rp.normalise(flow), out_name, "normalising a flow")


@app.post("/api/content")
def content() -> Response:
    """The .replication JSON inside a flow, for reading and copying.

    Exactly the bytes the archive holds, so what someone copies out of here can
    be pasted straight back in — or into a colleague's browser.
    """
    body = request.get_json(silent=True) or {}
    flow, name = resolve(body.get("flow"), "flow")
    text = rp.json.dumps(flow.spec, separators=rp.JSON_SEPARATORS)
    return jsonify({"text": text, "bytes": len(text.encode()),
                    **describe(flow, name)})


@app.get("/api/file/<path:name>")
def download_folder_file(name: str) -> Response:
    """Serve a file from the flows folder as a download."""
    path = flow_path(name)
    if not path.is_file():
        abort(404, f"no such flow in the folder: {name}")
    return Response(path.read_bytes(), mimetype="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{path.name}"',
    })


@app.post("/api/delete")
def delete() -> Response:
    """Remove a flow from the folder.

    A .tgz is moved aside to *.bk rather than unlinked, so a mistaken delete is
    a rename away from being undone. A .bk itself has nowhere further to go, and
    is removed outright — which the caller is told, so the confirmation can say
    which of the two is about to happen.
    """
    only_local("deleting from the flows folder")
    body = request.get_json(silent=True) or {}
    name = body.get("name") or ""
    path = flow_path(name)
    if not path.is_file():
        abort(404, f"no such flow in the folder: {name}")

    if path.name.endswith(rp.BACKUP_SUFFIX):
        path.unlink()
        return jsonify({"ok": True, "removed": path.name, "keptAs": None})
    kept = rp.backup(path)
    path.unlink()
    return jsonify({"ok": True, "removed": path.name,
                    "keptAs": kept.name if kept else None})


@app.get("/api/download/<handle>")
def download(handle: str) -> Response:
    entry = _STORE.get(handle)
    if entry is None:
        abort(404, "that archive is no longer held — generate it again")
    name, data = entry
    return Response(data, mimetype="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "Content-Length": str(len(data)),
    })


# -------------------------------------------------------------------- main ---

def main(argv: Optional[list] = None) -> int:
    global FLOWS_DIR

    ap = argparse.ArgumentParser(
        prog="di-repl-sync-web",
        description="Browser front end for the DI replication-flow sync.")
    server.add_server_args(ap)
    # --dir is what run.sh used to accept itself; it now arrives here as it is
    ap.add_argument("--flows-dir", "--dir", default="flows",
                    help="folder holding the .tgz exports (default: ./flows)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    FLOWS_DIR = Path(args.flows_dir).expanduser().resolve()
    if not FLOWS_DIR.exists() and args.flows_dir == "flows":
        FLOWS_DIR.mkdir(parents=True)           # first run in a fresh checkout
    if not FLOWS_DIR.is_dir():
        print(f"  ! no such folder: {FLOWS_DIR}", file=sys.stderr)
        return 2

    return server.serve(
        app, args,
        lines=[f"  flows folder  {FLOWS_DIR}",
               "  unofficial tool, no warranty — see the About box"],
        network_note="    Shutdown, restart, update and folder writes stay refused "
                     "to them; reading the flows does not.")


if __name__ == "__main__":
    raise SystemExit(main())
