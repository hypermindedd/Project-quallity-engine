"""Analyzers. Importing this package registers every built-in analyzer.

Each module is self-contained (input: AnalysisContext, output: AnalyzerResult)
so any of them can be lifted into its own skill/package later.
"""

from . import (  # noqa: F401 - imported for registration side effects
    accessibility,
    animation,
    architecture,
    consistency,
    interaction,
    layout,
    performance,
    rtl,
    visual_critic,
    voice,
)
from .base import AnalysisContext, all_analyzers, run_safely

__all__ = ["AnalysisContext", "all_analyzers", "run_safely"]
