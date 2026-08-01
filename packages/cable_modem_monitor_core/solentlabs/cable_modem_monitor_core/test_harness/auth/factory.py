"""Auth handler factory — dynamic dispatch on ``auth.strategy``.

Resolves the declared strategy literal from modem.yaml to the
corresponding handler module via ``importlib``.  Each handler module
provides a ``create_handler(modem_config, har_entries)`` entry point.

**Validation coverage:** The catalog fleet test
(``test_modems.py``) auto-discovers every modem with a HAR fixture
and runs ``run_modem_test_orchestrated``, which exercises this
factory for each modem's auth strategy. A missing or misnamed
handler module fails the fleet test at CI time.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .base import AuthHandler, extract_action_config

if TYPE_CHECKING:
    from ...models.modem_config import ModemConfig

_HANDLER_PACKAGE = "solentlabs.cable_modem_monitor_core.test_harness.auth"


def create_auth_handler(
    modem_config: ModemConfig | None,
    har_entries: list[dict[str, Any]] | None = None,
) -> AuthHandler:
    """Create the appropriate auth handler from modem config.

    Dynamically imports the handler module matching the strategy
    literal (e.g., ``"form_nonce"`` → ``test_harness.auth.form_nonce``)
    and calls its ``create_handler()`` entry point.

    Args:
        modem_config: Validated ``ModemConfig`` instance (or None for no auth).
            Uses ``auth.strategy`` to select the handler.
        har_entries: HAR ``log.entries`` list. Required for HNAP auth
            (merged data response) and CBN auth (getter dispatch).
            Ignored for other strategies.

    Returns:
        Auth handler instance.
    """
    if modem_config is None or modem_config.auth is None:
        # An omitted auth block is valid (ch8978e has none) and says
        # nothing about actions, so a config that declares them still
        # gets them matched.
        no_auth = AuthHandler()
        if modem_config is not None:
            no_auth.configure_actions(extract_action_config(modem_config))
        return no_auth

    strategy = modem_config.auth.strategy

    try:
        module = importlib.import_module(f".{strategy}", package=_HANDLER_PACKAGE)
    except ModuleNotFoundError as exc:
        # Falling back to no-auth here would let a HAR replay pass while
        # simulating the wrong auth behaviour. A strategy declared as
        # auth.strategy always needs a handler, so a missing one is a gap
        # to surface, not a runtime condition to absorb. Mirrors
        # create_auth_manager_for_action in Core's runtime factory.
        raise ModuleNotFoundError(
            f"No test-harness auth handler for strategy '{strategy}'. "
            f"Add test_harness/auth/{strategy}.py with a create_handler() entry point."
        ) from exc

    handler: AuthHandler = module.create_handler(modem_config, har_entries)
    # Action matching is uniform across strategies and driven by the
    # declared actions block, so it is configured here rather than in
    # each handler module.
    handler.configure_actions(extract_action_config(modem_config))
    return handler
