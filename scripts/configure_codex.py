"""Enable the verified local endpoint without printing any credentials."""
import datetime
import json
from pathlib import Path
import re
import shutil
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_server import load_credentials


def main():
    credentials = load_credentials()
    path = Path.home() / '.codex/config.toml'
    original = path.read_text(encoding='utf-8')
    config = tomllib.loads(original)
    current = config.get('mcp_servers', {}).get('discord-mcp', {})
    if current and current.get('url') != 'http://127.0.0.1:8085/mcp':
        raise RuntimeError('Existing Discord MCP configuration differs; review it before replacing.')
    block = '\n'.join([
        '[mcp_servers.discord-mcp]',
        'url = "http://127.0.0.1:8085/mcp"',
        'enabled = true',
        'startup_timeout_sec = 60',
        'tool_timeout_sec = 120',
        'http_headers = { Authorization = ' + json.dumps('Bearer ' + credentials['mcp_token']) + ' }',
        '', '',
    ])
    pattern = r'^\[mcp_servers\.discord-mcp\]\s*\n.*?(?=^\[|\Z)'
    if re.search(pattern, original, re.M | re.S):
        updated = re.sub(pattern, lambda _: block, original, flags=re.M | re.S)
    else:
        updated = original.rstrip() + '\n\n' + block
    parsed = tomllib.loads(updated)
    assert parsed['mcp_servers']['discord-mcp']['http_headers']['Authorization'] == 'Bearer ' + credentials['mcp_token']
    backup = path.with_name('config.toml.before-personal-discord-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '.bak')
    shutil.copy2(path, backup)
    path.write_text(updated, encoding='utf-8')
    print('Codex configured for authenticated personal-account HTTP MCP.')


if __name__ == '__main__':
    main()
