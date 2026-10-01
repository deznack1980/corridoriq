"""Internal Arizona ROC identity enrichment.

Does not change opportunity_score, customer_relevance_score, or
account_priority_score. Ranking consumption stays disabled.
"""

from pipeline.config.settings import ROC_MODEL_VERSION

__all__ = ["ROC_MODEL_VERSION"]
