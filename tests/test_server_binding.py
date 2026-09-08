import pytest
import uvicorn

from main import run_server


@pytest.mark.parametrize("configured_host,expected_host", [(None, "127.0.0.1"), ("0.0.0.0", "0.0.0.0")])
def test_server_uses_loopback_unless_host_is_explicit(monkeypatch, configured_host, expected_host):
    monkeypatch.delenv("SCORING_HOST", raising=False)
    if configured_host is not None:
        monkeypatch.setenv("SCORING_HOST", configured_host)
    monkeypatch.setenv("SCORING_PORT", "8876")
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    run_server()

    assert len(calls) == 1
    assert calls[0][0] == ("main:app",)
    assert calls[0][1]["host"] == expected_host
    assert calls[0][1]["port"] == 8876
    assert calls[0][1]["reload"] is False
