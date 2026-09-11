#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
skill_install.py — put the di-replication-sync skill where an agent will find it.

The skill travels inside the package, so a pip or pipx install already has it;
what it does not have is a copy under ~/.claude/skills, which is where Claude
Code looks. This command bridges that.

    di-repl-sync-skill                  # where the packaged copy lives
    di-repl-sync-skill --install        # copy it to ~/.claude/skills/<name>
    di-repl-sync-skill --install --force
    di-repl-sync-skill --print          # write it to stdout
    di-repl-sync-skill --check FILE     # has another copy drifted from this one?

There are two copies in the repository — the packaged one and the checkout's own
.claude/skills/<name>/SKILL.md, which has to be a real file because a symlink
does not survive a clone on Windows. --check is how that pair is kept honest,
and the test suite runs it.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

SKILL_NAME = "di-replication-sync"


def packaged() -> Path:
    """The SKILL.md that shipped with this install."""
    return Path(__file__).resolve().parent / "skill" / "SKILL.md"


def destination() -> Path:
    return Path.home() / ".claude" / "skills" / SKILL_NAME / "SKILL.md"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--install", action="store_true",
                    help="copy the skill into ~/.claude/skills")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a skill that is already installed")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="write the skill to stdout")
    ap.add_argument("--check", metavar="FILE",
                    help="compare another copy against the packaged one")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    src = packaged()
    if not src.exists():
        print(f"  ! the packaged skill is missing: {src}", file=sys.stderr)
        return 1

    if args.check:
        other = Path(args.check)
        if not other.exists():
            print(f"  ! no such file: {other}", file=sys.stderr)
            return 2
        if filecmp.cmp(src, other, shallow=False):
            print(f"  in step with {src}")
            return 0
        print(f"  ! {other} has drifted from {src}", file=sys.stderr)
        return 1

    if args.show:
        sys.stdout.write(src.read_text(encoding="utf-8"))
        return 0

    if not args.install:
        print(src)
        return 0

    dest = destination()
    if dest.exists() and not args.force:
        same = filecmp.cmp(src, dest, shallow=False)
        print(f"  {dest} already exists"
              + (" and matches — nothing to do" if same
                 else " and differs — pass --force to replace it"))
        return 0 if same else 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    print(f"  installed to {dest}")
    print("  start a new session for an agent to pick it up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
