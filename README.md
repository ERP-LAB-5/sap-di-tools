# sap-di-tools

Unofficial tools for SAP Data Intelligence Replication Management Service (RMS)
flows.

> **Unofficial. Provided as is.** Not affiliated with, endorsed by or supported
> by SAP. There is **no warranty of any kind** — see sections 15–17 of the
> [licence](LICENSE). Use at your own risk, on systems you are authorised to
> work with, and check what these tools produce before uploading it anywhere.
> Nothing here connects to a DI tenant; every result is a file you review and
> upload yourself.

Currently one tool.

---

## di-replication-sync

Compare two replication-flow exports and promote the table scope of one onto the
other, without disturbing the target's connections.

An RMS replication flow exports as a `.tgz` holding one file,
`<FLOW>.replication`: compact JSON with the flow name, one source space, one
target space, and one task per replicated table. Promoting a flow between
landscapes means moving the *table scope* — acceptance reads a different ABAP
client and writes a different object store than production, and those
differences are the point.

Done by hand it goes wrong quietly, which is what this exists to prevent.

### Three ideas worth knowing

**A gap check is not a diff.** `loadType` and `targetDataset` say what is
replicated; a difference there is a gap between two landscapes. `truncate` is
different — it is a *deployment* setting, telling the flow to clear the target of
previously extracted content before it loads, so an object-store prefix is
emptied rather than a re-initialisation landing beside stale files. It is set at
deployment rather than in the modeller, appears only as `true` (DI omits the key
when it is off), and two landscapes can legitimately differ on it while
replicating identical tables. So it is reported and can be *set* on a sync, but
never counted as a gap. Without that split, two landscapes replicating exactly
the same tables look like they differ on every single task.

**Space names should match the connection they use.** DI names a space
`<flow>_<connectionId>_src` or `_tgt`. A flow migrated by replacing the name
prefix keeps the *old* landscape's connection id in that name while pointing at
the correct connection — invisible in the UI, and misleading to the next person
who reads the export. `normalise` fixes the name and every task that references
it, touching nothing else.

**Always start from an empty template.** A sync blanks the target's task list
before filling it, so no table from a previous scope can survive one. Extract
that shell once and keep it as `<FLOW>.tgz.template`.

### Use it

```bash
git clone https://github.com/ERP-LAB-5/sap-di-tools
cd sap-di-tools
./run.sh                       # http://127.0.0.1:8766
```

`run.sh` creates the virtualenv on first run. The browser opens on a home screen
with two ways in:

- **Explore** — everything in the flows folder. Open a flow to read its tables
  and connections, copy the JSON out, download it, or remove it. Deleting a
  `.tgz` keeps it as `.bk`, so it is a rename away from being undone.
- **Sync wizard** — *Pick the two flows → Gap check → Confirm and generate →
  Result*, ending on the generated file with copy and download to hand.

```bash
./run.sh --port 9000 --dir ~/exports
./run.sh --stop
```

Same thing without a browser:

```bash
di-repl-sync check     SOURCE.tgz TARGET.tgz          # exits 1 on a gap
di-repl-sync template  TARGET.tgz --out TARGET.tgz.template
di-repl-sync apply     --source SOURCE.tgz --target TARGET.tgz.template \
                       --out RESULT.tgz \
                       [--state copy|on|off] [--properties source|target] \
                       [--no-backup]
di-repl-sync normalise FLOW.tgz --out FLOW.tgz
```

`check` is CI-friendly: zero when the two landscapes replicate the same tables,
one when they do not. `apply` prints the gap check and asks before writing;
`--yes` skips the prompt.

### What a sync touches

| | |
|---|---|
| **Replaced** | `oneSourceOneTargetTasks` — emptied, then refilled from the source |
| **Kept from the target** | flow `name`, `description`, `version` |
| **Normalised** | both space names, to `<flow>_<connectionId>_src` / `_tgt` |
| **Rewritten per task** | `sourceSpace`, `targetSpace`, and the flow prefix in `name` |
| **Copied verbatim per task** | `sourceDataset`, `targetDataset`, `loadType` |
| **Copied, or set flow-wide** | `truncate`, per `--state` |
| **Copied by default** | `datasetProperties` — format, compression, delta grouping — per `--properties` |
| **Never copied** | `connectionId`, `connectionType`, `container` — the target keeps its own, always |

The last two rows are the split that matters: *how* the flow writes travels with
it, *where* it writes belongs to the landscape.

Task names keep the opaque suffix DI gave them (`..._SOMETABLE_qve0cf`), which
makes a sync deterministic: run it twice and you get the same bytes, so a re-sync
reads as an empty gap rather than every task renamed.

`--state` decides what happens to `truncate`. It is normally a property of the
landscape rather than of individual tables, so the two flow-wide settings are the
usual answer:

| | applies to | when |
|---|---|---|
| `on` | every task | The target landscape always clears before loading. |
| `off` | every task | A flow that must never clear, or one not yet deployed. |
| `copy` (default) | per task, from the source | The source's task-by-task settings are themselves the intent. |

Overwriting a flow keeps the previous file as `<name>.tgz.bk`, byte for byte.

### When the export is not a file you have

A flow can be handed over as the JSON inside the archive rather than the archive
itself. **Paste JSON…** on either panel of the wizard takes it directly, and any
flow can be opened and copied out the same way. To get that text from an
archive: `tar -xzOf FLOW.tgz`.

### Where the flows live

`./flows` by default, and it is git-ignored. A replication-flow export carries
system ids, connection names, bucket names and the complete table list of
whoever produced it. Point the tool elsewhere with `--dir`.

### Archive format

Regenerated archives are byte-identical to DI's own, tar for tar:

- one USTAR member `<FLOW>.replication`, mode `0644`, uid/gid `999`,
  group `systemd-journal`, no PAX header
- payload is JSON with `,`/`:` separators and no trailing newline
- gzip with no stored filename, `mtime` 0, `XFL` 0

The deflate bytes differ, because DI does not compress with zlib. The tests
compare the tar inside, which is the only part DI reads back.

### Driving it from an agent

A Claude Code skill ships inside the package.

```bash
di-repl-sync-skill --install       # copies it into ~/.claude/skills
di-repl-sync-skill --print         # to stdout
```

Working in a clone, `.claude/skills/di-replication-sync/SKILL.md` is already
there and picked up automatically.

### Tests

```bash
./test.sh
```

The suite runs on synthetic flows built in `tests/conftest.py` — no real system
is needed and none is referenced. `tests/test_local_flows.py` additionally checks
every export it finds in `./flows` and skips when the folder is empty.

> `test.sh` and `run.sh` both `unset PYTHONPATH` first. A workstation with ROS 2
> installed puts `/opt/ros` on `PYTHONPATH` for every shell, which shadows the
> venv and registers pytest plugins that cannot import here.

### Layout

```
di_replication_sync/replication.py    read / gap check / normalise / blank / sync / verify, plus the CLI
di_replication_sync/app.py            Flask front end — moves flows, decides nothing
di_replication_sync/templates,static  the page
di_replication_sync/skill/            the agent skill
tests/                                synthetic fixtures, plus optional checks against ./flows
flows/                                runtime exports, git-ignored
```

`replication.py` is standard library only, so it is usable as a library and a
CLI without Flask.

---

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE).

SAP, SAP Data Intelligence and other SAP product names are trademarks of SAP SE.
This project is not affiliated with SAP SE.
