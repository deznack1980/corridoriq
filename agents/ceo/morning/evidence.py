"""Moved to pipeline.trust.evidence; the CEO agent and the portal share one trust layer."""

import sys

from pipeline.trust import evidence as _impl

sys.modules[__name__] = _impl
