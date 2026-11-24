"""X12 5010 helpers: envelopes, a tolerant segment reader, structural checks.

Separators used on write: element '*', component ':', repetition '^', segment '~'.
The reader takes its separators from the ISA header, so it also reads files
produced by other systems.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime

ELEM, COMP, REP, SEG = "*", ":", "^", "~"
_PUNCT = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def clean(value) -> str:
    """Strip characters that would break an X12 segment (the component separator is kept for composites)."""
    if value is None:
        return ""
    text = str(value).translate(_PUNCT)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")   # X12 is ASCII-only
    return text.replace(ELEM, " ").replace(SEG, " ").replace(REP, " ").strip()


def seg(*elements) -> str:
    """Build one segment; trailing empty elements are dropped as X12 requires."""
    els = [clean(e) if i else str(e) for i, e in enumerate(elements)]
    while len(els) > 1 and els[-1] == "":
        els.pop()
    return ELEM.join(els) + SEG


def money(cents: int) -> str:
    s = f"{cents / 100:.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def isa(sender: str, receiver: str, control: int, when: datetime, usage: str = "T") -> str:
    return ELEM.join([
        "ISA", "00", " " * 10, "00", " " * 10, "ZZ", sender.ljust(15)[:15], "ZZ", receiver.ljust(15)[:15],
        when.strftime("%y%m%d"), when.strftime("%H%M"), REP, "00501", f"{control:09d}", "0", usage, COMP]) + SEG


def envelope(body_segments: list[list[str]], *, sender: str, receiver: str, functional_id: str,
             version: str, control: int = 1, when: datetime | None = None) -> str:
    """Wrap one or more transaction sets (each a list of segments, ST..SE excluded) in ISA/GS/IEA."""
    when = when or datetime.now().replace(second=0, microsecond=0)
    tx_code = {"HC": "837", "HP": "835", "HR": "276", "HN": "277"}[functional_id]
    out = [isa(sender, receiver, control, when),
           seg("GS", functional_id, sender, receiver, when.strftime("%Y%m%d"), when.strftime("%H%M"), control, "X", version)]
    for i, body in enumerate(body_segments, start=1):
        st_ctl = f"{i:04d}"
        segs = [seg("ST", tx_code, st_ctl, version)] + body
        segs.append(seg("SE", len(segs) + 1, st_ctl))
        out.extend(segs)
    out.append(seg("GE", len(body_segments), control))
    out.append(seg("IEA", 1, f"{control:09d}"))
    return "\n".join(out) + "\n"


def read_segments(text: str) -> list[list[str]]:
    """Split an X12 document into segments of elements, honouring the ISA-declared separators."""
    text = text.lstrip()
    if not text.startswith("ISA"):
        raise ValueError("Not an X12 interchange (no ISA)")
    elem = text[3]
    seg_term = text[105]
    segs = []
    for raw in text.split(seg_term):
        raw = raw.strip("\r\n ")
        if raw:
            segs.append(raw.split(elem))
    return segs


def component_sep(text: str) -> str:
    return text.lstrip()[104]


def structure_errors(text: str) -> list[str]:
    """Envelope-level checks: ISA length, SE counts, GE/IEA counts and control numbers."""
    errors = []
    t = text.lstrip()
    if len(t.split(t[105])[0]) != 105:
        errors.append("ISA segment is not 106 characters")
    segs = read_segments(text)
    st_open, count, st_total, gs_ctl = None, 0, 0, None
    for s in segs:
        tag = s[0]
        if tag == "GS":
            gs_ctl = s[6]
        elif tag == "ST":
            st_open, count = s[2], 1
            st_total += 1
            continue
        if st_open is not None:
            count += 1
        if tag == "SE":
            if int(s[1]) != count:
                errors.append(f"SE count {s[1]} != actual {count} in ST {st_open}")
            if s[2] != st_open:
                errors.append(f"SE control {s[2]} != ST {st_open}")
            st_open = None
        elif tag == "GE":
            if int(s[1]) != st_total:
                errors.append(f"GE count {s[1]} != {st_total} transaction sets")
            if s[2] != gs_ctl:
                errors.append("GE control number does not match GS")
        elif tag == "IEA" and s[2] != segs[0][13]:
            errors.append("IEA control number does not match ISA")
    if st_open is not None:
        errors.append(f"ST {st_open} not closed by SE")
    return errors
