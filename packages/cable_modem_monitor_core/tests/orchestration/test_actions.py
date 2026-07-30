"""Tests for action execution — dispatch, HTTP, and HNAP executors.

Covers: single dispatch routing, HTTP pre-fetch + form-action extraction,
HNAP SOAP signing + pre-fetch + param interpolation + response validation,
connection loss handling for both transports.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
import requests
import requests.cookies
from solentlabs.cable_modem_monitor_core.auth.base import AuthContext
from solentlabs.cable_modem_monitor_core.models.modem_config.actions import (
    HnapAction,
    HttpAction,
)
from solentlabs.cable_modem_monitor_core.models.modem_config.auth import HnapAuth, NoneAuth
from solentlabs.cable_modem_monitor_core.orchestration.actions import (
    execute_action,
    execute_hnap_action,
    execute_http_action,
)

# ------------------------------------------------------------------
# Tests — execute_http_action
# ------------------------------------------------------------------


class TestExecuteHttpAction:
    """HTTP action execution and error handling."""

    def test_connection_error_treated_as_success(self) -> None:
        """ConnectionError during restart is success (modem rebooting)."""
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.ConnectionError("refused")
        action = HttpAction(type="http", method="POST", endpoint="/restart.htm")

        result = execute_http_action(session, "http://192.168.100.1", action)

        assert result.success is True
        session.request.assert_called_once()

    def test_timeout_treated_as_success(self) -> None:
        """Timeout during restart is success."""
        session = MagicMock(spec=requests.Session)
        session.request.side_effect = requests.Timeout("timed out")
        action = HttpAction(type="http", method="POST", endpoint="/restart.htm")

        result = execute_http_action(session, "http://192.168.100.1", action)

        assert result.success is True

    def test_successful_action_returns_status_code(self) -> None:
        """Successful action returns result with status code in details."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = 200
        session.request.return_value = resp
        action = HttpAction(
            type="http",
            method="GET",
            endpoint="/logout",
            headers={"X-Token": "abc"},
            params={"confirm": "yes"},
        )

        result = execute_http_action(session, "http://192.168.100.1", action, timeout=5)

        assert result.success is True
        assert result.details["status_code"] == 200
        session.request.assert_called_once_with(
            "GET",
            "http://192.168.100.1/logout",
            data={"confirm": "yes"},
            headers={"X-Token": "abc"},
            timeout=5,
        )


# ------------------------------------------------------------------
# Tests — HTTP pre-fetch and form-action extraction
# ------------------------------------------------------------------


class TestHttpPreFetch:
    """Pre-fetch URL support for HTTP actions."""

    def test_pre_fetch_executed_before_action(self) -> None:
        """pre_fetch_url is fetched before the main action request."""
        session = MagicMock(spec=requests.Session)
        pre_resp = MagicMock()
        pre_resp.status_code = 200
        pre_resp.content = b"<html>security page</html>"
        pre_resp.text = "<html>security page</html>"
        session.get.return_value = pre_resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/goform/restart",
            pre_fetch_url="/Security.asp",
            params={"RestartAction": "1"},
        )

        execute_http_action(session, "http://192.168.100.1", action)

        session.get.assert_called_once_with(
            "http://192.168.100.1/Security.asp",
            timeout=10,
        )
        session.request.assert_called_once()

    def test_pre_fetch_connection_error_falls_back_to_static(self) -> None:
        """If pre-fetch fails, action uses static endpoint."""
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError("refused")

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/goform/restart",
            pre_fetch_url="/Security.asp",
        )

        execute_http_action(session, "http://192.168.100.1", action)

        session.get.assert_called_once()
        session.request.assert_called_once_with(
            "POST",
            "http://192.168.100.1/goform/restart",
            data=None,
            headers=None,
            timeout=10,
        )

    def test_no_pre_fetch_skips_get(self) -> None:
        """Without pre_fetch_url, no GET request is made."""
        session = MagicMock(spec=requests.Session)
        action = HttpAction(type="http", method="POST", endpoint="/restart")

        execute_http_action(session, "http://192.168.100.1", action)

        session.get.assert_not_called()
        session.request.assert_called_once()


class TestFormActionExtraction:
    """Form-action keyword extraction from pre-fetch pages."""

    def test_extracts_dynamic_endpoint_from_form(self) -> None:
        """Keyword matches form action, dynamic endpoint extracted."""
        session = MagicMock(spec=requests.Session)
        pre_resp = MagicMock()
        pre_resp.status_code = 200
        pre_resp.content = b""
        pre_resp.text = '<html><form name="status" method="POST" ' 'action="/goform/RouterStatus?id=12345">'
        session.get.return_value = pre_resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/goform/RouterStatus",
            pre_fetch_url="/RouterStatus.htm",
            endpoint_pattern="RouterStatus",
        )

        execute_http_action(session, "http://192.168.100.1", action)

        # Main request uses extracted endpoint with session ID
        session.request.assert_called_once_with(
            "POST",
            "http://192.168.100.1/goform/RouterStatus?id=12345",
            data=None,
            headers=None,
            timeout=10,
        )

    def test_double_quotes_in_form_action(self) -> None:
        """Extraction works with double-quoted form action."""
        session = MagicMock(spec=requests.Session)
        pre_resp = MagicMock()
        pre_resp.status_code = 200
        pre_resp.content = b""
        pre_resp.text = '<form action="/goform/RouterStatus?id=99">'
        session.get.return_value = pre_resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/goform/RouterStatus",
            pre_fetch_url="/RouterStatus.htm",
            endpoint_pattern="RouterStatus",
        )

        execute_http_action(session, "http://192.168.100.1", action)

        session.request.assert_called_once_with(
            "POST",
            "http://192.168.100.1/goform/RouterStatus?id=99",
            data=None,
            headers=None,
            timeout=10,
        )

    def test_no_match_falls_back_to_static_endpoint(self) -> None:
        """When keyword not found in any form, static endpoint is used."""
        session = MagicMock(spec=requests.Session)
        pre_resp = MagicMock()
        pre_resp.status_code = 200
        pre_resp.content = b""
        pre_resp.text = "<html>no matching form here</html>"
        session.get.return_value = pre_resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/goform/RouterStatus",
            pre_fetch_url="/RouterStatus.htm",
            endpoint_pattern="RouterStatus",
        )

        execute_http_action(session, "http://192.168.100.1", action)

        # Falls back to static endpoint
        session.request.assert_called_once_with(
            "POST",
            "http://192.168.100.1/goform/RouterStatus",
            data=None,
            headers=None,
            timeout=10,
        )

    def test_no_match_no_fallback_returns_failure(self) -> None:
        """When extraction fails and no static endpoint, action fails."""
        session = MagicMock(spec=requests.Session)
        pre_resp = MagicMock()
        pre_resp.status_code = 200
        pre_resp.content = b""
        pre_resp.text = "<html>no matching form</html>"
        session.get.return_value = pre_resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="",
            pre_fetch_url="/RouterStatus.htm",
            endpoint_pattern="RouterStatus",
        )

        result = execute_http_action(session, "http://192.168.100.1", action)

        assert result.success is False
        assert "extraction failed" in result.message.lower()
        session.request.assert_not_called()

    def test_case_insensitive_form_matching(self) -> None:
        """Form tag matching is case-insensitive."""
        session = MagicMock(spec=requests.Session)
        pre_resp = MagicMock()
        pre_resp.status_code = 200
        pre_resp.content = b""
        pre_resp.text = '<FORM ACTION="/goform/RouterStatus?id=1">'
        session.get.return_value = pre_resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/goform/RouterStatus",
            pre_fetch_url="/RouterStatus.htm",
            endpoint_pattern="RouterStatus",
        )

        execute_http_action(session, "http://192.168.100.1", action)

        session.request.assert_called_once_with(
            "POST",
            "http://192.168.100.1/goform/RouterStatus?id=1",
            data=None,
            headers=None,
            timeout=10,
        )


# ------------------------------------------------------------------
# Tests — execute_hnap_action
# ------------------------------------------------------------------


class TestExecuteHnapAction:
    """HNAP SOAP action execution."""

    def test_basic_action_sends_signed_request(self) -> None:
        """HNAP action sends HMAC-signed SOAP POST."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.json.return_value = {
            "SetStatusSecuritySettingsResponse": {
                "SetStatusSecuritySettingsResult": "OK",
            },
        }
        session.post.return_value = resp

        action = HnapAction(
            type="hnap",
            action_name="SetStatusSecuritySettings",
            params={"MotoStatusSecurityAction": "1"},
            response_key="SetStatusSecuritySettingsResponse",
            result_key="SetStatusSecuritySettingsResult",
            success_value="OK",
        )

        result = execute_hnap_action(
            session,
            "http://192.168.100.1",
            action,
            private_key="test_key",
        )

        assert result.success is True
        assert result.details["result"] == "OK"

        # Verify SOAP headers
        call_kwargs = session.post.call_args
        headers = call_kwargs[1]["headers"] if "headers" in call_kwargs[1] else call_kwargs[0][2]
        assert "HNAP_AUTH" in headers
        assert "SOAPAction" in headers

    def test_connection_error_treated_as_success(self) -> None:
        """Connection drop during restart = success."""
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = requests.ConnectionError("reset")

        action = HnapAction(type="hnap", action_name="Reboot")

        result = execute_hnap_action(
            session,
            "http://192.168.100.1",
            action,
            private_key="key",
        )

        assert result.success is True

    def test_unexpected_result_returns_failure(self) -> None:
        """HNAP action with unexpected result value fails."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.json.return_value = {
            "RebootResponse": {"RebootResult": "ERROR"},
        }
        session.post.return_value = resp

        action = HnapAction(
            type="hnap",
            action_name="Reboot",
            response_key="RebootResponse",
            result_key="RebootResult",
            success_value="OK",
        )

        result = execute_hnap_action(
            session,
            "http://192.168.100.1",
            action,
            private_key="key",
        )

        assert result.success is False
        assert "ERROR" in result.message

    def test_invalid_json_response(self) -> None:
        """Non-JSON response returns failure."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.json.side_effect = ValueError("not json")
        resp.status_code = 500
        session.post.return_value = resp

        action = HnapAction(type="hnap", action_name="Reboot")

        result = execute_hnap_action(
            session,
            "http://192.168.100.1",
            action,
            private_key="key",
        )

        assert result.success is False

    # ┌────────────────────┬─────────────────────────────────┐
    # │ json_value         │ description                     │
    # ├────────────────────┼─────────────────────────────────┤
    # │ "not a dict"       │ string response                 │
    # │ [1, 2, 3]          │ list response                   │
    # │ 42                 │ integer response                │
    # └────────────────────┴─────────────────────────────────┘
    #
    # fmt: off
    ACTION_NOT_DICT_CASES = [
        # (json_value,    description)
        ("not a dict",    "string response"),
        ([1, 2, 3],       "list response"),
        (42,              "integer response"),
    ]
    # fmt: on

    @pytest.mark.parametrize(
        "json_value,desc",
        ACTION_NOT_DICT_CASES,
        ids=[c[1] for c in ACTION_NOT_DICT_CASES],
    )
    def test_non_dict_json_action_response(self, json_value: object, desc: str) -> None:
        """Non-dict JSON response returns failure."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.json.return_value = json_value
        resp.status_code = 200
        session.post.return_value = resp

        action = HnapAction(
            type="hnap",
            action_name="Reboot",
            response_key="RebootResponse",
            result_key="RebootResult",
            success_value="OK",
        )

        result = execute_hnap_action(
            session,
            "http://192.168.100.1",
            action,
            private_key="key",
        )

        assert result.success is False
        assert "not a json object" in result.message.lower()


class TestHnapPreFetch:
    """HNAP pre-fetch action for parameter interpolation."""

    def test_pre_fetch_values_interpolated(self) -> None:
        """Pre-fetch response values replace ${var:default} placeholders."""
        session = MagicMock(spec=requests.Session)

        # Pre-fetch response
        pre_resp = MagicMock()
        pre_resp.json.return_value = {
            "GetArrisConfigurationInfoResponse": {
                "ethSWEthEEE": "1",
                "LedStatus": "0",
            },
        }

        # Main action response
        main_resp = MagicMock()
        main_resp.json.return_value = {
            "SetArrisConfigurationInfoResponse": {
                "SetArrisConfigurationInfoResult": "OK",
            },
        }

        session.post.side_effect = [pre_resp, main_resp]

        action = HnapAction(
            type="hnap",
            action_name="SetArrisConfigurationInfo",
            pre_fetch_action="GetArrisConfigurationInfo",
            params={
                "Action": "reboot",
                "SetEEEEnable": "${ethSWEthEEE:0}",
                "LED_Status": "${LedStatus:1}",
            },
            response_key="SetArrisConfigurationInfoResponse",
            result_key="SetArrisConfigurationInfoResult",
            success_value="OK",
        )

        result = execute_hnap_action(
            session,
            "http://192.168.100.1",
            action,
            private_key="test_key",
        )

        assert result.success is True

        # Verify main action params have interpolated values
        main_call = session.post.call_args_list[1]
        body = json.loads(main_call[1]["data"])
        params = body["SetArrisConfigurationInfo"]
        assert params["SetEEEEnable"] == "1"
        assert params["LED_Status"] == "0"
        assert params["Action"] == "reboot"

    def test_pre_fetch_failure_uses_defaults(self) -> None:
        """When pre-fetch fails, ${var:default} uses default values."""
        session = MagicMock(spec=requests.Session)

        # Pre-fetch fails
        session.post.side_effect = [
            requests.ConnectionError("refused"),
            # Main action also fails (modem rebooting)
            requests.ConnectionError("reset"),
        ]

        action = HnapAction(
            type="hnap",
            action_name="SetArrisConfigurationInfo",
            pre_fetch_action="GetArrisConfigurationInfo",
            params={
                "Action": "reboot",
                "SetEEEEnable": "${ethSWEthEEE:0}",
                "LED_Status": "${LedStatus:1}",
            },
        )

        result = execute_hnap_action(
            session,
            "http://192.168.100.1",
            action,
            private_key="key",
        )

        # Action still attempted — pre-fetch failure is not fatal
        assert result.success is True
        assert session.post.call_count == 2

    # ┌────────────────────┬─────────────────────────────────┐
    # │ json_value         │ description                     │
    # ├────────────────────┼─────────────────────────────────┤
    # │ "not a dict"       │ string response                 │
    # │ [1, 2, 3]          │ list response                   │
    # │ 42                 │ integer response                │
    # └────────────────────┴─────────────────────────────────┘
    #
    # fmt: off
    PRE_FETCH_NOT_DICT_CASES = [
        # (json_value,    description)
        ("not a dict",    "string response"),
        ([1, 2, 3],       "list response"),
        (42,              "integer response"),
    ]
    # fmt: on

    @pytest.mark.parametrize(
        "json_value,desc",
        PRE_FETCH_NOT_DICT_CASES,
        ids=[c[1] for c in PRE_FETCH_NOT_DICT_CASES],
    )
    def test_pre_fetch_non_dict_json_uses_defaults(
        self,
        json_value: object,
        desc: str,
    ) -> None:
        """When pre-fetch returns non-dict JSON, ${var:default} uses defaults."""
        session = MagicMock(spec=requests.Session)

        # Pre-fetch returns non-dict JSON
        pre_resp = MagicMock()
        pre_resp.json.return_value = json_value

        # Main action succeeds (connection lost = restart success)
        session.post.side_effect = [
            pre_resp,
            requests.ConnectionError("reset"),
        ]

        action = HnapAction(
            type="hnap",
            action_name="SetArrisConfigurationInfo",
            pre_fetch_action="GetArrisConfigurationInfo",
            params={
                "Action": "reboot",
                "SetEEEEnable": "${ethSWEthEEE:0}",
                "LED_Status": "${LedStatus:1}",
            },
        )

        result = execute_hnap_action(
            session,
            "http://192.168.100.1",
            action,
            private_key="key",
        )

        # Pre-fetch degraded to {} — defaults used
        assert result.success is True
        main_call = session.post.call_args_list[1]
        body = json.loads(main_call[1]["data"])
        params = body["SetArrisConfigurationInfo"]
        assert params["SetEEEEnable"] == "0"  # default
        assert params["LED_Status"] == "1"  # default


# ------------------------------------------------------------------
# Tests — HTTP cookie-param interpolation
# ------------------------------------------------------------------


class TestHttpCookieParamInterpolation:
    """Cookie-value placeholder resolution in HTTP action params."""

    # fmt: off
    INTERPOLATION_CASES = [
        # (params, cookies, expected_data, description)
        ({"confirm": "yes"}, {}, {"confirm": "yes"}, "no placeholders — verbatim"),
        ({"tok": "{cookie:csrfp_token}"}, {"csrfp_token": "abc123"}, {"tok": "abc123"}, "cookie placeholder resolved"),
        ({"tok": "{cookie:missing}"}, {}, {"tok": "{cookie:missing}"}, "absent cookie — sent as-is"),
        ({"a": "{cookie:c1}", "b": "x"}, {"c1": "v1"}, {"a": "v1", "b": "x"}, "mixed — only placeholder replaced"),
    ]
    # fmt: on

    @pytest.mark.parametrize(
        "params,cookies,expected_data,desc",
        INTERPOLATION_CASES,
        ids=[c[3] for c in INTERPOLATION_CASES],
    )
    def test_cookie_param_interpolation(
        self,
        params: dict[str, str],
        cookies: dict[str, str],
        expected_data: dict[str, str],
        desc: str,
    ) -> None:
        """Cookie-value placeholders in params resolve from session jar."""
        session = MagicMock(spec=requests.Session)
        jar = requests.cookies.RequestsCookieJar()
        for name, value in cookies.items():
            jar.set(name, value)
        session.cookies = jar

        resp = MagicMock()
        resp.status_code = 200
        session.request.return_value = resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/action",
            params=params,
        )

        execute_http_action(session, "http://192.168.100.1", action)

        session.request.assert_called_once_with(
            "POST",
            "http://192.168.100.1/action",
            data=expected_data,
            headers=None,
            timeout=10,
        )

    def test_no_params_no_interpolation_attempted(self) -> None:
        """Action with no params sends data=None, no cookie access."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = 200
        session.request.return_value = resp

        action = HttpAction(type="http", method="POST", endpoint="/action")

        execute_http_action(session, "http://192.168.100.1", action)

        session.request.assert_called_once_with(
            "POST",
            "http://192.168.100.1/action",
            data=None,
            headers=None,
            timeout=10,
        )


# ------------------------------------------------------------------
# Tests — HTTP auth-value endpoint interpolation
# ------------------------------------------------------------------


class TestHttpAuthEndpointInterpolation:
    """{auth:token} / {auth:user_id} resolution in an action endpoint."""

    _LOGOUT = "/rest/v1/user/{auth:user_id}/token/{auth:token}"

    # fmt: off
    INTERPOLATION_CASES = [
        # (endpoint, auth_context, expected_path, description)
        ("/logout", AuthContext(token="t"), "/logout", "no placeholders — verbatim"),
        (_LOGOUT, AuthContext(token="tok", user_id="3"), "/rest/v1/user/3/token/tok", "both keys resolved"),
        (_LOGOUT, AuthContext(token="tok"), "/rest/v1/user/{auth:user_id}/token/tok", "empty user id — left literal"),
        (_LOGOUT, None, _LOGOUT, "no authenticated session — left literal"),
        ("/x/{auth:nope}", AuthContext(token="tok"), "/x/{auth:nope}", "unknown key — left literal"),
    ]
    # fmt: on

    @pytest.mark.parametrize(
        "endpoint,auth_context,expected_path,desc",
        INTERPOLATION_CASES,
        ids=[c[3] for c in INTERPOLATION_CASES],
    )
    def test_auth_endpoint_interpolation(
        self,
        endpoint: str,
        auth_context: AuthContext | None,
        expected_path: str,
        desc: str,
    ) -> None:
        """Auth-value placeholders resolve from the session's AuthContext."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = 204
        session.request.return_value = resp

        action = HttpAction(type="http", method="DELETE", endpoint=endpoint)

        execute_http_action(session, "http://192.168.100.1", action, auth_context=auth_context)

        assert session.request.call_args[0][1] == f"http://192.168.100.1{expected_path}"

    def test_params_are_not_auth_interpolated(self) -> None:
        """Placeholders resolve in the endpoint only — params keep their own scheme."""
        session = MagicMock(spec=requests.Session)
        session.cookies = requests.cookies.RequestsCookieJar()
        resp = MagicMock()
        resp.status_code = 200
        session.request.return_value = resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/x/{auth:token}",
            params={"tok": "{auth:token}"},
        )

        execute_http_action(session, "http://192.168.100.1", action, auth_context=AuthContext(token="tok"))

        assert session.request.call_args[0][1] == "http://192.168.100.1/x/tok"
        assert session.request.call_args[1]["data"] == {"tok": "{auth:token}"}


# ------------------------------------------------------------------
# Tests — execute_action dispatch
# ------------------------------------------------------------------


class TestExecuteAction:
    """Single dispatch routing for HTTP and HNAP actions."""

    def test_http_action_dispatches(self) -> None:
        """HTTP action routes to execute_http_action with the collector's auth context."""
        collector = MagicMock()
        collector._session = MagicMock(spec=requests.Session)
        collector._base_url = "http://192.168.100.1"
        collector._auth_context = AuthContext(token="tok", user_id="3")

        modem_config = MagicMock()
        modem_config.auth = NoneAuth(strategy="none")
        modem_config.timeout = 10

        action = HttpAction(type="http", method="POST", endpoint="/restart.htm")

        with patch("solentlabs.cable_modem_monitor_core.orchestration.actions.execute_http_action") as mock_http:
            mock_http.return_value = MagicMock(success=True)
            execute_action(collector, modem_config, action)

        mock_http.assert_called_once_with(
            collector._session,
            collector._base_url,
            action,
            timeout=10,
            log_level=logging.INFO,
            model=modem_config.model,
            query_params=None,
            auth_context=collector._auth_context,
        )

    def test_hnap_action_dispatches(self) -> None:
        """HNAP action routes to execute_hnap_action with credentials."""
        collector = MagicMock()
        collector._session = MagicMock(spec=requests.Session)
        collector._base_url = "http://192.168.100.1"
        collector._auth_context = AuthContext(private_key="test_key")

        modem_config = MagicMock()
        modem_config.auth = HnapAuth(strategy="hnap", hmac_algorithm="sha256")
        modem_config.timeout = 10

        action = HnapAction(type="hnap", action_name="Reboot")

        with patch("solentlabs.cable_modem_monitor_core.orchestration.actions.execute_hnap_action") as mock_hnap:
            mock_hnap.return_value = MagicMock(success=True)
            execute_action(collector, modem_config, action)

        mock_hnap.assert_called_once()
        call_kwargs = mock_hnap.call_args
        assert call_kwargs[1]["private_key"] == "test_key"
        assert call_kwargs[1]["hmac_algorithm"] == "sha256"

    def test_hnap_no_auth_context_uses_empty_key(self) -> None:
        """HNAP dispatch with no auth context uses empty private key."""
        collector = MagicMock()
        collector._session = MagicMock(spec=requests.Session)
        collector._base_url = "http://192.168.100.1"
        collector._auth_context = None

        modem_config = MagicMock()
        modem_config.auth = HnapAuth(strategy="hnap", hmac_algorithm="md5")
        modem_config.timeout = 10

        action = HnapAction(type="hnap", action_name="Reboot")

        with patch("solentlabs.cable_modem_monitor_core.orchestration.actions.execute_hnap_action") as mock_hnap:
            mock_hnap.return_value = MagicMock(success=True)
            execute_action(collector, modem_config, action)

        assert mock_hnap.call_args[1]["private_key"] == ""

    def test_cbn_action_dispatches(self) -> None:
        """CBN action routes to execute_cbn_action with setter endpoint."""
        from solentlabs.cable_modem_monitor_core.models.modem_config.actions import (
            CbnAction,
        )

        collector = MagicMock()
        collector._session = MagicMock(spec=requests.Session)
        collector._base_url = "http://192.168.100.1"

        modem_config = MagicMock()
        modem_config.auth = MagicMock()
        modem_config.auth.setter_endpoint = "/xml/setter.xml"
        modem_config.auth.session_cookie_name = "sessionToken"
        modem_config.timeout = 10

        action = CbnAction(type="cbn", fun=8)

        with patch("solentlabs.cable_modem_monitor_core.orchestration.actions.execute_cbn_action") as mock_cbn:
            mock_cbn.return_value = MagicMock(success=True)
            execute_action(collector, modem_config, action)

        mock_cbn.assert_called_once()
        call_kwargs = mock_cbn.call_args[1]
        assert call_kwargs["setter_endpoint"] == "/xml/setter.xml"
        assert call_kwargs["session_cookie_name"] == "sessionToken"
        assert call_kwargs["timeout"] == 10

    def test_unknown_action_returns_failure(self, caplog) -> None:
        """An action object that's none of the known types returns ActionResult(success=False)."""

        class _UnknownAction:
            """Sentinel — not registered in dispatch."""

        collector = MagicMock()
        modem_config = MagicMock()
        modem_config.model = "TPS-2000"

        with caplog.at_level(logging.WARNING):
            result = execute_action(collector, modem_config, _UnknownAction())  # type: ignore[arg-type]  # _UnknownAction is intentionally off-spec to exercise the unknown-action branch

        assert result.success is False
        assert "Unknown action type" in result.message
        assert "Unknown action type" in caplog.text


# ------------------------------------------------------------------
# Tests — HttpAction json_body field
# ------------------------------------------------------------------


class TestHttpActionJsonBody:
    """json_body passes application/json body; mutually exclusive with params."""

    def test_json_body_sends_json_kwarg(self) -> None:
        """When json_body is set, session.request receives json= kwarg."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = 200
        session.request.return_value = resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/rest/v1/system/reboot",
            json_body={"reboot": {"enable": True}},
        )

        execute_http_action(session, "http://192.168.100.1", action)

        session.request.assert_called_once_with(
            "POST",
            "http://192.168.100.1/rest/v1/system/reboot",
            json={"reboot": {"enable": True}},
            headers=None,
            timeout=10,
        )

    def test_json_body_does_not_send_data_kwarg(self) -> None:
        """When json_body is set, data= kwarg is not passed."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = 200
        session.request.return_value = resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/rest/v1/system/reboot",
            json_body={"reboot": {"enable": True}},
        )

        execute_http_action(session, "http://192.168.100.1", action)

        call_kwargs = session.request.call_args[1]
        assert "data" not in call_kwargs

    def test_params_sends_data_kwarg(self) -> None:
        """When params is set (no json_body), session.request receives data= kwarg."""
        session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = 200
        session.request.return_value = resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/goform/restart",
            params={"restart": "1"},
        )

        execute_http_action(session, "http://192.168.100.1", action)

        session.request.assert_called_once_with(
            "POST",
            "http://192.168.100.1/goform/restart",
            data={"restart": "1"},
            headers=None,
            timeout=10,
        )

    def test_json_body_and_params_mutual_exclusion(self) -> None:
        """Setting both json_body and params raises a validation error."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            HttpAction(
                type="http",
                method="POST",
                endpoint="/rest/v1/system/reboot",
                params={"restart": "1"},
                json_body={"reboot": {"enable": True}},
            )


# ------------------------------------------------------------------
# Tests — execute_action with action_auth (per-action auth)
# ------------------------------------------------------------------


class TestHttpActionWithActionAuth:
    """Per-action auth: fresh session, token injected, original session untouched."""

    def _make_action(self) -> HttpAction:
        from solentlabs.cable_modem_monitor_core.models.modem_config.auth import BearerAuth

        return HttpAction(
            type="http",
            method="POST",
            endpoint="/rest/v1/system/reboot",
            json_body={"reboot": {"enable": True}},
            action_auth=BearerAuth(
                strategy="bearer",
                login_endpoint="/rest/v1/user/login",
                token_path="created.token",
            ),
        )

    def _make_collector(self, session: MagicMock) -> MagicMock:
        collector = MagicMock()
        collector._session = session
        collector._base_url = "http://192.168.100.1"
        collector._username = "admin"
        collector._password = "secret"
        return collector

    def _make_modem_config(self) -> MagicMock:
        config = MagicMock()
        config.timeout = 10
        config.model = "Hub5"
        config.session = None
        return config

    def _make_fresh_session_success(self, token: str = "tok") -> MagicMock:
        fresh = MagicMock(spec=requests.Session)
        fresh.headers = {}
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"created": {"token": token}}
        action_resp = MagicMock()
        action_resp.status_code = 200
        fresh.post.return_value = login_resp
        fresh.request.return_value = action_resp
        return fresh

    def _make_fresh_session_failure(self, status_code: int = 401) -> MagicMock:
        fresh = MagicMock(spec=requests.Session)
        fresh.headers = {}
        login_resp = MagicMock()
        login_resp.status_code = status_code
        login_resp.json.side_effect = ValueError("not json")
        fresh.post.return_value = login_resp
        return fresh

    _PATCH = "solentlabs.cable_modem_monitor_core.orchestration.actions.create_session"

    def test_original_session_not_used_for_request(self) -> None:
        """When action_auth is set, the original session is not used for the action request."""
        original_session = MagicMock(spec=requests.Session)
        original_session.headers = {}
        fresh_session = self._make_fresh_session_success()

        with patch(self._PATCH, return_value=fresh_session):
            result = execute_action(
                self._make_collector(original_session), self._make_modem_config(), self._make_action()
            )

        assert result.success is True
        original_session.request.assert_not_called()
        fresh_session.request.assert_called_once()

    def test_bearer_token_injected_into_fresh_session(self) -> None:
        """Bearer token from login is injected into the fresh session's headers."""
        original_session = MagicMock(spec=requests.Session)
        original_session.headers = {}
        fresh_session = self._make_fresh_session_success(token="tok_abc")

        with patch(self._PATCH, return_value=fresh_session):
            execute_action(self._make_collector(original_session), self._make_modem_config(), self._make_action())

        assert fresh_session.headers.get("Authorization") == "Bearer tok_abc"

    def test_original_session_headers_unchanged(self) -> None:
        """The original session's headers are not modified by per-action auth."""
        original_session = MagicMock(spec=requests.Session)
        original_headers: dict[str, str] = {}
        original_session.headers = original_headers
        fresh_session = self._make_fresh_session_success()

        with patch(self._PATCH, return_value=fresh_session):
            execute_action(self._make_collector(original_session), self._make_modem_config(), self._make_action())

        assert original_headers == {}

    def test_auth_failure_returns_action_failure(self) -> None:
        """If per-action auth fails, execute_action returns failure."""
        original_session = MagicMock(spec=requests.Session)
        original_session.headers = {}
        fresh_session = self._make_fresh_session_failure()

        with patch(self._PATCH, return_value=fresh_session):
            result = execute_action(
                self._make_collector(original_session), self._make_modem_config(), self._make_action()
            )

        assert result.success is False
        fresh_session.request.assert_not_called()

    def test_no_action_auth_uses_original_session(self) -> None:
        """Without action_auth, the original session is used for the request."""
        original_session = MagicMock(spec=requests.Session)
        resp = MagicMock()
        resp.status_code = 200
        original_session.request.return_value = resp

        action = HttpAction(
            type="http",
            method="POST",
            endpoint="/goform/restart",
        )
        collector = self._make_collector(original_session)

        execute_action(collector, self._make_modem_config(), action)

        original_session.request.assert_called_once()
