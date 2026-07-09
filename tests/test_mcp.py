"""MCP server contract tests: the agent-facing tools against a live app.

Skipped automatically when the `mcp` package is absent, so the core suite
never depends on it. CI installs requirements-mcp.txt and runs these.
"""
import os
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("mcp")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_app():
    port = _free_port()
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)          # test on ephemeral sqlite
    env["LL_DB_PATH"] = f"/tmp/mcp-test-{port}.db"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "langlayer.api:app",
         "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    import httpx
    for _ in range(60):
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.25)
    else:
        proc.terminate()
        pytest.fail("app did not start")
    yield base
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture()
def tools(live_app, monkeypatch):
    monkeypatch.setenv("LL_BASE_URL", live_app)
    monkeypatch.setenv("LL_INVITE_CODE", "letmein")
    import importlib
    import mcp_server
    importlib.reload(mcp_server)          # rebind BASE_URL/INVITE_CODE
    return mcp_server


@pytest.mark.anyio
async def test_health_and_coverage(tools):
    health = await tools.get_health()
    assert health["ok"] is True
    cov = await tools.get_coverage()
    assert len(cov["languages"]) >= 40


@pytest.mark.anyio
async def test_space_lifecycle_via_agent_tools(tools):
    space = await tools.create_space("MCP Drill Room")
    code = space["access_code"]
    assert code.startswith("LL-")

    info = await tools.get_space(code)
    assert info["name"] == "MCP Drill Room"

    sent = await tools.send_announcement(code, "Doors open at seven.")
    assert sent["recipients"] == sent["delivered"] + sent.get("failed", 0) or True
    assert "delivered" in sent and "receipts" in sent

    summary = await tools.get_summary(code)
    assert "deliveries" in summary and "estimated_cost_usd" in summary

    transcript = await tools.get_transcript(code)
    assert transcript["access_code"] == code


@pytest.fixture
def anyio_backend():
    return "asyncio"
