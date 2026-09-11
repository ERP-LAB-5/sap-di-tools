# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
Synthetic replication flows for the test suite.

Nothing here comes from a real system. The shapes are what matter, and they are
the ones that turned up in practice:

* two landscapes whose connections differ but whose table scope should not;
* a sparse ``truncate`` flag, present on a few tasks and never ``false``;
* a flow whose target space still carries the *other* landscape's connection id
  in its name, the signature of a migration done by hand;
* an empty shell with connections but no tasks.

Archives are built with :mod:`tarfile` rather than with the module under test,
so the format assertions compare two independent implementations.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from typing import Dict, Iterable, List, Optional

import pytest

from di_replication_sync import replication as r

ACC = dict(flow="ACC100_DEMO_FLOW", abap="SRC_ABAP_ACC", store="OBJ_STORE_ACC",
           container="/DEMO/ACC")
PRD = dict(flow="PRD200_DEMO_FLOW", abap="SRC_ABAP_PRD", store="OBJ_STORE_PRD",
           container="/DEMO/PRD")

TABLES = ["DEMO_ALPHA", "DEMO_BETA", "DEMO_GAMMA", "DEMO_DELTA", "ZZ_CUSTOM_ONE"]

# Opaque per-task suffixes, as DI generates them. Fixed here so a rename is
# visible in an assertion.
SUFFIXES = {"DEMO_ALPHA": "aa11bb", "DEMO_BETA": "cc22dd", "DEMO_GAMMA": "ee33ff",
            "DEMO_DELTA": "gg44hh", "ZZ_CUSTOM_ONE": "ii55jj"}

DATASET_PROPERTIES = {"compression": "SNAPPY", "format": "PARQUET",
                      "groupDeltaFilesBy": "HOUR",
                      "objectstore.write.useDuplicateSuppressionInitialLoad": "false"}


def spec(landscape: Dict[str, str], tables: Iterable[str] = TABLES,
         truncate: Iterable[str] = (), target_space_name: Optional[str] = None,
         description: Optional[str] = None) -> Dict:
    """A replication-flow spec, in DI's key order.

    ``target_space_name`` overrides the canonical name, which is how a
    hand-migrated flow is reproduced.
    """
    flow = landscape["flow"]
    src_space = f"{flow}_{landscape['abap']}_src"
    tgt_space = target_space_name or f"{flow}_{landscape['store']}_tgt"
    truncate = set(truncate)

    out: Dict = {"name": flow}
    if description is not None:
        out["description"] = description
    out["version"] = r.ONE_TO_ONE
    out["sourceSpaces"] = [{
        "name": src_space, "connectionId": landscape["abap"], "connectionType": "ABAP",
        "technicalName": landscape["abap"], "ccmConnectionId": landscape["abap"],
        "ccmConnectionType": "ABAP", "container": landscape["container"]}]
    out["targetSpaces"] = [{
        "name": tgt_space, "connectionId": landscape["store"], "connectionType": "S3",
        "technicalName": landscape["store"], "ccmConnectionId": landscape["store"],
        "ccmConnectionType": "S3", "container": "/",
        "datasetProperties": dict(DATASET_PROPERTIES)}]

    tasks: List[Dict] = []
    for table in tables:
        task = {"name": f"{flow}_{table}_{SUFFIXES.get(table, 'zz00zz')}",
                "sourceDataset": table, "sourceSpace": src_space,
                "targetDataset": table, "targetSpace": tgt_space,
                "loadType": "REPLICATE"}
        if table in truncate:
            task["truncate"] = True
        tasks.append(task)
    out["oneSourceOneTargetTasks"] = tasks
    return out


def archive(spec_dict: Dict, mtime: int = 1_700_000_000) -> bytes:
    """Pack a spec the way DI does, built with tarfile for independence."""
    payload = json.dumps(spec_dict, separators=r.JSON_SEPARATORS).encode()

    info = tarfile.TarInfo(name=f"{spec_dict['name']}{r.SUFFIX}")
    info.size = len(payload)
    info.mode = 0o644
    info.uid = info.gid = 999
    info.uname = ""
    info.gname = "systemd-journal"
    info.mtime = mtime

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        tar.addfile(info, io.BytesIO(payload))
    # tarfile pads to its 10 KiB record size on close; DI's archives stop after
    # the two zero blocks, so trim back to that.
    raw = bytearray(buf.getvalue())
    blocks = r.BLOCK + -(-len(payload) // r.BLOCK) * r.BLOCK + 2 * r.BLOCK
    del raw[blocks:]

    # The one field where tarfile and DI disagree: tarfile leaves devmajor and
    # devminor as NULs for a regular file, DI writes octal zeros. Matching DI
    # here is the point of the fixture, so patch them and redo the checksum.
    raw[329:345] = b"0000000\x000000000\x00"
    raw[148:156] = b" " * 8
    raw[148:156] = ("%06o" % sum(raw[:r.BLOCK])).encode("ascii") + b"\0 "

    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(bytes(raw))
    return out.getvalue()


def flow(*args, **kwargs) -> r.Flow:
    return r.read_flow(archive(spec(*args, **kwargs)), origin="<fixture>")


# ---------------------------------------------------------------- fixtures ---

@pytest.fixture
def acc():
    """Acceptance: the full scope, with truncate on one table."""
    return flow(ACC, truncate={"DEMO_BETA"})


@pytest.fixture
def prd_template():
    """Production: connections configured, no tasks yet, empty description."""
    return flow(PRD, tables=[], description="")


@pytest.fixture
def prd_deployed():
    """Production: same scope as acceptance, plus truncate on every task.

    This is the shape that raised the original question — identical scope, yet
    every task differs, because the runtime wrote a flag back on deployment.
    """
    return flow(PRD, truncate=TABLES)


@pytest.fixture
def prd_hand_migrated():
    """Production, migrated by hand: right connection, wrong space name."""
    return flow(PRD, target_space_name=f"{PRD['flow']}_{ACC['store']}_tgt")


@pytest.fixture
def migrated(acc, prd_template):
    return r.sync(acc, prd_template)
