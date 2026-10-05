"""Governed, read-only analytics for the CorridorIQ CEO agent."""

from agents.ceo.analytics.answer import answer_question
from agents.ceo.analytics.brief import build_daily_brief

__all__ = ["answer_question", "build_daily_brief"]
