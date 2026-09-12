# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""Sync SAP Data Intelligence replication flows between landscapes.

    replication  the engine and the di-repl-sync command line, standard library only
    app          the browser front end (Flask), on top of core.server
    core/        shared with every D-LAB-5 tool; `copier update` maintains it
"""

from .core.identity import REPO_URL  # noqa: F401
from .core.version import __version__  # noqa: F401
