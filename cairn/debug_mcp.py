import asyncio
import traceback
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    try:
        async with streamablehttp_client("http://localhost:8000/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("YHTEYS ONNISTUI")
    except Exception:
        traceback.print_exc()

asyncio.run(main())