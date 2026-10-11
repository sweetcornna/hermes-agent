"""Diagnose public-search relevance without involving the model or chat."""
import asyncio
import json
import tempfile
import os

with tempfile.TemporaryDirectory(prefix='search-engine-probe-') as cache:
    os.environ['SEARCH_MCP_CACHE_DIR'] = cache
    os.environ['SEARCH_MCP_REQUEST_TIMEOUT'] = '8'
    os.environ['SEARCH_MCP_RESCUE_ENABLED'] = 'false'
    os.environ['SEARCH_MCP_FETCH_STRATEGY'] = 'http'
    from search_mcp.aggregator import aggregate_search
    async def main():
        for query, engines in [
            ('骑士学院2 官网', ['baidu', 'anysearch']),
            ('Knights College 2 kaiju09', ['duckduckgo', 'mojeek', 'googlenews', 'bing']),
            ('Python 官方 文档', ['baidu', 'bing']),
        ]:
            try:
                data = await asyncio.wait_for(aggregate_search(query, engines=engines, max_results=5, use_cache=False), 25)
                print(json.dumps({'query': query, 'engines': engines, 'result': data}, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(json.dumps({'query': query, 'error_type': type(exc).__name__}), flush=True)
    asyncio.run(main())
