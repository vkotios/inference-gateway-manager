"""Security middleware regressions: malformed credentials and host parsing."""

import httpx
import pytest

from gateway.app import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host,authorization,status",
    [
        ("testserver", b"Bearer \xff", 401),
        ("testserver", b"bearer " + b"a" * 32, 200),
        ("[::1]:9000", b"Bearer " + b"a" * 32, 200),
        ("evil.test", b"Bearer " + b"a" * 32, 403),
        ("user@testserver", b"Bearer " + b"a" * 32, 403),
    ],
)
async def test_s4_l9_authorization_and_ipv6_host(tmp_path, host, authorization, status):
    config = {"allowed_hosts": ["testserver", "[::1]"], "engines": {"one": {"models": {}}}}
    app = create_app(config, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            result = await client.get(
                "/api/engines", headers={"Host": host, "Authorization": authorization}
            )
            assert result.status_code == status
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin,expected",
    [("https://testserver", 200), ("https://evil.test", 403), ("http://testserver", 200)],
)
async def test_l7_tls_origin_is_explicit_and_cross_origin_remains_denied(
    tmp_path, origin, expected
):
    config = {
        "allowed_hosts": ["testserver"],
        "allowed_origins": ["https://testserver"],
        "engines": {"one": {"models": {}}},
    }
    app = create_app(config, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            result = await client.post("/api/unload", json={}, headers={"Origin": origin})
            assert result.status_code == expected
    finally:
        await app.state.manager.close()
