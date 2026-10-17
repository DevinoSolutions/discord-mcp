import pytest
from starlette.testclient import TestClient
from local_server import LocalAuth


async def endpoint(scope, receive, send):
    from starlette.responses import JSONResponse
    await JSONResponse({'ok': True})(scope, receive, send)


def test_http_requires_bearer_token():
    client = TestClient(LocalAuth(endpoint, 'x' * 48, lambda: True))
    assert client.post('/mcp').status_code == 401
    assert client.post('/mcp', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert client.post('/mcp', headers={'Authorization': 'Bearer ' + 'x' * 48}).status_code == 200


def test_cross_site_browser_requests_are_denied():
    client = TestClient(LocalAuth(endpoint, 'x' * 48, lambda: True))
    assert client.post('/mcp', headers={'Authorization': 'Bearer ' + 'x' * 48, 'Origin': 'https://untrusted.example'}).status_code == 403


def test_health_exposes_only_connection_status():
    client = TestClient(LocalAuth(endpoint, 'x' * 48, lambda: True))
    assert client.get('/health').json() == {'status': 'ready'}
    connecting = TestClient(LocalAuth(endpoint, 'x' * 48, lambda: False))
    assert connecting.get('/health').status_code == 503


def test_empty_bearer_is_rejected():
    with pytest.raises(ValueError):
        LocalAuth(endpoint, '', lambda: True)
