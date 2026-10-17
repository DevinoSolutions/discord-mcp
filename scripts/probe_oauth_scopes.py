"""One-shot localhost OAuth scope probe. Never logs credentials or changes guilds."""
import hmac
import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, parse_qs

import httpx
import win32crypt

ROOT = Path(__file__).resolve().parents[1]
STATE = secrets.token_urlsafe(32)
ORIGIN = 'http://127.0.0.1:8766'
EXPECTED_SCOPES = {'identify', 'guilds', 'guilds.members.read'}
# Your Discord application's client ID, with http://127.0.0.1:8766/callback as a redirect URI.
CLIENT_ID = os.environ.get('DISCORD_OAUTH_CLIENT_ID', '')
EXPECTED_USER_ID = os.environ.get('DISCORD_EXPECTED_USER_ID', '')
DONE = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, data, kind='application/json'):
        data = data.encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != '/callback' or self.headers.get('Host') != '127.0.0.1:8766':
            return self.reply(404, '{}')
        self.reply(200, '''<!doctype html><meta charset="utf-8"><title>Discord scope test</title>
<h1>Discord scope test</h1><pre id="result">Checking token permissions...</pre>
<script>
const fragment = location.hash.slice(1);
history.replaceState(null, '', '/callback');
fetch('/capture', {method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:fragment})
.then(r=>r.json()).then(r=>document.getElementById('result').textContent=JSON.stringify(r,null,2))
.catch(()=>document.getElementById('result').textContent='Scope test failed.');
</script>''', 'text/html; charset=utf-8')

    def do_POST(self):
        global DONE
        if self.path != '/capture' or self.headers.get('Origin') != ORIGIN or self.headers.get('Host') != '127.0.0.1:8766':
            return self.reply(403, '{}')
        size = int(self.headers.get('Content-Length', '0'))
        if size < 1 or size > 16384:
            return self.reply(400, '{}')
        values = {k: v[0] for k, v in parse_qs(self.rfile.read(size).decode()).items()}
        if not hmac.compare_digest(values.get('state', ''), STATE):
            return self.reply(403, '{"error":"OAuth state mismatch"}')
        if not values.get('access_token') or values.get('token_type', '').lower() != 'bearer':
            return self.reply(400, '{"error":"No bearer token received"}')
        if set(values.get('scope', '').split()) != EXPECTED_SCOPES:
            return self.reply(400, '{"error":"Unexpected scope grant"}')
        try:
            report = {'granted_scopes': sorted(EXPECTED_SCOPES), 'tests': {}}
            with httpx.Client(base_url='https://discord.com/api/v10', headers={'Authorization': 'Bearer ' + values['access_token']}, timeout=20) as client:
                me = client.get('/users/@me')
                report['tests']['read_profile'] = me.status_code
                me.raise_for_status()
                if EXPECTED_USER_ID and me.json()['id'] != EXPECTED_USER_ID:
                    raise ValueError('Unexpected user')
                report['username'] = me.json()['username']
                auth = client.get('/oauth2/@me')
                report['tests']['inspect_authorization'] = auth.status_code
                if auth.status_code == 200:
                    report['server_confirmed_scopes'] = auth.json().get('scopes', [])
                guilds = client.get('/users/@me/guilds')
                report['tests']['list_servers'] = guilds.status_code
                guilds.raise_for_status()
                items = guilds.json()
                report['server_count'] = len(items)
                target = next((g for g in items if g.get('owner')), items[0] if items else None)
                if target:
                    gid = target['id']
                    report['sample_server'] = {'name': target['name'], 'account_is_owner': target.get('owner', False)}
                    report['tests']['read_own_membership'] = client.get(f'/users/@me/guilds/{gid}/member').status_code
                    report['tests']['read_server_channels'] = client.get(f'/guilds/{gid}/channels').status_code
                report['tests']['read_connections_without_scope'] = client.get('/users/@me/connections').status_code
            vault = {k: values[k] for k in ('access_token', 'token_type', 'expires_in', 'scope') if k in values}
            vault['created_at'] = time.time()
            (ROOT / 'oauth-probe.dpapi').write_bytes(win32crypt.CryptProtectData(json.dumps(vault).encode(), 'Discord restricted OAuth probe', None, None, None, 0))
            (ROOT / 'oauth-probe-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            DONE = True
            self.reply(200, json.dumps(report))
        except Exception as exc:
            self.reply(500, json.dumps({'error': type(exc).__name__}))


if __name__ == '__main__':
    if not CLIENT_ID:
        raise SystemExit('Set DISCORD_OAUTH_CLIENT_ID to your Discord application client ID.')
    server = HTTPServer(('127.0.0.1', 8766), Handler)
    server.timeout = 1
    url = 'https://discord.com/oauth2/authorize?' + urlencode({
        'client_id': CLIENT_ID, 'response_type': 'token',
        'redirect_uri': ORIGIN + '/callback', 'scope': ' '.join(sorted(EXPECTED_SCOPES)),
        'state': STATE, 'prompt': 'consent',
    })
    (ROOT / 'oauth-probe-url.txt').write_text(url, encoding='utf-8')
    print('Local OAuth probe ready on 127.0.0.1:8766', flush=True)
    deadline = time.monotonic() + 900
    while not DONE and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    print('Probe complete' if DONE else 'Probe expired', flush=True)
