# Corlinman Search Capability Migration

## Decision

Use the bundled portable plugin at `plugins/corlinman_search/`, enabled through
Hermes' existing Agent Plugins v1 loader. It runs one Hermes-owned stdio MCP
server. The server is a narrow adapter over `free-search-mcp`; it does not
restore Corlinman's gateway process or depend on an already-running
`free-search-mcp` process.

This is intentionally not Hermes' generic `web_search` provider: that provider
has a different response contract and does not own an MCP subprocess lifecycle.

## Source Contract

| Concern | Corlinman source evidence | Target contract |
| --- | --- | --- |
| Lifecycle | `python/packages/corlinman-server/src/corlinman_server/gateway/lifecycle/bundled_mcp.py:14-33, 265-365` seeds a managed server and preserves operator deletion; `web_search_freesearch.py:67-119, 150-155` lazily creates, reuses, and closes its own manager. | Hermes launches and reaps the stdio child through `tools/mcp_tool.py`; only its live registry entry is a capability. A residual OS process is never treated as healthy. |
| Transport and launch | `bundled_mcp.py:90-109` specifies stdio, `uvx`, `free-search-mcp>=0.8.0`, 180 s handshake, 120 s call ceiling, and a workspace download path. | The portable manifest resolves to `uv run --isolated --with free-search-mcp>=0.8.0,<1 python server.py`; Hermes' stdio watchdog owns the process group. The adapter has a 20 s per-search deadline. |
| Tool and parameters | `web_search_freesearch.py:121-145` calls upstream `search` with `query`, `max_results`, and `format: json`. `corlinman_agent/web/search.py:70-75, 134-155` exposes the stable builtin search name and result ceiling. | One target tool only: `mcp__agent_plugin_corlinman_search_<hash>__search(query, max_results)`. The adapter suppresses the SDK's default empty resource/prompt capabilities, so Hermes does not generate unrelated utility tools. `max_results` is required to be 1..5; output is always structured JSON, so no caller-controlled format is exposed. |
| Output | `web_search_freesearch.py:157-186` consumes `{query, engines, results[]}` with `title`, `url`, and `snippet`; `corlinman_agent/web/search.py:30-38` defines success and degraded envelopes. | `{content_warning, engines, results[{title,url,snippet}], truncated?, safety_filtered?}`. No query echo, no unbounded backend diagnostics, at most five rows, and at most 8192 output characters. |
| Timeout and errors | `web_search_freesearch.py:54-64, 128-148` has a 240 s cold-start bound, delegates call timing to the MCP spec, and raises an explicit tool failure; `main.py:409-426` makes registration failure non-fatal. | A timeout or missing/broken backend returns MCP `isError`, so Hermes continues the main turn with a clear unavailable/timeout tool result. Empty results accompanied by backend errors are unavailable, never reported as a successful no-match. |
| Credentials and privacy | `bundled_mcp.py:60-67` pins the security floor because model-selected URLs reach the server. The original bundle has no API credential field. | No credential is configured or logged by this plugin. Queries leave the machine for public search engines; its profile-scoped cache is under `${PLUGIN_DATA}`. The stdio environment is Hermes-filtered and only explicit non-secret settings are passed. Do not place proxy URLs, API keys, or user-sensitive query logging in this plugin config. |

The upstream package's larger tool surface (`fetch`, downloads, documents,
research, and resources) is deliberately not exposed. The adapter invokes only
its library search aggregator and disables downloads.

## Safety Boundaries

- The adapter applies a 20 s deadline, limits the query to 512 characters,
  result count to five, fields to fixed sizes, and output to 8192 characters.
- URL schemes other than `http` and `https` are discarded. Control characters
  and common role/prompt-injection constructions are removed; all surviving
  snippets are labeled untrusted data.
- Upstream dependency errors and engine-wide errors do not include raw backend
  details in model-visible output. The plugin itself logs neither queries nor
  result text.
- `SEARCH_MCP_ALLOW_PRIVATE_HOSTS=false`, `SEARCH_MCP_DOWNLOAD_ENABLED=false`,
  and `SEARCH_MCP_FETCH_STRATEGY=http` reduce SSRF, local-write, and browser
  exposure. The upstream package remains responsible for outbound search-engine
  requests and its own URL-validation implementation.

## Deployment Prerequisites

1. Install `uv` on the Hermes host. The first managed launch resolves
   `free-search-mcp>=0.8.0,<1`; it may need package-index access and therefore
   must be reviewed in the deployment window, not during a user turn.
2. Enable the plugin in the target profile's `config.yaml`:

```yaml
plugins:
  enabled:
    - corlinman-search
```

3. Restart Hermes or reload its MCP discovery. The loader derives a
   profile-specific server name and tool prefix; do not configure a second raw
   `uvx free-search-mcp` entry beside it.
4. Verify with `hermes mcp list` and the MCP health surface that the derived
   server is `connected` and exposes exactly one `search` tool. A failed status
   is a deployment failure, not a usable fallback.

## Offline Verification

```bash
scripts/run_tests.sh tests/plugins/corlinman_search/test_bridge.py -q
.venv/bin/ruff check plugins/corlinman_search tests/plugins/corlinman_search
.venv/bin/ruff format --check plugins/corlinman_search tests/plugins/corlinman_search
git diff --check
```

Run the real managed-server smoke separately when the deployment dependency is
available. It starts the exact `uv run --isolated --with
free-search-mcp>=0.8.0,<1` child generated from `mcp.json`, then performs only
the MCP initialize and `list_tools` handshake. It does not call `search` or
contact a search provider:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/plugins/corlinman_search/real_loader_smoke.py
```

The tests use the real portable-plugin parser and native MCP registration path,
but replace the stdio server and search backend with fakes. They cover tool
discovery and health, missing configuration, timeout, subprocess non-zero exit,
backend unavailability, and malicious or oversized result payloads. No test
contacts a search engine or starts `uv`.

## Remaining Risk

- Search quality and anti-bot behavior remain upstream/network-dependent. A
  connected MCP transport only proves availability, not factual correctness.
- The adapter's instruction filtering is a defense in depth measure, not a
  proof that arbitrary web text is harmless. Responses remain explicitly marked
  untrusted and should be corroborated with their URLs.
- The `uv` resolver fetches code on first deployment. Pinning the compatible
  major range gives security updates within the supported line; an environment
  with stricter supply-chain policy should prebuild and approve the dependency
  cache before enabling this profile.
- `MCPServer` currently has no public API to omit its default empty
  resource/prompt handlers. The adapter removes those handlers before its
  first initialize response so Hermes exposes only `search`; re-run the real
  smoke after updating `free-search-mcp` or its MCP SDK dependency.
