import tomllib
import pytest
from scripts import configure_codex


@pytest.mark.parametrize('discord_section', [
    '',
    '[mcp_servers.discord-mcp]\nurl = "http://127.0.0.1:8085/mcp"\nenabled = false\nstartup_timeout_sec = 60\n\n',
])
def test_configuration_preserves_other_servers_and_does_not_duplicate(tmp_path, monkeypatch, discord_section):
    directory = tmp_path / '.codex'
    directory.mkdir()
    path = directory / 'config.toml'
    path.write_text('model = "example"\n\n' + discord_section + '[mcp_servers.other]\ncommand = "example"\n', encoding='utf-8')
    monkeypatch.setattr(configure_codex.Path, 'home', staticmethod(lambda: tmp_path))
    monkeypatch.setattr(configure_codex, 'load_credentials', lambda: {'mcp_token': 'test-only-value'})
    configure_codex.main()
    configure_codex.main()
    config = tomllib.loads(path.read_text(encoding='utf-8'))
    assert config['model'] == 'example'
    assert config['mcp_servers']['other'] == {'command': 'example'}
    assert config['mcp_servers']['discord-mcp']['enabled'] is True
    assert config['mcp_servers']['discord-mcp']['http_headers']['Authorization'] == 'Bearer test-only-value'
