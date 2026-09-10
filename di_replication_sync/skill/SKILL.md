---
name: di-replication-sync
description: Compare and promote SAP Data Intelligence replication flows between landscapes — acceptance to production, sandbox to acceptance — from their .tgz exports. Runs a gap check that separates table scope from deployment settings, derives an empty .tgz.template shell from any flow, syncs a source's table scope into a target while keeping the target's own connections, normalises space names left wrong by a hand migration, and verifies the result before writing. Use whenever someone asks to compare two replication flows, check whether two landscapes replicate the same tables, migrate or promote a flow, fill an empty flow that exists only as configuration, or work with a *.replication export. Triggers on "replication flow", "DI flow", "promote the flow", "are these two flows in sync", "gap check between the systems", "migrate the tables to production", "*_DML.tgz", ".replication", "di-repl-sync", or an ask to edit a flow export by hand.
---

# DI replication sync

A SAP Data Intelligence replication flow exports as a `.tgz` holding exactly one
file, `<FLOW>.replication`: compact JSON with the flow name, one source space,
one target space, and one task per replicated table.

**Never edit that JSON by hand, and never repack the archive with `tar czf`.**
Both are how the defects this tool exists to catch get introduced. Use the CLI.

## The one thing to understand first

Promoting a flow between landscapes moves the **table scope** and nothing else.
The connections differ *by design* — acceptance reads a different ABAP client and
writes a different object store than production — so the target keeps its own
identity and receives only the source's list of tables.

What travels and what does not:

| | |
|---|---|
| **Moves with the flow** | the table scope, and the write settings under `datasetProperties` — format, compression, delta grouping. These describe the data, so the same flow should write the same way wherever it runs. |
| **Stays with the landscape** | `connectionId`, `connectionType`, `container`, on both spaces. Never copy these, however different they look. A differing connection id is correct, not a finding. |
| **Rewritten** | the two space names, into `<flow>_<connectionId>_src` / `_tgt`. |

Three consequences, and they are the whole skill:

1. **A gap is about scope, not about every field.** `loadType` and
   `targetDataset` say what is replicated. `truncate` is a *deployment* setting —
   it clears the target of previously extracted content before the flow loads, so
   on an object store the prefix is emptied rather than a re-initialisation
   landing beside stale files. It is set at deployment, not in the modeller, and
   two landscapes can legitimately differ on it while replicating identical
   tables. `check` therefore ignores it. Report it, never call it a gap.

2. **Space names must match the connection they use.** DI names a space
   `<flow>_<connectionId>_src` or `_tgt`. A flow migrated by replacing the name
   prefix keeps the *previous* landscape's connection id inside that name while
   pointing at the correct connection — invisible in the DI UI, and the clearest
   sign a migration was done by hand. `normalise` fixes it.

3. **A sync always starts from an empty shell.** It blanks the target's task list
   before filling it, so no table from a previous scope can survive one. Keep
   that shell as `<FLOW>.tgz.template`.

## Commands

```bash
di-repl-sync check     SOURCE.tgz TARGET.tgz            # exit 0 = no gap, 1 = gap
di-repl-sync template  FLOW.tgz --out FLOW.tgz.template
di-repl-sync normalise FLOW.tgz --out FLOW.tgz
di-repl-sync apply     --source SOURCE.tgz \
                       --target TARGET.tgz.template \
                       --out RESULT.tgz \
                       [--state copy|on|off] [--properties source|target] \
                       [--no-backup]
```

From a checkout, each is `python3 -m di_replication_sync.replication <command>`.

`apply`, `template` and `normalise` print what they will do and **prompt before
writing**. Running unattended, pass `--yes` — but only after showing the user the
`check` output and getting a decision, because `apply` overwrites its `--out` and
replaces every task in the target.

Overwriting keeps the previous file as `<name>.tgz.bk`, byte for byte, so a sync
is undoable with a `mv`. Say where the backup went. `--no-backup` turns it off;
do not pass it unless asked.

`check` is the one to reach for first, and the only one safe to run without
asking. Its exit code is the answer: 0 when both landscapes replicate the same
tables, 1 when they do not.

## The migration, end to end

```bash
# 1. What is the gap? Always start here, and show the user the output.
di-repl-sync check flows/ACC_FLOW.tgz flows/PRD_FLOW.tgz

# 2. Take the target's empty shell, once. Keep it in the repo's flows folder.
di-repl-sync template flows/PRD_FLOW.tgz --out flows/PRD_FLOW.tgz.template

# 3. Fill it. --state is a decision for the user, see below.
di-repl-sync apply --source flows/ACC_FLOW.tgz \
                   --target flows/PRD_FLOW.tgz.template \
                   --out    flows/PRD_FLOW.tgz --state on

# 4. Prove it: no gap left, and the result is a clean re-sync of itself.
di-repl-sync check flows/ACC_FLOW.tgz flows/PRD_FLOW.tgz
```

Step 4 exiting 0 is the acceptance criterion. `apply` also runs `verify` before
writing anything and refuses to produce a file that fails it, so a written file
has already passed: table set matches the source, every task points at a space
the flow declares, task names carry the target's prefix, space names are
canonical, and **no identifier from the source landscape survives anywhere in the
payload**.

The generated `.tgz` is then **uploaded to the tenant by hand**. Nothing in this
tool talks to a DI system, and it should stay that way — do not offer to.

## Choosing `--state`

`truncate` is normally a property of the *landscape*, not of individual tables,
so the two flow-wide settings are the usual answer:

| | applies to | when |
|---|---|---|
| `on` | every task | The target landscape always clears before loading. |
| `off` | every task | A flow that must never clear, or one not yet deployed. |
| `copy` (default) | per task, from the source | The source's task-by-task settings are themselves the intent. |

`copy` is the default because it is the one that invents nothing, but it is
rarely what someone means when they say "our systems have truncate on" — that is
`--state on`.

`--properties` is the matching switch for the write settings: `source` (the
default) moves them with the flow, `target` leaves the target's alone. Reach for
`target` only when a landscape deliberately writes in its own format.

This is a **decision about live data**: `truncate` on means an object-store prefix
gets emptied when the flow loads. Ask the user which they want rather than
inferring it from the source, and say how many tasks it will affect.

## Reading a check

```
  source  ACC_FLOW  (83 tables)  flows/ACC_FLOW.tgz
  target  PRD_FLOW  (83 tables)  flows/PRD_FLOW.tgz

  no gap — the same 83 tables on both sides
           83 task(s) differ on truncate, which is deployment state, not scope
```

That is a **pass**. Do not "fix" it. Two landscapes replicating identical tables
while differing on `truncate` is normal and expected.

```
  GAP: 74 to add · 0 to remove · 0 changed · 0 in common
    + SOME_TABLE
    ...
  target needs normalising:
    ! target space is named 'PRD_FLOW_STORE_ACC_tgt' but connects to
      'STORE_PRD'; DI's convention is 'PRD_FLOW_STORE_PRD_tgt'
```

The `+`/`-`/`~` lines are the real gap. A **target needs normalising** block is a
separate problem: it is fixed automatically by an `apply` (a sync normalises on
the way through), or on its own with `normalise` when the scope is already right.

The connection lines below a check show both landscapes side by side. Most of
them differ, and that is correct — only the space name is ever rewritten. Never
report a differing connection id or container as something the sync will fix.

## When the export is not a file you have

A flow can be handed over as the JSON inside the archive rather than the archive
itself — copied out of a system there is no download from, or pasted into a
message. The browser front end takes it directly: **Paste JSON…** on either
panel, or just paste into the panel. `read_spec()` is the same entry point in
the library.

To produce that text from an archive: `tar -xzOf FLOW.tgz`.

## The browser front end

```bash
./run.sh                    # http://127.0.0.1:8766
./run.sh --dir ~/exports    # a different folder of exports
./run.sh --stop
```

Same three stages — pick, gap check, confirm — over the same code, plus upload,
drag-and-drop and paste for exports that are not on the machine running it. Offer it when
someone wants to *look* at a comparison rather than act on one, or when the
`--state` decision needs a human. Both front ends read the same folder, so a file
written by the CLI shows up on a Reload.

## Where the files live

`./flows` by default, and it is **git-ignored on purpose**: an export carries
system ids, connection names, bucket names and the full table list of whoever
produced it. Never commit one, never paste one into an issue, and never put a
real flow name, SID, client or connection id into the repository — not in code,
tests, comments or documentation. The test suite runs entirely on synthetic
flows for exactly this reason.

## Judgement

- **Run `check` before anything else, and show the user its output.** Every other
  command changes a file; this one is free and answers the actual question.
- **Never hand-edit a `.replication` file.** Reach for `apply`, `template` or
  `normalise`. If none of them does what is needed, the tool is missing a
  feature — say so rather than opening the JSON.
- **`truncate` differing is not a finding.** Reporting it as one is the single
  most likely way to mislead someone reading a comparison.
- **Prefer a template as the `apply` target.** Targeting a populated flow works
  and discards its tasks, which is correct but worth saying out loud first.
- **A sync is deterministic.** Task names keep DI's opaque suffix, so running it
  twice gives identical bytes. If a re-run produces a different file, something
  is wrong — investigate rather than shipping it.
- **Do not compare gzip bytes** to decide whether two archives match. DI does not
  compress with zlib, so a regenerated archive differs there while the tar inside
  is identical. Compare with `check`, or `tar -xzOf`.
- **The upload is the user's.** Finish by telling them which file to upload and
  to which system; never imply the change is live.
