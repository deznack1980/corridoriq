"""Moved to pipeline.trust.scope; the CEO agent and the portal share one trust layer."""

import sys

from pipeline.trust import scope as _impl

sys.modules[__name__] = _impl
