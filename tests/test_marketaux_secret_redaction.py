import logging

import pytest
import requests

from news.marketaux import MarketauxDiscovery


class _ProviderErrorResponse:
    def __init__(self, *, status_code: int, payload: dict, http_error: bool) -> None:
        self.status_code = status_code
        self._payload = payload
        self._http_error = http_error

    def raise_for_status(self) -> None:
        if self._http_error:
            raise requests.HTTPError("mock provider failure", response=self)

    def json(self) -> dict:
        return self._payload


@pytest.mark.parametrize(
    ("status_code", "http_error"),
    [(200, False), (401, True)],
)
def test_marketaux_error_diagnostics_redact_token_from_logs_and_snapshot(
    status_code, http_error, caplog
):
    token = "MARKETAUX_TEST_SECRET_VALUE"
    response = _ProviderErrorResponse(
        status_code=status_code,
        payload={
            "error": {
                "code": "invalid_token",
                "message": f"rejected api_token={token}; credential {token}",
            }
        },
        http_error=http_error,
    )
    client = MarketauxDiscovery(
        logging.getLogger("test.marketaux.secret-redaction"),
        "2026-10-26",
        token=token,
        request_get=lambda *args, **kwargs: response,
    )

    with caplog.at_level(logging.WARNING):
        assert client.search("synthetic query", "GENERAL") == []

    diagnostics = client.query_diagnostics[0]
    assert diagnostics["error_code"] == "invalid_token"
    assert "api_token=[REDACTED]" in diagnostics["error_message"]
    assert token not in diagnostics["error_message"]
    assert token not in caplog.text
