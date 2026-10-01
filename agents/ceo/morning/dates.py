"""Moved to pipeline.trust.dates; the CEO agent and the portal share one trust layer."""

import sys

from pipeline.trust import dates as _impl

sys.modules[__name__] = _impl
