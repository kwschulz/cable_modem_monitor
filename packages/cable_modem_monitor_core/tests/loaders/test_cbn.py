"""Tests for CBNLoader.

Table-driven for error scenarios. Mock HTTP session for all tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests
from requests.cookies import RequestsCookieJar
from solentlabs.cable_modem_monitor_core.fetch_list import ResourceTarget
from solentlabs.cable_modem_monitor_core.loaders.cbn import CBNLoader
from solentlabs.cable_modem_monitor_core.loaders.http import ResourceLoadError

_SAMPLE_XML = "<downstream_table><downstream><freq>500</freq></downstream></downstream_table>"
_MALFORMED_XML = "this is not xml {{"


def _make_session(token: str = "tok") -> MagicMock:
    """Create a mock session with sessionToken cookie."""
    session = MagicMock(spec=requests.Session)
    jar = RequestsCookieJar()
    jar.set("sessionToken", token)
    session.cookies = jar
    return session


def _make_loader(
    session: MagicMock,
    *,
    getter_endpoint: str = "/xml/getter.xml",
    cookie_name: str = "sessionToken",
    timeout: int = 10,
    model: str = "T100",
) -> CBNLoader:
    """Create a CBNLoader with defaults."""
    return CBNLoader(
        session=session,
        base_url="http://192.168.0.1",
        getter_endpoint=getter_endpoint,
        session_cookie_name=cookie_name,
        timeout=timeout,
        model=model,
    )


def _targets(*funs: str) -> list[ResourceTarget]:
    """Create ResourceTarget list from fun values."""
    return [ResourceTarget(path=f, format="xml") for f in funs]


def _mock_response(
    status_code: int = 200,
    text: str = _SAMPLE_XML,
    content: bytes = b"",
    ok: bool | None = None,
) -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    resp.content = content or text.encode()
    resp.ok = ok if ok is not None else (200 <= status_code < 400)
    resp.headers = {"Content-Type": "text/xml"}
    resp.request = None
    return resp


# ---------------------------------------------------------------------------
# Successful fetch
# ---------------------------------------------------------------------------


class TestSuccessfulFetch:
    """Successful CBN resource loading."""

    def test_single_target(self) -> None:
        """Single target returns parsed XML element."""
        session = _make_session("tok123")
        session.post.return_value = _mock_response()

        loader = _make_loader(session)
        result = loader.fetch(_targets("10"))

        assert "10" in result
        assert result["10"].tag == "downstream_table"

    def test_multiple_targets(self) -> None:
        """Multiple targets each get their own entry."""
        session = _make_session()

        xml_ds = "<downstream_table><ds><freq>500</freq></ds></downstream_table>"
        xml_us = "<upstream_table><us><freq>30</freq></us></upstream_table>"
        responses = [
            _mock_response(text=xml_ds, content=xml_ds.encode()),
            _mock_response(text=xml_us, content=xml_us.encode()),
        ]
        session.post.side_effect = responses

        loader = _make_loader(session)
        result = loader.fetch(_targets("10", "11"))

        assert len(result) == 2
        assert result["10"].tag == "downstream_table"
        assert result["11"].tag == "upstream_table"

    def test_token_is_first_param(self) -> None:
        """POST body starts with token= parameter."""
        session = _make_session("my_token")
        session.post.return_value = _mock_response()

        loader = _make_loader(session)
        loader.fetch(_targets("10"))

        data_call = session.post.call_args_list[0]
        post_body = data_call.kwargs.get("data", "")
        assert post_body.startswith("token=my_token")

    def test_no_logout_in_loader(self) -> None:
        """Loader does NOT send logout — collector handles it."""
        session = _make_session()
        session.post.return_value = _mock_response()

        loader = _make_loader(session)
        loader.fetch(_targets("10"))

        # Only one POST: data fetch (no logout)
        assert session.post.call_count == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Errors are logged, targets skipped, fetch continues."""

    # Skip-and-continue was replaced by surface-and-let-policy-decide
    # (#200). Coverage for what escapes now lives in
    # TestConnectivityIsNotAMissingResource: the non-2xx case is
    # test_http_error_raises_with_status, and "one failure stops the
    # poll" is test_connection_error_stops_the_poll.

    # Each row: (exception_class, exception_arg, what escapes fetch()).
    # SSLError subclasses requests.ConnectionError, so it travels the
    # connectivity path and the collector reads it as CONNECTIVITY
    # (RESOURCE_LOADING_SPEC § Error Signals). HTTPError does not, so it
    # becomes LOAD_ERROR the same way loaders/http.py converts it.
    _FETCH_EXCEPTIONS: list[tuple[type[Exception], str, type[Exception]]] = [
        (requests.ConnectionError, "refused", requests.ConnectionError),
        (requests.Timeout, "timed out", requests.Timeout),
        (requests.exceptions.SSLError, "handshake", requests.ConnectionError),
        (requests.HTTPError, "bad response", ResourceLoadError),
    ]

    @pytest.mark.parametrize(
        "exc_class,exc_arg,escapes_as",
        _FETCH_EXCEPTIONS,
        ids=[c[0].__name__ for c in _FETCH_EXCEPTIONS],
    )
    def test_transport_error_escapes_for_the_collector_to_classify(
        self,
        exc_class: type[Exception],
        exc_arg: str,
        escapes_as: type[Exception],
    ) -> None:
        """Every transport failure leaves the loader; none is silently skipped."""
        session = _make_session()
        session.post.side_effect = exc_class(exc_arg)

        loader = _make_loader(session)

        with pytest.raises(escapes_as):
            loader.fetch(_targets("10"))

    def test_malformed_xml_skipped(self) -> None:
        """Malformed XML response skips the target."""
        session = _make_session()

        bad_xml = _mock_response(text=_MALFORMED_XML, content=_MALFORMED_XML.encode())
        good_xml = _mock_response()
        session.post.side_effect = [bad_xml, good_xml]

        loader = _make_loader(session)
        result = loader.fetch(_targets("10", "11"))

        assert "10" not in result
        assert "11" in result


class TestConnectivityIsNotAMissingResource:
    """A fetch failure the loader swallows becomes a stub-page verdict (#200).

    RESOURCE_LOADING_SPEC § Error Signals gives the loader one job on a
    transport failure: surface it. Returning ``None`` drops the key from
    the resource dict, the coordinator counts 0/N anchors, and the poll
    ends as LOAD_INTEGRITY — which increments the auth streak and reports
    AUTH_FAILED. On the CH7465MT that turned a few seconds of
    unreachability into a stopped integration and a reauth prompt.

    Skipping stays correct for the one case it was built for: a body that
    will not decode (d6cd3246). That path is unchanged below.
    """

    def test_connection_error_propagates(self) -> None:
        """An unreachable modem raises, so the collector can report CONNECTIVITY (UC-30)."""
        session = _make_session()
        session.post.side_effect = requests.ConnectionError("refused")

        loader = _make_loader(session)

        with pytest.raises(requests.ConnectionError):
            loader.fetch(_targets("10", "11"))

    def test_timeout_propagates(self) -> None:
        """Same for a modem too slow to answer (UC-31)."""
        session = _make_session()
        session.post.side_effect = requests.Timeout("timed out")

        loader = _make_loader(session)

        with pytest.raises(requests.Timeout):
            loader.fetch(_targets("10"))

    def test_connection_error_stops_the_poll(self) -> None:
        """The first failure aborts; the remaining targets are not attempted.

        Discriminating: a loader that raised only after working through
        every target would satisfy the test above while still spending
        four round trips on a modem known to be unreachable.
        """
        session = _make_session()
        session.post.side_effect = requests.ConnectionError("refused")

        loader = _make_loader(session)

        with pytest.raises(requests.ConnectionError):
            loader.fetch(_targets("10", "11", "2", "144"))

        assert session.post.call_count == 1

    # Each row: (status, expected signal the collector derives from it).
    # 401/403 is a stale session (LOAD_AUTH); everything else is the
    # modem declining to serve (LOAD_ERROR, no auth streak). Skipping
    # the target instead routes both to LOAD_INTEGRITY, which counts
    # toward the auth streak the 5xx is explicitly not supposed to touch.
    _STATUS_CASES = [(401, "stale session"), (403, "stale session"), (500, "server error"), (503, "declining")]

    @pytest.mark.parametrize("status,why", _STATUS_CASES, ids=[str(c[0]) for c in _STATUS_CASES])
    def test_http_error_raises_with_status(self, status: int, why: str) -> None:
        """A non-2xx carries its status out, so the collector can classify it (UC-32)."""
        session = _make_session()
        session.post.side_effect = [_mock_response(status_code=status, ok=False)]

        loader = _make_loader(session)

        with pytest.raises(ResourceLoadError) as exc_info:
            loader.fetch(_targets("10"))

        assert exc_info.value.status_code == status, why

    def test_malformed_xml_still_skips(self) -> None:
        """Unchanged: a body that will not decode is the case skipping exists for."""
        session = _make_session()
        session.post.side_effect = [
            _mock_response(text=_MALFORMED_XML, content=_MALFORMED_XML.encode()),
            _mock_response(),
        ]

        loader = _make_loader(session)
        result = loader.fetch(_targets("10", "11"))

        assert "10" not in result
        assert "11" in result
