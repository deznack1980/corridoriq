"""Moved to pipeline.trust.contract; the CEO agent and the portal share one trust layer."""

import sys

from pipeline.trust import contract as _impl

sys.modules[__name__] = _impl
