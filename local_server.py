"""Authenticated, persistent HTTP entry point for this Windows installation."""
import hmac
import json
import os
from pathlib import Path
import sys

from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parent


def load_credentials():
    import win32crypt
    encrypted = (ROOT / 'credentials.dpapi').read_bytes()
    return json.loads(win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1])


class LocalAuth:
    def __init__(self, app, bearer: str, ready):
        if len(bearer) < 32:
            raise ValueError('A strong local MCP bearer token is required.')
        self.app = app
        self.expected = ('Bearer ' + bearer).encode()
        self.ready = ready

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = dict(scope.get('headers', []))
            # No browser-origin cross-site requests, including authenticated ones.
            origin = headers.get(b'origin')
            if origin and origin != b'http://127.0.0.1:8085':
                await JSONResponse({'error': 'Origin not allowed'}, status_code=403)(scope, receive, send)
                return
            if scope['path'] == '/health' and scope['method'] == 'GET':
                # ready() returns a bool or a status string ('ready', 'connecting', 'error').
                status = self.ready()
                if isinstance(status, bool):
                    status = 'ready' if status else 'connecting'
                await JSONResponse({'status': status}, status_code=200 if status == 'ready' else 503)(scope, receive, send)
                return
            if not hmac.compare_digest(headers.get(b'authorization', b''), self.expected):
                await JSONResponse({'error': 'Unauthorized'}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main():
    import msvcrt
    # Hold the lock for the whole process, before authenticating to Discord.
    singleton = (ROOT / '.singleton.lock').open('a+b')
    singleton.seek(0)
    singleton.write(b'0')
    singleton.flush()
    singleton.seek(0)
    try:
        msvcrt.locking(singleton.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return
    log_stream = (ROOT / 'server.log').open('a', encoding='utf-8', buffering=1)
    sys.stdout = sys.stderr = log_stream
    import uvicorn
    credentials = load_credentials()
    os.environ['DISCORD_TOKEN'] = credentials['discord_token']
    os.environ['DISCORD_EXPECTED_USER_ID'] = credentials['user_id']
    from discord_py_self_mcp.main import connection_status, create_streamable_http_app
    # The port opens before Discord logs in; /health stays 503 until READY, goes
    # back to 503 whenever the gateway socket drops, and reports 'error' if
    # Discord rejected the session.
    app = LocalAuth(create_streamable_http_app('127.0.0.1'), credentials['mcp_token'], connection_status)
    # Selector, not uvicorn's default Proactor loop on Windows: Proactor's accept loop
    # permanently closes the listening socket on WinError 64 (client reset mid-accept),
    # leaving the process alive with nothing on 8085.
    uvicorn.run(app, host='127.0.0.1', port=8085, log_level='info', access_log=False,
                loop='asyncio:SelectorEventLoop')


if __name__ == '__main__':
    main()
