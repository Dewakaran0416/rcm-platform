"""Reference data: payers, procedure/diagnosis code sets, adjustment codes.

Descriptions are short plain-language summaries written for this project,
not the official code-set descriptors. Timely-filing and appeal limits are
typical defaults for simulation only; real values belong in the payer TFL
tracker and must be verified per contract.
"""
from .payers import LOB_MIX, PAYERS, PAYER_BEHAVIOR
from .codes import CPT, ICD10, CARC, RARC, BUNDLING_PAIRS, POS, SPECIALTIES, STATUS_CATEGORY, cpt_info
from .carc_map import CARC_TO_DRIVER, driver_for, is_denial

__all__ = [
    "LOB_MIX", "PAYERS", "PAYER_BEHAVIOR", "CPT", "ICD10", "CARC", "RARC",
    "BUNDLING_PAIRS", "POS", "SPECIALTIES", "STATUS_CATEGORY", "cpt_info",
    "CARC_TO_DRIVER", "driver_for", "is_denial",
]
