from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from lakeprune.server import create_server

@pytest.fixture()
def server_handle(demo_audit):
    server = create_server(demo_audit["db"], port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        yield server, base, demo_audit["result"].run_id
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))

def _get(base: str, path: str):
    return _opener().open(base + path, timeout=3)

def test_healthz_index_and_run_detail(server_handle):
    _, base, run_id = server_handle
    with _get(base, "/healthz") as response:
        assert response.status == 200 and json.loads(response.read()) == {"status": "ok", "tool": "lakeprune"}
    with _get(base, "/") as response:
        index = response.read().decode("utf-8")
    assert response.status == 200 and run_id in index and f"/run/{run_id}" in index
    assert "https://" not in index and "http://" not in index
    with _get(base, f"/run/{run_id}") as response:
        detail = response.read().decode("utf-8")
    assert response.status == 200 and "q_readings_hot_day" in detail
    assert "MIS_PRUNE_RISK" in detail and "STATS_MISMATCH" in detail
    assert "No write endpoints" in detail and "<script" not in detail and "https://" not in detail

def test_unknown_run_write_methods_and_non_loopback_bind(server_handle, demo_audit):
    _, base, _ = server_handle
    with pytest.raises(urllib.error.HTTPError) as missing:
        _get(base, "/run/not-a-real-run")
    assert missing.value.code == 404
    request = urllib.request.Request(base + "/", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as rejected:
        _opener().open(request, timeout=3)
    assert rejected.value.code == 405 and rejected.value.headers["Allow"] == "GET"
    with pytest.raises(ValueError):
        create_server(demo_audit["db"], port=0, host="0.0.0.0")
