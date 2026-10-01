"""Moved to pipeline.trust.health; the CEO agent and the portal share one trust layer."""

import sys

from pipeline.trust import health as _impl

sys.modules[__name__] = _impl
