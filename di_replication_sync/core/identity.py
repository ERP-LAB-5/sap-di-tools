# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 D-LAB-5
"""
identity.py — who this tool is, as far as the core needs to know.

Generated from .copier-answers.yml by the D-LAB-5 tool template. Change the
answer and run `copier update` rather than editing this file: it is core-owned,
and every other module under core/ reads its names from here so that those
modules come out byte-identical in every tool.
"""

TOOL_NAME = "di-replication-sync"
PACKAGE = "di_replication_sync"
TITLE = "DI replication sync"
DESCRIPTION = "Compare and promote SAP Data Intelligence replication flows between landscapes"
AUTHOR = "D-LAB-5"
COPYRIGHT = "© 2026 D-LAB-5"
LICENCE = "GPL-3.0-or-later"
LICENCE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"
DISCLAIMER = "Unofficial. Not affiliated with, endorsed by or supported by SAP. Provided as is, without warranty of any kind \u2014 use at your own risk, and check what it produces before uploading it anywhere."
COFFEE_URL = "https://www.buymeacoffee.com/dlab5"

GITHUB_ORG = "ERP-LAB-5"
REPO_NAME = "sap-di-tools"
REPO_URL = f"https://github.com/{GITHUB_ORG}/{REPO_NAME}"
DEFAULT_BRANCH = "main"

DEFAULT_PORT = 8766

CLI_COMMAND = "di-repl-sync"
CLI_MODULE = "replication"
WEB_COMMAND = "di-repl-sync-web"
MCP_COMMAND = "di-repl-sync-mcp"
SKILL_COMMAND = "di-repl-sync-skill"
SKILL_NAME = "di-replication-sync"

WITH_MCP = False
WITH_WORKSPACE = False
WITH_PLUGIN = True
WORKSPACE_DIR = ""
SHARED_DIR = ""
