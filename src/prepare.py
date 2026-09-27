"""Prepare historical advisory cohorts and candidate remediation decisions."""

import json, gzip, hashlib, collections, datetime, concurrent.futures, argparse
from pathlib import Path
from versioning import (
    compile_record,
    membership,
    signature,
    stable,
    version,
    direct_membership,
)

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400
START = int(datetime.datetime(2022, 6, 1, tzinfo=datetime.timezone.utc).timestamp())
END = int(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc).timestamp())


def iso_time(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load_history():
    blobs = {}
    with gzip.open(ROOT / "data/blobs.jsonl.gz", "rt") as f:
        for line in f:
            o = json.loads(line)
            b = o["text"].encode()
            assert (
                hashlib.sha1(b"blob " + str(len(b)).encode() + b"\0" + b).hexdigest()
                == o["sha"]
            )
            blobs[o["sha"]] = json.loads(o["text"])
    groups = collections.defaultdict(list)
    with gzip.open(ROOT / "data/events.jsonl.gz", "rt") as f:
        for line in f:
            r = json.loads(line)
            groups[(r["id"], r["order"])].append(r)
    histories = collections.defaultdict(list)
    conflicts = []
    for (aid, order), rows in sorted(groups.items(), key=lambda x: x[0][1]):
        present = [r for r in rows if r["new"] != "0" * 40]
        if len({r["new"] for r in present}) > 1:
            conflicts.append((aid, order))
        r = present[-1] if present else rows[-1]
        sha = r["new"]
        o = blobs[sha] if sha != "0" * 40 else None
        histories[aid].append(
            {
                "order": order,
                "commit": r["commit"],
                "time": r["time"],
                "sha": sha,
                "path": r["path"],
                "record": o,
            }
        )
    assert not conflicts, conflicts[:10]
    return histories


def process_one(item):
    aid, h = item
    counts = collections.Counter()
    first = h[0]
    firstobj = first["record"]
    out = []
    changes = []
    if firstobj is None:
        return [], [], {"id": aid, "exclusion": "no_initial_record"}, dict(counts)
    published = iso_time(firstobj.get("published", firstobj.get("modified")))
    lag = (first["time"] - published) / DAY
    eligible = START <= first["time"] < END and 0 <= lag <= 30
    exclusion = (
        "eligible"
        if eligible
        else (
            "outside_inception_window"
            if not START <= first["time"] < END
            else "publication_lag"
        )
    )
    states = []
    prev = None
    lastt = first["time"]
    semantic = 0
    for i, s in enumerate(h):
        rec = compile_record(s["record"])
        sig = signature(rec)
        if s["time"] < lastt:
            counts["clock_regressions"] += 1
        t = max(s["time"], lastt)
        lastt = t
        states.append((s, rec, t))
        if prev is not None:
            changed = sig != signature(prev)
            semantic += changed
            changes.append(
                {
                    "id": aid,
                    "commit": s["commit"],
                    "time": t,
                    "order": s["order"],
                    "semantic": changed,
                    "status": rec["status"],
                    "old_sha": h[i - 1]["sha"],
                    "new_sha": s["sha"],
                    "eligible_advisory": eligible,
                }
            )
        prev = rec
        if any(p["errors"] for p in rec["packages"].values()):
            counts["unknown_snapshots"] += 1
    base = {
        "id": aid,
        "first_time": first["time"],
        "published": published,
        "publication_lag_days": lag,
        "snapshots": len(h),
        "edits": len(h) - 1,
        "semantic_edits": semantic,
        "eligible": eligible,
        "exclusion": exclusion,
        "ecosystems": sorted({k[0] for s, r, t in states for k in r["packages"]}),
    }
    if not (START <= first["time"] < END and lag >= 0):
        return [], changes, base, dict(counts)
    seen = set()
    for j, (s, rec, t) in enumerate(states):
        if rec["status"] != "active":
            continue
        for k, p in rec["packages"].items():
            for v in p["fixed"]:
                if (k, version(k[0], v)) in seen:
                    continue
                seen.add((k, version(k[0], v)))
                counts["fixed_candidates"] += 1
                if not stable(k[0], v):
                    counts["prerelease_targets"] += 1
                    continue
                if membership(rec, k, v) != "unaffected":
                    counts["initial_not_unaffected"] += 1
                    continue
                series = []
                for hs, hr, ht in states[j:]:
                    state = membership(hr, k, v)
                    if state in ("unaffected", "affected"):
                        oracle = direct_membership(hs["record"], k, v)
                        assert state == oracle, (aid, k, v, state, oracle)
                        counts["independent_membership_checks"] += 1
                    series.append(
                        {
                            "time": ht,
                            "order": hs["order"],
                            "state": state,
                            "commit": hs["commit"],
                            "sha": hs["sha"],
                        }
                    )
                out.append(
                    {
                        "id": aid,
                        "ecosystem": k[0],
                        "package": k[1],
                        "version": v,
                        "decision_time": t,
                        "decision_order": s["order"],
                        "initial": j == 0,
                        "publication_lag_days": lag,
                        "initial_sha": s["sha"],
                        "initial_commit": s["commit"],
                        "series": series,
                    }
                )
    return out, changes, base, dict(counts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    histories = load_history()
    print("loaded", len(histories), "advisories", flush=True)
    result = ROOT / "results"
    result.mkdir(exist_ok=True)
    counts = collections.Counter()
    n = 0
    packages = set()
    with gzip.open(result / "candidates.jsonl.gz", "wt") as f, gzip.open(
        result / "revision_events.jsonl.gz", "wt"
    ) as g, gzip.open(result / "advisories.jsonl.gz", "wt") as a:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
            for decisions, changes, base, c in pool.map(
                process_one, sorted(histories.items()), chunksize=40
            ):
                counts.update(c)
                a.write(json.dumps(base, sort_keys=True) + "\n")
                for r in decisions:
                    f.write(json.dumps(r, sort_keys=True) + "\n")
                    n += 1
                    packages.add((r["ecosystem"], r["package"]))
                for r in changes:
                    g.write(json.dumps(r, sort_keys=True) + "\n")
    (result / "preparation_counts.json").write_text(
        json.dumps(dict(counts), indent=2, sort_keys=True)
    )
    (result / "registry_packages.json").write_text(
        json.dumps(sorted(packages), indent=2)
    )
    print(
        "prepared",
        n,
        "candidate decisions",
        len(packages),
        "packages",
        dict(counts),
        flush=True,
    )


if __name__ == "__main__":
    main()
