"""OSV numbered-version predicates for npm and PyPI.

Intervals use inclusive lower endpoints and inclusive/exclusive upper endpoints.
Unsupported or malformed records are explicitly unknown. No lexical version ordering.
"""

from dataclasses import dataclass
from functools import lru_cache
from packaging.version import Version as PyVersion
from packaging.utils import canonicalize_name
from semantic_version import Version as SemVersion


@lru_cache(maxsize=200000)
def version(ecosystem, text):
    text = str(text)
    if ecosystem == "npm":
        v = SemVersion(text)
        # Build metadata has no effect on SemVer precedence.
        return SemVersion(
            major=v.major, minor=v.minor, patch=v.patch, prerelease=v.prerelease
        )
    if ecosystem == "PyPI":
        return PyVersion(text)
    raise ValueError("unsupported ecosystem " + ecosystem)


def stable(eco, text):
    v = version(eco, text)
    return not (
        bool(v.prerelease) if eco == "npm" else (v.is_prerelease or v.is_devrelease)
    )


def name(eco, n):
    return canonicalize_name(n or "") if eco == "PyPI" else (n or "")


def key(a):
    p = a.get("package", {})
    return p.get("ecosystem", ""), name(p.get("ecosystem", ""), p.get("name", ""))


@dataclass(frozen=True)
class Interval:
    low: object
    high: object
    closed: bool = False

    def has(self, v):
        return (self.low is None or v >= self.low) and (
            self.high is None or v < self.high or (self.closed and v == self.high)
        )

    def serial(self):
        return (
            str(self.low) if self.low is not None else None,
            str(self.high) if self.high is not None else None,
            self.closed,
        )


def merge_intervals(intervals):
    out = []
    for b in sorted(intervals, key=lambda x: (x.low is not None, x.low)):
        if (
            b.low is not None
            and b.high is not None
            and (b.low > b.high or (b.low == b.high and not b.closed))
        ):
            continue
        if not out:
            out.append(b)
            continue
        a = out[-1]
        if a.high is None:
            continue
        if b.low is None or b.low <= a.high:
            # b includes its lower endpoint, so equality yields a contiguous union.
            if b.high is None or b.high > a.high:
                out[-1] = Interval(a.low, b.high, b.closed)
            elif b.high == a.high:
                out[-1] = Interval(a.low, a.high, a.closed or b.closed)
        else:
            out.append(b)
    return tuple(out)


def compile_entries(eco, entries):
    intervals = []
    fixed = set()
    errors = []
    if any(not key(a)[1] for a in entries):
        errors.append("missing package name")
    for a in entries:
        if not a.get("ranges") and not a.get("versions"):
            errors.append("empty predicate")
        for s in a.get("versions", []):
            try:
                v = version(eco, s)
                intervals.append(Interval(v, v, True))
            except (ValueError, TypeError):
                errors.append("invalid explicit version: " + str(s))
        for r in a.get("ranges", []):
            if r.get("type") not in ("SEMVER", "ECOSYSTEM"):
                errors.append("unsupported range type: " + str(r.get("type")))
                continue
            try:
                events = []
                for e in r.get("events", []):
                    if len(e) != 1:
                        raise ValueError("non-singleton event")
                    typ, txt = next(iter(e.items()))
                    if typ not in ("introduced", "fixed", "last_affected"):
                        raise ValueError("unsupported event: " + typ)
                    val = (
                        None
                        if typ == "introduced" and txt == "0"
                        else version(eco, txt)
                    )
                    events.append((val, typ))
                    if typ == "fixed":
                        fixed.add(str(val))
                if not any(t == "introduced" for v, t in events):
                    raise ValueError("no introduction")
                if {"fixed", "last_affected"} <= {t for v, t in events}:
                    raise ValueError("mixed fixed and last_affected")
                # Same-version conflicting boundaries are ambiguous and rejected.
                seen = {}
                for v, t in events:
                    if v in seen and seen[v] != t:
                        raise ValueError("conflicting same-version events")
                    seen[v] = t
                events = sorted(set(events), key=lambda x: (x[0] is not None, x[0]))
                active = False
                low = None
                for val, typ in events:
                    if typ == "introduced":
                        if not active:
                            low = val
                            active = True
                    elif active:
                        intervals.append(Interval(low, val, typ == "last_affected"))
                        active = False
                if active:
                    intervals.append(Interval(low, None))
            except (ValueError, TypeError) as e:
                errors.append(str(e))
    return {
        "intervals": merge_intervals(intervals),
        "fixed": tuple(sorted(fixed)),
        "errors": tuple(sorted(set(errors))),
    }


def compile_record(obj):
    if obj is None:
        return {"status": "deleted", "packages": {}}
    packages = {}
    for a in obj.get("affected", []):
        eco, n = key(a)
        if eco in ("npm", "PyPI"):
            packages.setdefault((eco, n), []).append(a)
    return {
        "status": "withdrawn" if obj.get("withdrawn") else "active",
        "packages": {k: compile_entries(k[0], a) for k, a in packages.items()},
    }


def membership(rec, k, v):
    if rec["status"] != "active":
        return rec["status"]
    if k not in rec["packages"]:
        return "absent"
    p = rec["packages"][k]
    if p["errors"]:
        return "unknown"
    v = version(k[0], v)
    return "affected" if any(i.has(v) for i in p["intervals"]) else "unaffected"


def signature(rec):
    return (
        rec["status"],
        tuple(
            (k, v["intervals"], v["errors"]) for k, v in sorted(rec["packages"].items())
        ),
    )


def direct_membership(obj, k, txt):
    """Independent state-machine evaluation of raw OSV events for validation.

    Returns None for unsupported/malformed input; caller compares valid predicates.
    """
    if obj is None:
        return "deleted"
    if obj.get("withdrawn"):
        return "withdrawn"
    entries = [a for a in obj.get("affected", []) if key(a) == k]
    if not entries:
        return "absent"
    v = version(k[0], txt)
    matched = False
    for a in entries:
        for s in a.get("versions", []):
            if v == version(k[0], s):
                matched = True
        for r in a.get("ranges", []):
            if r.get("type") not in ("SEMVER", "ECOSYSTEM"):
                return None
            ev = []
            for e in r["events"]:
                if len(e) != 1:
                    return None
                typ, s = next(iter(e.items()))
                if typ not in ("introduced", "fixed", "last_affected"):
                    return None
                q = None if s == "0" and typ == "introduced" else version(k[0], s)
                ev.append((q, typ))
            on = False
            for q, typ in sorted(ev, key=lambda t: (t[0] is not None, t[0])):
                if typ == "introduced" and (q is None or v >= q):
                    on = True
                elif typ == "fixed" and v >= q:
                    on = False
                elif typ == "last_affected" and v > q:
                    on = False
            matched = matched or on
    return "affected" if matched else "unaffected"
