"""Audit source traces and prespecified/exploratory robustness summaries."""

from pathlib import Path
import json, gzip, collections, datetime
import pandas as pd
from analyze import readgz, stats, outcome, load_registry, version, iso_time, DAY
from versioning import key, compile_record, membership, signature

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "results"


def main():
    blobs = {
        r["sha"]: json.loads(r["text"]) for r in readgz(ROOT / "data/blobs.jsonl.gz")
    }
    rows = readgz(RES / "decision_histories.jsonl.gz")
    candidates = readgz(RES / "candidates.jsonl.gz")
    registry = load_registry()
    cutoff = iso_time(
        json.loads((ROOT / "data/source_manifest.json").read_text())["head_commit_time"]
    )
    out = {}
    flow = collections.Counter()
    excluded = []
    for d in candidates:
        if not (d["initial"] and d["publication_lag_days"] <= 30):
            continue
        flow["stable_initial_candidates"] += 1
        status, times = registry.get((d["ecosystem"], d["package"]), (None, {}))
        v = version(d["ecosystem"], d["version"])
        if status != 200:
            reason = "registry_unavailable"
        elif v not in times:
            reason = "release_not_found"
        elif times[v] > d["decision_time"]:
            reason = "released_after_decision"
        else:
            reason = "included"
        flow[reason] += 1
        if reason != "included":
            excluded.append(
                {k: x for k, x in d.items() if k != "series"} | {"reason": reason}
            )
    out["main_registry_flow"] = dict(flow)
    pd.DataFrame(excluded).to_csv(RES / "excluded_initial_decisions.csv", index=False)
    cases = []
    clean = []
    positive_ids = set()
    durations = []
    gaps = []
    for d in rows:
        custom = False
        for s in d["series"]:
            o = blobs[s["sha"]]
            for a in o.get("affected", []):
                if key(a) != (d["ecosystem"], d["package"]):
                    continue
                custom |= "last_known_affected_version_range" in a.get(
                    "database_specific", {}
                )
                custom |= any(
                    "last_known_affected_version_range"
                    in r.get("database_specific", {})
                    for r in a.get("ranges", [])
                )
        if not custom:
            clean.append(d)
        prev = d["series"][0]
        for i, s in enumerate(d["series"][1:], 1):
            if s["state"] == "affected" and prev["state"] == "unaffected":
                old = blobs[prev["sha"]]
                new = blobs[s["sha"]]
                ks = (d["ecosystem"], d["package"])
                later = next(
                    (z for z in d["series"][i + 1 :] if z["state"] == "unaffected"),
                    None,
                )
                end = later["time"] if later else d["end_time"]
                duration = (end - s["time"]) / DAY
                cases.append(
                    {
                        "id": d["id"],
                        "ecosystem": d["ecosystem"],
                        "package": d["package"],
                        "version": d["version"],
                        "initial_time": d["decision_time"],
                        "reversal_time": s["time"],
                        "latency_days": (s["time"] - d["decision_time"]) / DAY,
                        "affected_duration_days": duration,
                        "returned_unaffected": bool(later),
                        "custom_annotation_in_history": custom,
                        "old_commit": prev["commit"],
                        "new_commit": s["commit"],
                        "old_sha": prev["sha"],
                        "new_sha": s["sha"],
                        "old_affected": json.dumps(
                            [a for a in old.get("affected", []) if key(a) == ks],
                            sort_keys=True,
                        ),
                        "new_affected": json.dumps(
                            [a for a in new.get("affected", []) if key(a) == ks],
                            sort_keys=True,
                        ),
                        "summary": new.get("summary"),
                        "aliases": json.dumps(new.get("aliases", [])),
                        "references": json.dumps(new.get("references", [])),
                    }
                )
                durations.append(duration)
                positive_ids.add(d["id"])
            prev = s
    out["no_custom_annotation_sensitivity"] = stats(
        clean, "No custom range annotation in observed package history"
    )
    out["censor_reasons"] = dict(collections.Counter(r["censor"] for r in rows))
    out["reversal_episodes"] = {
        "count": len(durations),
        "at_least_1_day": sum(x >= 1 for x in durations),
        "at_least_7_days": sum(x >= 7 for x in durations),
        "median_observed_affected_days": float(pd.Series(durations).median()),
    }
    out["reversal_latency"] = {
        str(h): sum(r["reversed"] and r["first_reversal_days"] <= h for r in rows)
        for h in [1, 7, 30, 90, 180, 365]
    }
    out["reversal_latency"]["max_days"] = max(
        (r["first_reversal_days"] for r in rows if r["reversed"]), default=None
    )
    out["initial_followup_days"] = (
        pd.Series([r["followup_days"] for r in rows])
        .describe(percentiles=[0.25, 0.5, 0.75])
        .to_dict()
    )
    # Advisory-level denominator: any released initial decision in that advisory.
    groups = collections.defaultdict(list)
    for r in rows:
        groups[r["id"]].append(r)
    out["advisory_level"] = {
        "advisories": len(groups),
        "reversed": sum(any(r["reversed"] for r in g) for g in groups.values()),
    }
    # Exploratory: bridge only brief repository deletions; retain other censoring boundaries.
    mainindex = {(r["id"], r["ecosystem"], r["package"], r["version"]) for r in rows}
    bridged = []
    bridgecount = 0
    for d in candidates:
        if (d["id"], d["ecosystem"], d["package"], d["version"]) not in mainindex:
            continue
        ser = d["series"]
        new = []
        i = 0
        while i < len(ser):
            s = ser[i]
            if s["state"] == "deleted":
                j = i + 1
                while j < len(ser) and ser[j]["state"] == "deleted":
                    j += 1
                if (
                    j < len(ser)
                    and ser[j]["state"] in ("unaffected", "affected")
                    and ser[j]["time"] - s["time"] <= DAY
                ):
                    bridgecount += 1
                    i = j
                    continue
            new.append(s)
            i += 1
        bridged.append(outcome(d | {"series": new}, cutoff))
    out["brief_deletion_reentry_sensitivity"] = {
        "bridged_gaps": bridgecount,
        "decisions": len(bridged),
        "reversals": sum(r["reversed"] for r in bridged),
    }
    # Semantic filter validation over every observable follow-up snapshot.
    events = readgz(RES / "revision_events.jsonl.gz")
    semmap = {(r["id"], r["order"]): r["semantic"] for r in events}
    checked = changes = 0
    for r in rows:
        prev = r["series"][0]["state"]
        for s in r["series"][1:]:
            checked += 1
            if s["state"] != prev:
                assert semmap[(r["id"], s["order"])]
                changes += 1
            prev = s["state"]
    out["semantic_filter_validation"] = {
        "followup_snapshot_checks": checked,
        "state_changes": changes,
        "missed_changes": 0,
    }
    breakdown = collections.Counter()
    for e in events:
        if not e["eligible_advisory"]:
            continue
        a = compile_record(blobs.get(e["old_sha"]))
        b = compile_record(blobs.get(e["new_sha"]))
        if not e["semantic"]:
            kind = "Other record content"
        elif a["status"] != b["status"]:
            kind = "Record lifecycle"
        elif a["packages"].keys() != b["packages"].keys():
            kind = "Package identity or presence"
        elif any(
            a["packages"][k]["errors"] != b["packages"][k]["errors"]
            for k in a["packages"]
        ):
            kind = "Predicate support or validity"
        else:
            kind = "Version predicate"
        breakdown[kind] += 1
    out["inception_revision_types"] = dict(breakdown)
    pd.DataFrame(
        [{"revision_type": k, "edits": v} for k, v in sorted(breakdown.items())]
    ).to_csv(RES / "revision_types.csv", index=False)
    phase = pd.read_csv(RES / "polling_phase_sensitivity.csv")
    out["polling_phase_ranges"] = (
        phase.groupby("period_days")
        .stale_unaffected_days.agg(["min", "median", "max"])
        .to_dict("index")
    )
    pd.DataFrame(cases).to_csv(RES / "case_traces.csv", index=False)
    (RES / "audit_summary.json").write_text(json.dumps(out, indent=2, sort_keys=True))
    print(json.dumps(out, indent=2), flush=True)


if __name__ == "__main__":
    main()
