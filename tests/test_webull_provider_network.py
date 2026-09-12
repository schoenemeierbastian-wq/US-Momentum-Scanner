
from types import SimpleNamespace

import pytest

from spike_scanner.providers.webull_provider import (
    TransientNetworkError,
    WebullMarketDataProvider,
    is_transient_network_error,
)


class FakeResponse:
    status_code = 503
    text = "service unavailable"


def _provider():
    provider = object.__new__(WebullMarketDataProvider)
    provider.settings = SimpleNamespace(
        network_retry_attempts=4,
        network_retry_base_seconds=0.5,
    )
    return provider


def test_final_transient_http_is_classified_as_network_error():
    provider = _provider()
    calls = 0

    def call():
        nonlocal calls
        calls += 1
        return FakeResponse()

    with pytest.raises(TransientNetworkError) as exc_info:
        provider._request_with_retry(
            "Test",
            call,
            attempts_override=1,
        )

    assert calls == 1
    assert is_transient_network_error(exc_info.value)


def test_attempts_override_limits_transient_http_retries(monkeypatch):
    provider = _provider()
    calls = 0

    monkeypatch.setattr(
        "spike_scanner.providers.webull_provider.time.sleep",
        lambda _: None,
    )

    def call():
        nonlocal calls
        calls += 1
        return FakeResponse()

    with pytest.raises(TransientNetworkError):
        provider._request_with_retry(
            "Test",
            call,
            attempts_override=2,
        )

    assert calls == 2
