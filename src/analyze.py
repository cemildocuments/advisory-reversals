"""Deterministic empirical analysis and operational replay; writes numeric results."""

from pathlib import Path
import json, gzip, collections, datetime, math, argparse, time
import numpy as np
import pandas as pd
from versioning import version
from prepare import DAY, iso_time

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "results"


def readgz(path):
    with gzip.open(path, "rt") as f:
        return [json.loads(x) for x in f]


def load_registry():
    out = {}
    for p in sorted((ROOT / "data/registry").glob("*.json")):
        r = json.loads(p.read_text())
        times = {}
        for v, t in r.get("release_times", {}).items():
            try:
                q = version(r["ecosystem"], v)
                ts = iso_time(t)
                times[q] = min(times.get(q, float("inf")), ts)
            except (ValueError, TypeError):
                continue
        out[(r["ecosystem"], r["package"])] = (r["status"], times)
    return out


def outcome(d, cutoff):
    end = cutoff
    censor = "administrative"
    series = []
    for s in d["series"]:
        if s["time"] > cutoff:
            continue
        if s["state"] not in ("affected", "unaffected"):
            end = s["time"]
            censor = s["state"]
            break
        series.append(s)
    affected = [s for s in series if s["state"] == "affected"]
    first = affected[0] if affected else None
    row = {k: v for k, v in d.items() if k != "series"}
    row.update(
        end_time=end,
        followup_days=(end - d["decision_time"]) / DAY,
        censor=censor,
        reversed=bool(first),
        first_reversal_time=first["time"] if first else None,
        first_reversal_days=(
            (first["time"] - d["decision_time"]) / DAY if first else None
        ),
        first_reversal_commit=first["commit"] if first else "",
        first_reversal_sha=first["sha"] if first else "",
    )
    transitions = []
    prev = "unaffected"
    for s in series:
        if s["state"] != prev:
            transitions.append(s)
            prev = s["state"]
    row["state_transitions"] = len(transitions)
    row["affected_episodes"] = sum(s["state"] == "affected" for s in transitions)
    row["returned_unaffected"] = any(s["state"] == "unaffected" for s in transitions)
    row["endpoint_affected"] = prev == "affected"
    row["series"] = series
    row["transitions"] = transitions
    return row


def cluster_ci(df, outcome_col, denom_col=None, seed=260927, draws=5000):
    if len(df) == 0:
        return [None, None]
    grouped = df.groupby(["ecosystem", "package"], sort=True)[outcome_col].agg(
        ["sum", "count"]
    )
    a = grouped["sum"].to_numpy(float)
    b = grouped["count"].to_numpy(float)
    rng = np.random.default_rng(seed)
    vals = []
    for start in range(0, draws, 100):
        idx = rng.integers(0, len(a), size=(min(100, draws - start), len(a)))
        vals.extend((a[idx].sum(1) / b[idx].sum(1)).tolist())
    return np.quantile(vals, [0.025, 0.975]).tolist()


def stats(rows, label):
    df = pd.DataFrame(rows)
    if df.empty:
        return {"group": label, "decisions": 0}
    n = len(df)
    k = int(df.reversed.sum())
    ci = cluster_ci(df, "reversed")
    pos = df[df.reversed]
    return {
        "group": label,
        "decisions": n,
        "advisories": df.id.nunique(),
        "packages": df[["ecosystem", "package"]].drop_duplicates().shape[0],
        "reversals": k,
        "reversal_pct": 100 * k / n,
        "ci_low_pct": 100 * ci[0],
        "ci_high_pct": 100 * ci[1],
        "reversal_advisories": pos.id.nunique(),
        "reversal_packages": pos[["ecosystem", "package"]].drop_duplicates().shape[0],
        "returned_unaffected": int(pos.returned_unaffected.sum()),
        "endpoint_affected": int(pos.endpoint_affected.sum()),
        "median_first_reversal_days": (
            float(pos.first_reversal_days.median()) if len(pos) else None
        ),
        "median_followup_days": float(df.followup_days.median()),
        "nonadministrative_censor": int((df.censor != "administrative").sum()),
    }


def replay(d, horizon, period=None, delay=None, phase=0):
    start = d["decision_time"]
    end = min(d["end_time"], start + horizon * DAY)
    events = [s for s in d["transitions"] if s["time"] <= end]
    items = [(s["time"], 0, s["state"]) for s in events]
    if period is not None:
        p = period * DAY
        origin = phase * DAY
        nexttime = (math.floor((start - origin) / p) + 1) * p + origin
        actions = [(float(t), 1, None) for t in np.arange(nexttime, end + 1, p)]
    elif delay is not None:
        actions = [
            (s["time"] + delay, 1, s["state"])
            for s in d.get("revision_actions", d["series"][1:])
            if s["time"] + delay <= end
        ]
    else:
        actions = []
    # Copying a revision's classification at a fixed delivery lag models a FIFO event feed.
    items.extend(actions)
    items.sort(key=lambda x: (x[0], x[1]))
    truth = "unaffected"
    cached = "unaffected"
    last = start
    negative = positive = 0.0
    alerts = 0
    for t, kind, state in items + [(end, 2, None)]:
        if truth == "affected" and cached == "unaffected":
            negative += t - last
        if truth == "unaffected" and cached == "affected":
            positive += t - last
        if kind == 0:
            truth = state
        elif kind == 1:
            new = truth if state is None else state
            alerts += cached == "unaffected" and new == "affected"
            cached = new
        last = t
    return {
        "stale_unaffected_days": negative / DAY,
        "stale_affected_days": positive / DAY,
        "rechecks": len(actions),
        "alerts": alerts,
        "observed_days": (end - start) / DAY,
    }


def main():
    ap = argparse.ArgumentParser()
    args = ap.parse_args()
    started = time.perf_counter()
    manifest = json.loads((ROOT / "data/source_manifest.json").read_text())
    cutoff = iso_time(manifest["head_commit_time"])
    registry = load_registry()
    candidates = readgz(RES / "candidates.jsonl.gz")
    ex = collections.Counter()
    rows = []
    for d in candidates:
        ex["stable_candidate_decisions"] += 1
        status, times = registry.get((d["ecosystem"], d["package"]), (None, {}))
        if status != 200:
            ex["registry_unavailable"] += 1
            continue
        vv = version(d["ecosystem"], d["version"])
        if vv not in times:
            ex["release_not_found"] += 1
            continue
        if times[vv] > d["decision_time"]:
            ex["released_after_decision"] += 1
            continue
        d["release_time"] = times[vv]
        r = outcome(d, cutoff)
        rows.append(r)
    mainrows = [r for r in rows if r["initial"] and r["publication_lag_days"] <= 30]
    summary = {
        "cutoff": manifest["head_commit_time"],
        "counts": dict(ex),
        "main": stats(mainrows, "All"),
        "ecosystems": [
            stats([r for r in mainrows if r["ecosystem"] == e], e)
            for e in ("npm", "PyPI")
        ],
    }
    adv = readgz(RES / "advisories.jsonl.gz")
    a = pd.DataFrame(adv)
    ae = a[a.eligible]
    rev = readgz(RES / "revision_events.jsonl.gz")
    rv = pd.DataFrame(rev)
    summary["corpus"] = {
        "advisories": len(a),
        "snapshots": int(a.snapshots.sum()),
        "edits": int(a.edits.sum()),
        "semantic_edits": int(a.semantic_edits.sum()),
        "inception_advisories": len(ae),
        "inception_snapshots": int(ae.snapshots.sum()),
        "inception_edits": int(ae.edits.sum()),
        "inception_semantic_edits": int(ae.semantic_edits.sum()),
        "inception_with_edits": int((ae.edits > 0).sum()),
        "inception_with_semantic_edits": int((ae.semantic_edits > 0).sum()),
        "exclusions": a.exclusion.value_counts().to_dict(),
    }
    cohortrows = []
    for e in ("npm", "PyPI"):
        sub = ae[ae.ecosystems.apply(lambda x: e in x)]
        ds = [r for r in mainrows if r["ecosystem"] == e]
        cohortrows.append(
            {
                "ecosystem": e,
                "advisories": len(sub),
                "snapshots": int(sub.snapshots.sum()),
                "edits": int(sub.edits.sum()),
                "semantic_edits": int(sub.semantic_edits.sum()),
                "revised_advisories": int((sub.edits > 0).sum()),
                "semantic_revised_advisories": int((sub.semantic_edits > 0).sum()),
                "initial_decisions": len(ds),
            }
        )
    pd.DataFrame(cohortrows).to_csv(RES / "cohort_counts.csv", index=False)
    # For transparent complete-follow-up proportions, require observation through H regardless of event.
    horizons = []
    for e in ("All", "npm", "PyPI"):
        for h in (7, 30, 90, 180, 365):
            rr = [
                outcome(r, r["decision_time"] + h * DAY)
                for r in mainrows
                if r["followup_days"] >= h and (e == "All" or r["ecosystem"] == e)
            ]
            s = stats(rr, e)
            s["horizon_days"] = h
            horizons.append(s)
    pd.DataFrame(horizons).to_csv(RES / "horizon_rates.csv", index=False)
    # Descriptive Kaplan–Meier estimator (first reversal, censor at withdrawal/invalidity/end).
    km = []
    for eco in ("All", "npm", "PyPI"):
        rr = [r for r in mainrows if eco == "All" or r["ecosystem"] == eco]
        times = collections.defaultdict(lambda: [0, 0])
        for r in rr:
            t = r["first_reversal_days"] if r["reversed"] else r["followup_days"]
            times[t][0 if r["reversed"] else 1] += 1
        n = len(rr)
        surv = 1.0
        km.append(
            {"ecosystem": eco, "days": 0.0, "cumulative_reversal": 0.0, "at_risk": n}
        )
        for t, (d, c) in sorted(times.items()):
            if n:
                surv *= 1 - d / n
            km.append(
                {
                    "ecosystem": eco,
                    "days": t,
                    "cumulative_reversal": 1 - surv,
                    "at_risk": n,
                    "events": d,
                    "censored": c,
                }
            )
            n -= d + c
    pd.DataFrame(km).to_csv(RES / "time_to_reversal.csv", index=False)
    sens = []
    for lag in (7, 30, 90, None):
        for initial in (True, False):
            rr = [
                r
                for r in rows
                if (not initial or r["initial"])
                and (lag is None or r["publication_lag_days"] <= lag)
            ]
            s = stats(
                rr,
                f"lag_{lag if lag is not None else 'unrestricted'}_"
                + ("initial" if initial else "all_targets"),
            )
            s.update(lag_limit_days=lag, initial_only=initial)
            sens.append(s)
    pd.DataFrame(sens).to_csv(RES / "sensitivity.csv", index=False)
    yearly = []
    for year in range(2022, 2026):
        for eco in ("npm", "PyPI"):
            rr = [
                outcome(r, r["decision_time"] + 180 * DAY)
                for r in mainrows
                if r["followup_days"] >= 180
                and datetime.datetime.fromtimestamp(
                    r["decision_time"], datetime.timezone.utc
                ).year
                == year
                and r["ecosystem"] == eco
            ]
            s = stats(rr, eco)
            s["year"] = year
            yearly.append(s)
    pd.DataFrame(yearly).to_csv(RES / "yearly_rates.csv", index=False)
    semmap = {(r["id"], r["order"]): r["semantic"] for r in rev}
    for r in mainrows:
        r["revision_actions"] = [
            s for s in r["series"][1:] if semmap.get((r["id"], s["order"]), True)
        ]
    replayrows = []
    policyrows = []
    policies = [
        ("No recheck", None, None),
        ("Daily", 1, None),
        ("Weekly", 7, None),
        ("Monthly", 30, None),
        ("Revision + 1 h", None, 3600),
        ("Revision + 24 h", None, 86400),
        ("Revision immediate", None, 0),
    ]
    for policy, period, delay in policies:
        pr = []
        for r in mainrows:
            m = replay(r, 180, period=period, delay=delay)
            m.update(
                policy=policy,
                id=r["id"],
                ecosystem=r["ecosystem"],
                package=r["package"],
                version=r["version"],
            )
            pr.append(m)
        frame = pd.DataFrame(pr)
        policyrows.append(
            {
                "policy": policy,
                "decisions": len(pr),
                "rechecks": int(frame.rechecks.sum()),
                "alerts": int(frame.alerts.sum()),
                "stale_unaffected_days": float(frame.stale_unaffected_days.sum()),
                "stale_affected_days": float(frame.stale_affected_days.sum()),
                "decision_days": float(frame.observed_days.sum()),
            }
        )
        replayrows.extend(pr)
    # Re-evaluation workloads: every associated edit versus semantic record edits.
    allchecks = semchecks = 0
    for r in mainrows:
        end = min(r["end_time"], r["decision_time"] + 180 * DAY)
        for s in r["series"][1:]:
            if s["time"] <= end:
                allchecks += 1
                semchecks += int(semmap.get((r["id"], s["order"]), True))
    summary["workload"] = {
        "all_associated_revision_checks": allchecks,
        "semantic_associated_revision_checks": semchecks,
        "actual_status_changes": sum(
            len(
                [
                    s
                    for s in r["transitions"]
                    if s["time"] <= min(r["end_time"], r["decision_time"] + 180 * DAY)
                ]
            )
            for r in mainrows
        ),
        "horizon_days": 180,
    }
    pd.DataFrame(policyrows).to_csv(RES / "policy_replay.csv", index=False)
    pd.DataFrame(replayrows).to_csv(RES / "policy_decisions.csv.gz", index=False)
    phases = []
    for period in (1, 7, 30):
        for f in np.linspace(0, period, 24, endpoint=False):
            measures = [replay(r, 180, period=period, phase=float(f)) for r in mainrows]
            phases.append(
                {
                    "period_days": period,
                    "phase_days": float(f),
                    "stale_unaffected_days": sum(
                        m["stale_unaffected_days"] for m in measures
                    ),
                    "rechecks": sum(m["rechecks"] for m in measures),
                }
            )
    pd.DataFrame(phases).to_csv(RES / "polling_phase_sensitivity.csv", index=False)
    # Numeric outputs separate from raw histories, so independent scripts can audit cases.
    flat = [
        {
            k: v
            for k, v in r.items()
            if k not in ("series", "transitions", "revision_actions")
        }
        for r in mainrows
    ]
    pd.DataFrame(flat).to_csv(RES / "decisions.csv", index=False)
    with gzip.open(RES / "decision_histories.jsonl.gz", "wt") as f:
        for r in mainrows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    pd.DataFrame([r for r in flat if r["reversed"]]).to_csv(
        RES / "reversals.csv", index=False
    )
    (RES / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2), flush=True)
    print(
        "Analysis completed in",
        round(time.perf_counter() - started, 2),
        "seconds",
        flush=True,
    )


if __name__ == "__main__":
    main()
