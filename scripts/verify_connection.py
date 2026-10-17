import asyncio
import json
from pathlib import Path
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_server import load_credentials


async def main():
    credentials = load_credentials()
    headers = {'Authorization': 'Bearer ' + credentials['mcp_token']}
    url = 'http://127.0.0.1:8085/mcp'
    async with httpx.AsyncClient() as http:
        assert (await http.post(url, json={})).status_code == 401
        assert (await http.post(url, json={}, headers={**headers, 'Origin': 'https://untrusted.example'})).status_code == 403
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert not any(t.name in {'delete_channel', 'delete_role', 'delete_message', 'delete_invite', 'send_slash_command'} for t in tools)
            denied = await session.call_tool('delete_channel', {'channel_id': '0'})
            assert denied.isError
            user = await session.call_tool('get_user_info', {})
            guilds = await session.call_tool('list_guilds', {})
            assert not user.isError and not guilds.isError
            guild_rows = [line for c in guilds.content if c.type == 'text' for line in c.text.splitlines() if line.strip()]
            print(json.dumps({'tool_count': len(tools), 'authenticated': True, 'deletion_blocked': True,
                              'user': [c.text for c in user.content if c.type == 'text'],
                              'server_count': len(guild_rows)}))


if __name__ == '__main__':
    asyncio.run(main())
