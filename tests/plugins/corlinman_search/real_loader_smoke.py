"""Run a real managed-server handshake without invoking a search engine.

This script is intentionally outside pytest collection. It needs an available
``free-search-mcp`` dependency and starts a real ``uv`` child through Hermes'
portable-plugin loader. It sends only MCP initialize/list_tools messages; it
never calls the search tool or contacts a search provider.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "corlinman_search"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="hermes-corlinman-search-") as temp_dir:
        home = Path(temp_dir) / "hermes-home"
        home.mkdir()
        (home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - corlinman-search\n", encoding="utf-8"
        )
        os.environ["HERMES_HOME"] = str(home)
        os.environ["HERMES_BUNDLED_PLUGINS"] = str(PLUGIN_ROOT.parent)

        import hermes_cli.plugins as plugins
        import tools.mcp_tool as mcp

        plugins._plugin_manager = None
        try:
            tool_names = mcp.discover_mcp_tools()
            [status] = mcp.get_mcp_status()
            [search_tool] = mcp._servers[status["name"]]._tools
            input_schema = search_tool.input_schema
        finally:
            mcp.shutdown_mcp_servers()

    assert len(tool_names) == 1, tool_names
    assert tool_names[0].endswith("__search"), tool_names
    assert status["connected"] is True, status
    assert status["tools"] == 1, status
    assert {"query", "max_results"}.issubset(input_schema["required"])
    max_results_schema = input_schema["properties"]["max_results"]
    assert max_results_schema["type"] == "integer"
    assert max_results_schema["minimum"] == 1
    assert max_results_schema["maximum"] == 5
    print(
        json.dumps(
            {
                "server": status["name"],
                "tool_names": tool_names,
                "schema": {
                    "required": input_schema["required"],
                    "max_results": max_results_schema,
                },
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
