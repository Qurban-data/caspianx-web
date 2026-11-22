"""Backward-compatible alias for the v3 workflow; the code lives in preprocessing / vfm / pta / exporters."""
from .preprocessing import (FEAT, flowing_mask, daily_features_from_hf, daily_choke, prepare_daily,  # noqa: F401
                            detect_events_hf, event_series_hf, qc_tests)
from .vfm import GasLiftVFM, run_vfm  # noqa: F401
from .pta import pvt_for, q_tp_from_vfm, pta_event, truncate_at_reopen  # noqa: F401
from .exporters import (VFM_COLS, PTA_COLS, PTA_OFFICIAL, vfm_excel_table, pta_excel_table,  # noqa: F401
                        pta_official_table, to_excel_bytes)
