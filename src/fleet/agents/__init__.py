"""Teaching agents what fleet is, on whatever surface they read.

**This is the only part of fleet that writes outside fleet's own directories.** That
sentence governed one module before the split and governs two now -- `docs` writes skill
files and marker-delimited regions in AGENTS.md, `clients` writes MCP config JSON -- so
it lives here, where both are in scope.

The rule it implies is the same in both: never touch a byte we did not write. Files we
fully own are overwritten wholesale; files that belong to the user get a delimited region
or a single key, and nothing else in them is disturbed.

`registry` is the data table the other two read, and `usage` is the prose they install.
Supporting a new agent should be a row in `registry`, not a branch anywhere else.
"""

from __future__ import annotations

from .clients import (apply_mcp, detect_mcp_clients, install_mcp, installed_mcp_clients,
                      mcp_entry, remove_mcp, stale_mcp_clients, uninstall_mcp)
from .docs import (BEGIN, END, Change, apply_block, detect_targets, install,
                   installed_targets, legacy_paths, plan, remove_block, stale_targets,
                   uninstall)
from .registry import (AGENTS, BY_NAME, HERMES_CATEGORY, MCP_CLIENTS, TARGETS, Agent,
                       McpClient, config_command, fleet_command, fleet_executable,
                       package_version)
from .usage import agents_block, dist_skill_text, hermes_skill_text, skill_text, stamp

__all__ = [
    "AGENTS", "BEGIN", "BY_NAME", "END", "HERMES_CATEGORY", "MCP_CLIENTS", "TARGETS",
    "Agent", "Change", "McpClient", "agents_block", "apply_block", "apply_mcp",
    "config_command", "detect_mcp_clients", "detect_targets", "dist_skill_text", "fleet_command",
    "fleet_executable", "hermes_skill_text",
    "install", "install_mcp", "installed_mcp_clients", "installed_targets",
    "legacy_paths", "stale_mcp_clients", "stale_targets", "mcp_entry", "package_version", "plan",
    "remove_block", "remove_mcp", "skill_text", "stamp", "uninstall", "uninstall_mcp",
]
