"""Store an explicitly authorized Discord session from Stealth request metadata.

The credential stays inside this process and Windows DPAPI. It is never printed.
"""
import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import secrets
import tomllib
from urllib.parse import urlparse

import httpx
import win32crypt
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main(request_id, expected_username=None):
    config = tomllib.loads((Path.home() / '.codex/config.toml').read_text(encoding='utf-8'))
    server = config['mcp_servers']['stealth-chrome-devtools-mcp']
    params = StdioServerParameters(command=server['command'], args=server.get('args', []), env={**os.environ, **server.get('env', {})})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
            await session.initialize()
            result = await session.call_tool('get_request_details', {'request_id': request_id})
            if result.isError:
                raise RuntimeError('Stealth could not retrieve the authorized Discord request.')
            data = result.structuredContent
            if data is None:
                data = json.loads(next(c.text for c in result.content if c.type == 'text'))
            detail = data.get('result', data)
            if not isinstance(detail, dict):
                raise RuntimeError('Discord request metadata is no longer available.')
            url = urlparse(detail.get('url', ''))
            if url.scheme != 'https' or url.hostname != 'discord.com' or not url.path.startswith('/api/'):
                raise RuntimeError('The selected request is not an authenticated Discord API request.')
            headers = {k.lower(): v for k, v in detail.get('headers', {}).items()}
            token = headers.get('authorization')
            if not token or token.startswith(('Bot ', 'Bearer ')):
                raise RuntimeError('The selected request does not carry a personal Discord session.')
            async with httpx.AsyncClient() as http:
                response = await http.get('https://discord.com/api/v9/users/@me', headers={'Authorization': token})
            if response.status_code != 200:
                raise RuntimeError('Discord did not accept the session credential.')
            user = response.json()
            if expected_username and user.get('username', '').lower() != expected_username.lower():
                raise RuntimeError('The signed-in account is not ' + expected_username + '.')
            data = {'discord_token': token, 'user_id': user['id'], 'username': user['username'], 'mcp_token': secrets.token_urlsafe(48)}
            encrypted = win32crypt.CryptProtectData(json.dumps(data).encode(), 'Discord personal MCP', None, None, None, 0)
            (ROOT / 'credentials.dpapi').write_bytes(encrypted)
            print('Encrypted credentials saved for ' + user['username'] + ' (ID ' + user['id'] + ').', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('request_id')
    parser.add_argument('--expected-username', default=os.environ.get('DISCORD_EXPECTED_USERNAME'),
                        help='Refuse to store the session unless it belongs to this username.')
    args = parser.parse_args()
    asyncio.run(main(args.request_id, args.expected_username))
