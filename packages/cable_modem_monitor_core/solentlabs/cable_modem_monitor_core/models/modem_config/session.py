"""Session configuration for modem.yaml.

Per MODEM_YAML_SPEC.md Session section.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SessionConfig(BaseModel):
    """Post-login session lifecycle.

    Session owns concurrency limits, static request headers, and
    post-login lifecycle calls. Cookie names and token prefixes live
    on the auth strategy — auth owns the cookie it produces. See
    ARCHITECTURE_DECISIONS.md "Session is lifecycle, auth owns the
    cookie."
    """

    model_config = ConfigDict(extra="forbid")
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict)
    post_login_endpoints: list[str] = Field(default_factory=list)

    def resolved_headers(self, *, base_url: str) -> dict[str, str]:
        """Return headers with ``{base_url}`` placeholder substituted.

        Some modems' AJAX endpoints validate Referer/Origin against the
        modem's own URL. Since base_url is per-deployment (each user has
        a different host), the YAML uses ``{base_url}`` as a placeholder
        that resolves at session-build time.
        """
        return {k: v.replace("{base_url}", base_url) for k, v in self.headers.items()}
