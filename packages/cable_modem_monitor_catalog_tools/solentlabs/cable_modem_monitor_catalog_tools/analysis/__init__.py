"""HAR analysis phases for modem onboarding.

Pipeline phases:

- Phase 1: ``transport`` - HNAP vs HTTP detection
- Phase 2: ``auth`` - auth strategy detection (HTTP / HNAP)
- Phase 3: ``session`` - cookie, header, and token detection (HTTP only)
- Phase 4: ``actions`` - logout / restart action detection (HTTP / HNAP)
- Phase 5: ``format`` - data page format classification (HTTP / HNAP)
- Phase 6: ``mapping`` - field mapping, channel type, filters, system_info

Post-analysis:

- ``js_endpoints`` - uncaptured JS endpoint discovery
- ``request_requirements`` - session-level query param detection
- ``unread_resources`` - captured JSON endpoints no config section reads

Pipeline-wide types (``CoreGap``, ``FleetPatterns``) live in ``types``.
Phase-local result types live with their phase (e.g., ``auth/types.py``,
``actions/types.py``, ``format/types.py``, ``mapping/types.py``).
"""

from .actions.types import ActionDetail, ActionsDetail
from .auth.types import AuthDetail

__all__ = ["ActionDetail", "ActionsDetail", "AuthDetail"]
