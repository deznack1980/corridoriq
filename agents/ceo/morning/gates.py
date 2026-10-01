"""Moved to pipeline.trust.gates; the CEO agent and the portal share one trust layer."""

import sys

from pipeline.trust import gates as _impl

sys.modules[__name__] = _impl
