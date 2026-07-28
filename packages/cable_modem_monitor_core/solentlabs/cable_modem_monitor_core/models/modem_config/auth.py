"""Auth strategy models for modem.yaml.

Ten strategies as a discriminated union on the 'strategy' field.
Each model carries ``display_name``, ``transport``, and ``stateless``
ClassVars so display labels, transport validation sets, factory
dispatch, login-page detection, and the published constraint tables
can derive from the models themselves.

Per MODEM_YAML_SPEC.md Auth section and ARCHITECTURE_DECISIONS.md
constraint model.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal, NamedTuple, get_args

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator


class AuthStrategyBase(BaseModel):
    """Base for all auth strategy models.

    Carries self-describing ClassVars so display labels, transport
    validation sets, login-page detection, and factory dispatch can
    derive from the models. ``stateless`` means the strategy
    establishes no server-side session. See ARCHITECTURE_DECISIONS.md
    § How to add an auth strategy.
    """

    display_name: ClassVar[str]
    transport: ClassVar[str]
    stateless: ClassVar[bool]


class NoneAuth(AuthStrategyBase):
    """No authentication required."""

    model_config = ConfigDict(extra="forbid")
    strategy: Literal["none"]

    display_name: ClassVar[str] = "No Authentication"
    transport: ClassVar[str] = "http"
    stateless: ClassVar[bool] = True


class BasicAuth(AuthStrategyBase):
    """HTTP Basic Authentication."""

    model_config = ConfigDict(extra="forbid")
    strategy: Literal["basic"]
    challenge_cookie: bool = False
    cookie_name: str = ""

    display_name: ClassVar[str] = "Basic Authentication"
    transport: ClassVar[str] = "http"
    stateless: ClassVar[bool] = True


class FormSuccess(BaseModel):
    """Success detection for form auth."""

    model_config = ConfigDict(extra="forbid")
    redirect: str = ""
    indicator: str = ""


class FormAuth(AuthStrategyBase):
    """HTML form POST login."""

    model_config = ConfigDict(extra="forbid")
    strategy: Literal["form"]
    action: str
    method: str = "POST"
    username_field: str = "username"
    password_field: list[str] = Field(default=["password"])
    encoding: Literal["plain", "base64"] = "plain"
    cookie_name: str = ""
    hidden_fields: dict[str, str] = Field(default_factory=dict)
    login_page: str = ""
    form_selector: str = ""
    success: FormSuccess | None = None

    display_name: ClassVar[str] = "Form Login"
    transport: ClassVar[str] = "http"
    stateless: ClassVar[bool] = False

    @field_validator("password_field", mode="before")
    @classmethod
    def _normalize_password_field(cls, v: str | list[str]) -> list[str]:
        """Accept a single string or a list — normalize to list."""
        if isinstance(v, str):
            return [v]
        return v


class FormNonceAuth(AuthStrategyBase):
    """Form POST with client-generated nonce."""

    model_config = ConfigDict(extra="forbid")
    strategy: Literal["form_nonce"]
    action: str
    username_field: str = "username"
    password_field: str = "password"
    nonce_field: str
    nonce_length: int = 8
    cookie_name: str = ""
    success_prefix: str = "Url:"
    error_prefix: str = "Error:"
    credential_encoding: Literal["plain", "b64_packed"] = "plain"
    credential_field: str = ""

    display_name: ClassVar[str] = "Form Login (Nonce)"
    transport: ClassVar[str] = "http"
    stateless: ClassVar[bool] = False


class UrlTokenAuth(AuthStrategyBase):
    """Credentials encoded in URL query string."""

    model_config = ConfigDict(extra="forbid")
    strategy: Literal["url_token"]
    login_page: str
    login_prefix: str = ""
    success_indicator: str = ""
    ajax_login: bool = False
    auth_header_data: bool = False
    cookie_name: str = ""
    token_prefix: str = ""
    inject_credential_cookie: bool = False

    display_name: ClassVar[str] = "URL Token"
    transport: ClassVar[str] = "http"
    stateless: ClassVar[bool] = False


class HnapAuth(AuthStrategyBase):
    """HNAP HMAC challenge-response authentication."""

    model_config = ConfigDict(extra="forbid")
    strategy: Literal["hnap"]
    hmac_algorithm: Literal["md5", "sha256"]

    display_name: ClassVar[str] = "HNAP"
    transport: ClassVar[str] = "hnap"
    stateless: ClassVar[bool] = False


class FormPbkdf2Auth(AuthStrategyBase):
    """Multi-round-trip PBKDF2 challenge-response auth."""

    model_config = ConfigDict(extra="forbid")
    strategy: Literal["form_pbkdf2"]
    login_endpoint: str
    salt_trigger: str = "seeksalthash"
    pbkdf2_iterations: int
    pbkdf2_key_length: int
    double_hash: bool = True
    csrf_init_endpoint: str = ""
    csrf_header: str = ""
    cookie_name: str = ""
    login_success: dict[str, Any] = Field(default_factory=dict)

    display_name: ClassVar[str] = "Form Login (PBKDF2)"
    transport: ClassVar[str] = "http"
    stateless: ClassVar[bool] = False


class FormSjclAuth(AuthStrategyBase):
    """SJCL (Stanford JavaScript Crypto Library) AES-CCM encrypted form auth.

    Some modem firmwares use the SJCL JavaScript library to encrypt
    credentials client-side with AES-CCM before POSTing. The server
    response is also encrypted — must decrypt to extract the CSRF
    nonce for subsequent requests. Key is derived via PBKDF2 from
    the password and a per-session salt provided by the server.

    Requires the ``cryptography`` package: install Core with the
    ``[sjcl]`` extra.
    """

    model_config = ConfigDict(extra="forbid")
    strategy: Literal["form_sjcl"]
    login_page: str = "/"
    login_endpoint: str
    session_validation_endpoint: str = ""
    pbkdf2_iterations: int
    pbkdf2_key_length: int
    ccm_tag_length: int = 16
    encrypt_aad: str = "loginPassword"
    decrypt_aad: str = "nonce"
    csrf_header: str = ""
    cookie_name: str = ""

    display_name: ClassVar[str] = "Form Login (SJCL)"
    transport: ClassVar[str] = "http"
    stateless: ClassVar[bool] = False


class FormCbnAuth(AuthStrategyBase):
    """CBN (Compal Broadband Networks) AES-256-CBC encrypted form auth.

    Compal modem firmwares use the CryptoJS library to encrypt the
    password client-side. The AES key and IV are derived from a
    rotating session token cookie. The login POST goes to a
    ``setter.xml`` endpoint with ``fun=N`` parameters (same XML POST
    pattern used for data fetching and actions).

    Requires the ``cryptography`` package: install Core with the
    ``[cbn]`` extra.
    """

    model_config = ConfigDict(extra="forbid")
    strategy: Literal["form_cbn"]
    login_page: str = "/common_page/login.html"
    getter_endpoint: str = "/xml/getter.xml"
    setter_endpoint: str = "/xml/setter.xml"
    session_cookie_name: str = "sessionToken"
    sid_cookie_name: str = "SID"
    username_value: str = "NULL"
    login_fun: int = 15

    display_name: ClassVar[str] = "Form Login CBN"
    transport: ClassVar[str] = "cbn"
    stateless: ClassVar[bool] = False


class BearerAuth(AuthStrategyBase):
    """Bearer token auth for REST JSON APIs (RFC 6750). See MODEM_YAML_SPEC.md § bearer."""

    model_config = ConfigDict(extra="forbid")
    strategy: Literal["bearer"]
    login_endpoint: str
    token_path: str

    display_name: ClassVar[str] = "Bearer Token"
    transport: ClassVar[str] = "http"
    stateless: ClassVar[bool] = False


AuthConfig = Annotated[
    Annotated[BasicAuth, Tag("basic")]
    | Annotated[BearerAuth, Tag("bearer")]
    | Annotated[FormAuth, Tag("form")]
    | Annotated[FormCbnAuth, Tag("form_cbn")]
    | Annotated[FormNonceAuth, Tag("form_nonce")]
    | Annotated[FormPbkdf2Auth, Tag("form_pbkdf2")]
    | Annotated[FormSjclAuth, Tag("form_sjcl")]
    | Annotated[HnapAuth, Tag("hnap")]
    | Annotated[NoneAuth, Tag("none")]
    | Annotated[UrlTokenAuth, Tag("url_token")],
    Discriminator("strategy"),
]

# ---------------------------------------------------------------------------
# Registry — co-located with the AuthConfig union.  Adding a new
# strategy requires adding the model class here AND to the union above.
# Both lists are sorted alphabetically by strategy literal.
# ---------------------------------------------------------------------------

_AUTH_MODELS: list[type[AuthStrategyBase]] = [
    BasicAuth,
    BearerAuth,
    FormAuth,
    FormCbnAuth,
    FormNonceAuth,
    FormPbkdf2Auth,
    FormSjclAuth,
    HnapAuth,
    NoneAuth,
    UrlTokenAuth,
]


class AuthStrategyRow(NamedTuple):
    """One strategy's self-description, read straight off its ClassVars."""

    strategy: str
    display_name: str
    transport: str
    stateless: bool


def get_auth_strategy_rows() -> list[AuthStrategyRow]:
    """Return every strategy's constraint row, sorted by strategy literal.

    The single authoritative source for the transport → auth
    constraint tables published in ARCHITECTURE.md and
    MODEM_YAML_SPEC.md; those tables are generated from this, not
    hand-maintained. See ``scripts/generate_constraint_tables.py``.
    """
    return sorted(
        AuthStrategyRow(
            strategy=get_args(m.model_fields["strategy"].annotation)[0],
            display_name=m.display_name,
            transport=m.transport,
            stateless=m.stateless,
        )
        for m in _AUTH_MODELS
    )


def get_strategy_display_labels() -> dict[str, str]:
    """Return ``{strategy_literal: display_name}`` for all auth models.

    Used by config flow to build human-readable variant dropdown labels.
    Replaces the hand-maintained ``AUTH_STRATEGY_LABELS`` dict.
    """
    return {row.strategy: row.display_name for row in get_auth_strategy_rows()}


def get_transport_strategy_sets() -> dict[str, frozenset[str]]:
    """Return ``{transport: frozenset(strategies)}`` derived from ClassVars.

    Formalises the transport → auth constraint table from
    ARCHITECTURE.md.  Used by ``ModemConfig`` validation to ensure
    the declared auth strategy is valid for the declared transport.
    """
    groups: dict[str, set[str]] = {}
    for row in get_auth_strategy_rows():
        groups.setdefault(row.transport, set()).add(row.strategy)
    return {k: frozenset(v) for k, v in groups.items()}


# Backward-compatible module-level names consumed by config.py validation.
_transport_sets = get_transport_strategy_sets()
HTTP_AUTH_STRATEGIES: frozenset[str] = _transport_sets["http"]
HNAP_AUTH_STRATEGIES: frozenset[str] = _transport_sets["hnap"]
CBN_AUTH_STRATEGIES: frozenset[str] = _transport_sets["cbn"]
