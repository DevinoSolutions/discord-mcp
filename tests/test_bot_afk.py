from discord_py_self_mcp.bot import client


def test_selfbot_starts_afk_so_mobile_pushes_are_delivered():
    assert client._connection._afk is True
    assert client._connection._idle_since
