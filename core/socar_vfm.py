"""Backward-compatible alias (older pickles / scripts). Use ``core.vfm`` and ``core.preprocessing``."""
from .preprocessing import qc_tests  # noqa: F401
from .vfm import GasLiftVFM, TrendModel, select_trend, gilbert_base, subcrit_base  # noqa: F401
