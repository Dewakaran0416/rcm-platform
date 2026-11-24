"""Map Claim Adjustment Reason Codes to the denial driver they signal.

This is what lets the scrubber and recovery modules score themselves on real
835 data, where there is no answer key: the payer's CARC says why a line was
denied. The groupings follow common RCM practice; extend them as you see new
codes in a client's remits.
"""

DRIVER_CARCS = {
    "eligibility":       {"27", "26", "31", "32", "33", "177", "200", "B9", "109"},
    "no_auth":           {"197", "198", "15", "62", "39", "243", "288"},
    "timely_filing":     {"29"},
    "bundling":          {"97", "231", "234", "236", "B15", "B20", "4"},
    "medical_necessity": {"50", "11", "55", "56", "57", "150", "151", "152", "167", "40", "58"},
    "duplicate":         {"18"},
    "missing_info":      {"16", "125", "252", "206", "A1", "5", "6", "7", "8", "9", "10", "146", "181", "182", "189"},
    "non_covered":       {"96", "204", "49", "119", "35", "B7", "B5", "170", "54", "185"},
    "coordination":      {"22", "23", "24", "B13"},
}

CARC_TO_DRIVER = {carc: drv for drv, carcs in DRIVER_CARCS.items() for carc in carcs}

# Adjustments that are not denials: contractual write-down and patient cost share
NOT_A_DENIAL = {("CO", "45"), ("PR", "1"), ("PR", "2"), ("PR", "3"), ("CO", "253"), ("OA", "45")}
PATIENT_SHARE_CARCS = {"1", "2", "3"}


def driver_for(carc: str | None) -> str:
    return CARC_TO_DRIVER.get((carc or "").strip().upper(), "other")


def is_denial(group: str, carc: str) -> bool:
    return (group, carc) not in NOT_A_DENIAL
