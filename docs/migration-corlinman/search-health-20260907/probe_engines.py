import asyncio,importlib.metadata,json,os,tempfile,time
with tempfile.TemporaryDirectory(prefix='hermes-engine-health-') as temporary:
 os.environ.update(SEARCH_MCP_CACHE_DIR=temporary,SEARCH_MCP_REQUEST_TIMEOUT='8',SEARCH_MCP_RESCUE_TIMEOUT='6',SEARCH_MCP_RESCUE_ENABLED='false',SEARCH_MCP_FETCH_STRATEGY='http',SEARCH_MCP_DOWNLOAD_ENABLED='false',SEARCH_MCP_DEFAULT_ENGINES='["baidu","anysearch","duckduckgo","mojeek","googlenews"]')
 from search_mcp.aggregator import aggregate_search
 from search_mcp.browser import pool
 from search_mcp.cache import cache
 async def main():
  print(json.dumps({'version':importlib.metadata.version('free-search-mcp'),'rescue_enabled':False,'fresh_cache':True}),flush=True)
  sem=asyncio.Semaphore(2)
  async def one(engine,query):
   async with sem:
    start=time.monotonic()
    try:
     data=await asyncio.wait_for(aggregate_search(query,engines=[engine],max_results=3,use_cache=False),25)
     print(json.dumps({'engine':engine,'query':query,'seconds':round(time.monotonic()-start,2),'payload':data},ensure_ascii=False),flush=True)
    except Exception as e:print(json.dumps({'engine':engine,'query':query,'seconds':round(time.monotonic()-start,2),'error_type':type(e).__name__,'error':str(e)[:400]},ensure_ascii=False),flush=True)
  await asyncio.gather(*(one(e,q) for e in ['baidu','anysearch','duckduckgo','mojeek','googlenews'] for q in ['Python official documentation tutorial','骑士学院2 官网']))
  await pool.shutdown()
  await cache.close()
 asyncio.run(main())
