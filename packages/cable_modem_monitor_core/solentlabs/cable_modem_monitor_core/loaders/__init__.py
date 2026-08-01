"""Resource loaders — fetch data from modems.

HTTP loader for standard web interfaces, HNAP loader for SOAP,
CBN loader for Compal XML POST.

HNAP and CBN loaders are lazy-imported by the collector to avoid
pulling transport-specific dependencies (HMAC signing, defusedxml)
into every consumer of this package.

See RESOURCE_LOADING_SPEC.md.
"""

from __future__ import annotations

from ..fetch_list import ResourceTarget, collect_fetch_targets
from .http import HTTPResourceLoader, LoginPageDetectedError, ResourceLoadError

__all__ = [
    "HTTPResourceLoader",
    "LoginPageDetectedError",
    "ResourceLoadError",
    "ResourceTarget",
    "collect_fetch_targets",
]
