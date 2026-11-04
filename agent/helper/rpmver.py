"""RPM version comparison, so a newer installed build counts as the fix Inspector asks for.

Inspector's fixedInVersion and Patch Manager's patch titles carry
[epoch:]version-release. Comparison follows rpm's rpmvercmp: segments of
digits or letters, digits newer than letters, '~' sorting before anything.
"""

import re
from typing import Optional, Tuple

_SEGMENT = re.compile(r"(~|\d+|[a-zA-Z]+)")


def _vercmp(a: str, b: str) -> int:
    if a == b:
        return 0
    sa, sb = _SEGMENT.findall(a), _SEGMENT.findall(b)
    for x, y in zip(sa, sb):
        if x == y:
            continue
        if x == "~" or y == "~":
            return -1 if x == "~" else 1
        if x.isdigit() and y.isdigit():
            return (int(x) > int(y)) - (int(x) < int(y))
        if x.isdigit() != y.isdigit():
            return 1 if x.isdigit() else -1
        return (x > y) - (x < y)
    if len(sa) == len(sb):
        return 0
    longer, sign = (sa, 1) if len(sa) > len(sb) else (sb, -1)
    # A trailing '~' segment makes the longer one older (1.0~rc1 < 1.0).
    return -sign if longer[min(len(sa), len(sb))] == "~" else sign


def _split(evr: str) -> Tuple[int, str, str]:
    epoch, _, rest = evr.rpartition(":") if ":" in evr else ("0", "", evr)
    version, _, release = rest.partition("-")
    return int(epoch or 0) if epoch.isdigit() else 0, version, release


def compare(a: str, b: str) -> int:
    """-1, 0 or 1 as [epoch:]version-release a is older than, equal to or newer than b."""
    ea, va, ra = _split(a)
    eb, vb, rb = _split(b)
    if ea != eb:
        return 1 if ea > eb else -1
    return _vercmp(va, vb) or _vercmp(ra, rb)


def installed_version(title: str, name_arch: str) -> Optional[str]:
    """The version in a patch title 'name.arch:[epoch:]version-release' for this package, else None."""
    prefix = f"{name_arch}:"
    return title[len(prefix):] if title.startswith(prefix) else None


def at_least(installed: str, fixed: str) -> bool:
    try:
        return compare(installed, fixed) >= 0
    except ValueError:
        return installed == fixed
