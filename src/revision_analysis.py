"""Additional frozen-data analyses of risk sets, observation loss, and delivery semantics."""

from pathlib import Path
from collections import Counter, defaultdict
import datetime as dt
import json
import math
import numpy as np
import pandas as pd
from analyze import readgz, stats, outcome, load_registry, iso_time, replay, DAY
from versioning import compile_record, signature, version, key, stable

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "results"


def decision_key(row):
    return tuple(row[k] for k in ("id", "ecosystem", "package", "version"))


def date_utc(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).date().isoformat()


def landmark_panel(rows, landmark_days):
    return [
        r
        for r in rows
        if r["followup_days"] > landmark_days
        and (not r["reversed"] or r["first_reversal_days"] > landmark_days)
    ]


def deletion_gaps(series):
    """Describe every observed deletion interval without imputing a missing state."""
    gaps = []
    for i, s in enumerate(series):
        if s["state"] != "deleted" or (i and series[i - 1]["state"] == "deleted"):
            continue
        back = next((z for z in series[i + 1 :] if z["state"] != "deleted"), None)
        previous = series[i - 1] if i else None
        delay = (back["time"] - s["time"]) / DAY if back else None
        gaps.append(
            {
                "deletion_time": s["time"],
                "deletion_date": date_utc(s["time"]),
                "deletion_commit": s["commit"],
                "previous_sha": previous["sha"] if previous else "",
                "previous_state": previous["state"] if previous else "",
                "restoration_time": back["time"] if back else None,
                "restoration_commit": back["commit"] if back else "",
                "restoration_sha": back["sha"] if back else "",
                "restoration_state": back["state"] if back else "",
                "restoration_delay_days": delay,
                "same_blob": bool(back and previous and back["sha"] == previous["sha"]),
                "same_classification": bool(
                    back and previous and back["state"] == previous["state"]
                ),
                "bridge_eligible": bool(
                    back and back["state"] in ("affected", "unaffected") and delay <= 1
                ),
            }
        )
    return gaps


def bridge_short_deletions(series):
    """Permit observation to resume; do not use the returned series to integrate time."""
    result = []
    i = 0
    while i < len(series):
        s = series[i]
        if s["state"] == "deleted":
            j = i + 1
            while j < len(series) and series[j]["state"] == "deleted":
                j += 1
            if (
                j < len(series)
                and series[j]["state"] in ("affected", "unaffected")
                and series[j]["time"] - s["time"] <= DAY
            ):
                i = j
                continue
        result.append(s)
        i += 1
    return result


def replay_notification_latest(d, horizon, delay):
    """At each delayed notification, query the current supported state; do not coalesce."""
    start = d["decision_time"]
    end = min(d["end_time"], start + horizon * DAY)
    items = [(s["time"], 0, s["state"]) for s in d["transitions"] if s["time"] <= end]
    actions = [
        (s["time"] + delay, 1, s["state"])
        for s in d["revision_actions"]
        if s["time"] + delay <= end
    ]
    items.extend(actions)
    items.sort(key=lambda x: (x[0], x[1]))
    truth = cached = "unaffected"
    last = start
    negative = positive = 0.0
    alerts = outdated = 0
    for t, kind, state in items + [(end, 2, None)]:
        if truth == "affected" and cached == "unaffected":
            negative += t - last
        if truth == "unaffected" and cached == "affected":
            positive += t - last
        if kind == 0:
            truth = state
        elif kind == 1:
            outdated += state != truth
            alerts += cached == "unaffected" and truth == "affected"
            cached = truth
        last = t
    return {
        "rechecks": len(actions),
        "alerts": alerts,
        "stale_unaffected_days": negative / DAY,
        "stale_affected_days": positive / DAY,
        "outdated_notification_payloads": outdated,
        "observed_days": (end - start) / DAY,
    }


def annotations(entries):
    values = []
    for entry in entries:
        objects = [entry] + entry.get("ranges", [])
        for obj in objects:
            text = obj.get("database_specific", {}).get(
                "last_known_affected_version_range"
            )
            if text is not None:
                values.append(text)
    return values


def simple_bound_membership(ecosystem, value, bound):
    """Audit simple upper-bound annotations only; no replacement for core OSV semantics."""
    import re

    m = re.fullmatch(r"\s*(<=|<|>=|>|==|=)\s*(\S+)\s*", bound)
    if not m:
        return None
    v, b = version(ecosystem, value), version(ecosystem, m.group(2))
    return {
        "<": v < b,
        "<=": v <= b,
        ">": v > b,
        ">=": v >= b,
        "=": v == b,
        "==": v == b,
    }[m.group(1)]


def main():
    rows = readgz(RES / "decision_histories.jsonl.gz")
    candidates = readgz(RES / "candidates.jsonl.gz")
    main_index = {decision_key(r): r for r in rows}
    full = {decision_key(r): r for r in candidates}
    blobs = {
        r["sha"]: json.loads(r["text"]) for r in readgz(ROOT / "data/blobs.jsonl.gz")
    }
    cutoff = iso_time(
        json.loads((ROOT / "data/source_manifest.json").read_text())["head_commit_time"]
    )
    registry = load_registry()
    report = {}

    # Conditional observed proportions: baseline and a prespecified-in-revision day-30 landmark.
    landmark = []
    for days in (0, 30):
        panel = landmark_panel(rows, days)
        s = stats(panel, f"Observable and unreversed at day {days}")
        s.update(
            landmark_days=days,
            median_remaining_observation_days=float(
                np.median([r["followup_days"] - days for r in panel])
            ),
            remaining_observation_q25=float(
                np.quantile([r["followup_days"] - days for r in panel], 0.25)
            ),
            remaining_observation_q75=float(
                np.quantile([r["followup_days"] - days for r in panel], 0.75)
            ),
        )
        landmark.append(s)
    pd.DataFrame(landmark).to_csv(RES / "landmark_rates.csv", index=False)
    report["landmark"] = landmark

    # Follow-up loss and already-observed events omitted by the complete-horizon rule.
    horizon = []
    for h in (7, 30, 90, 180, 365):
        included = [r for r in rows if r["followup_days"] >= h]
        lost = [r for r in rows if r["followup_days"] < h]
        reasons = Counter(r["censor"] for r in lost)
        omitted = [r for r in lost if r["reversed"] and r["first_reversal_days"] <= h]
        horizon.append(
            {
                "horizon_days": h,
                "included": len(included),
                "excluded": len(lost),
                "reversals_included": sum(
                    r["reversed"] and r["first_reversal_days"] <= h for r in included
                ),
                "reversals_excluded_after_occurrence": len(omitted),
                "deletions": reasons["deleted"],
                "withdrawals": reasons["withdrawn"],
                "absence_or_unknown": reasons["absent"] + reasons["unknown"],
                "administrative": reasons["administrative"],
                "omitted_reversal_ids": ";".join(r["id"] for r in omitted),
            }
        )
    pd.DataFrame(horizon).to_csv(RES / "horizon_attrition.csv", index=False)
    report["horizon_attrition"] = horizon

    # Withdrawal is a loss of supported interpretation, not a negative classification.
    any_withdrawal = {
        decision_key(r)
        for r in rows
        if any(s["state"] == "withdrawn" for s in full[decision_key(r)]["series"])
    }
    withdrawal = [
        stats(rows, "All primary decisions"),
        stats(
            [r for r in rows if r["censor"] != "withdrawn"],
            "Exclude first-censor withdrawals",
        ),
        stats(
            [r for r in rows if decision_key(r) not in any_withdrawal],
            "Exclude any observed withdrawal through cutoff",
        ),
    ]
    pd.DataFrame(withdrawal).to_csv(RES / "withdrawal_sensitivity.csv", index=False)
    report["withdrawal_sensitivity"] = withdrawal
    report["withdrawal_after_earlier_censor"] = len(any_withdrawal) - sum(
        r["censor"] == "withdrawn" for r in rows
    )

    gap_rows = []
    for r in rows:
        for gap in deletion_gaps(full[decision_key(r)]["series"]):
            gap.update({k: r[k] for k in ("id", "ecosystem", "package", "version")})
            gap["primary_censor"] = (
                r["censor"] == "deleted" and gap["deletion_time"] == r["end_time"]
            )
            gap_rows.append(gap)
    gaps = pd.DataFrame(gap_rows)
    gaps.to_csv(RES / "deletion_restoration_audit.csv", index=False)
    primary = gaps[gaps.primary_censor]
    calendar = []
    for day, group in primary.groupby("deletion_date", sort=True):
        calendar.append(
            {
                "date": day,
                "decisions": len(group),
                "advisories": group.id.nunique(),
                "restored_within_24h": int(group.bridge_eligible.sum()),
                "restored_later": int((group.restoration_delay_days > 1).sum()),
                "unrestored": int(group.restoration_delay_days.isna().sum()),
                "commits": ";".join(sorted(group.deletion_commit.unique())),
            }
        )
    pd.DataFrame(calendar).to_csv(RES / "deletion_calendar.csv", index=False)
    report["deletion"] = {
        "first_censors": len(primary),
        "first_censor_advisories": int(primary.id.nunique()),
        "restored_within_24h": int(primary.bridge_eligible.sum()),
        "restored_later": int((primary.restoration_delay_days > 1).sum()),
        "unrestored": int(primary.restoration_delay_days.isna().sum()),
        "same_blob_at_restoration": int(primary.same_blob.sum()),
        "same_classification_at_restoration": int(primary.same_classification.sum()),
        "all_bridge_eligible_gaps": int(gaps.bridge_eligible.sum()),
        "bridge_gaps_after_earlier_censor": int(
            (gaps.bridge_eligible & ~gaps.primary_censor).sum()
        ),
        "short_gap_seconds": sorted(
            set(
                (primary[primary.bridge_eligible].restoration_delay_days * DAY).tolist()
            )
        ),
        "restored_delay_quantiles_days": primary.restoration_delay_days.dropna()
        .quantile([0, 0.5, 0.95, 1])
        .to_dict(),
        "calendar": calendar,
    }
    bridged = [
        outcome(
            full[decision_key(r)]
            | {"series": bridge_short_deletions(full[decision_key(r)]["series"])},
            cutoff,
        )
        for r in rows
    ]
    report["deletion_reentry"] = stats(
        bridged, "Resume after deletion gaps at most 24 hours"
    )
    report["deletion_reentry"]["additional_reversal_ids"] = [
        r["id"]
        for r in bridged
        if r["reversed"] and not main_index[decision_key(r)]["reversed"]
    ]
    report["deletion_reentry"]["remaining_censors"] = dict(
        Counter(r["censor"] for r in bridged)
    )
    # Report support at the horizon without imputing the classification inside a deletion gap.
    reentry_horizons = []
    for h in (7, 30, 90, 180, 365):
        included = []
        for r in bridged:
            if r["followup_days"] < h:
                continue
            original = full[decision_key(r)]["series"]
            at_h = [s for s in original if s["time"] <= r["decision_time"] + h * DAY][
                -1
            ]
            if at_h["state"] in ("affected", "unaffected"):
                included.append(r)
        reentry_horizons.append(
            {
                "horizon_days": h,
                "observable_at_horizon_after_short_gap_reentry": len(included),
                "observed_reversals_by_horizon": sum(
                    r["reversed"] and r["first_reversal_days"] <= h for r in included
                ),
            }
        )
    pd.DataFrame(reentry_horizons).to_csv(
        RES / "reentry_horizon_counts.csv", index=False
    )
    report["reentry_horizon_counts"] = reentry_horizons

    # Structured-edit decomposition across the entire inception advisory cohort.
    revisions = readgz(RES / "revision_events.jsonl.gz")
    grouped = defaultdict(list)
    semantic_orders = defaultdict(set)
    life = Counter()
    nonlife_advisories = set()
    for e in revisions:
        if not e["eligible_advisory"]:
            continue
        grouped[e["id"]].append(e)
        if e["semantic"]:
            semantic_orders[e["id"]].add(e["order"])
        a, b = compile_record(blobs.get(e["old_sha"])), compile_record(
            blobs.get(e["new_sha"])
        )
        if a["status"] != b["status"]:
            life[f"{a['status']} -> {b['status']}"] += 1
        elif e["semantic"]:
            nonlife_advisories.add(e["id"])
    pairs = []
    short_identical_orders = defaultdict(set)
    for aid, events in sorted(grouped.items()):
        for i, e in enumerate(events):
            if e["new_sha"] != "0" * 40:
                continue
            following = next(
                (x for x in events[i + 1 :] if x["new_sha"] != "0" * 40), None
            )
            if following:
                delay = following["time"] - e["time"]
                same = e["old_sha"] == following["new_sha"]
                pairs.append(
                    {
                        "id": aid,
                        "deletion_commit": e["commit"],
                        "restoration_commit": following["commit"],
                        "delay_seconds": delay,
                        "same_blob": same,
                    }
                )
                if delay <= DAY and same:
                    short_identical_orders[aid].update([e["order"], following["order"]])
    pd.DataFrame(pairs).to_csv(RES / "lifecycle_pairs.csv", index=False)
    only_short = [
        aid
        for aid, orders in semantic_orders.items()
        if orders and orders <= short_identical_orders[aid]
    ]
    report["lifecycle"] = {
        "transitions": dict(life),
        "lifecycle_edits": sum(life.values()),
        "deletion_restoration_pairs": len(pairs),
        "pairs_within_24h": sum(p["delay_seconds"] <= DAY for p in pairs),
        "byte_identical_pairs_within_24h": sum(
            p["delay_seconds"] <= DAY and p["same_blob"] for p in pairs
        ),
        "advisories_with_nonlifecycle_semantic_edit": len(nonlife_advisories),
        "advisories_with_semantic_edits_only_short_identical_pairs": len(only_short),
    }

    # Audit excluded targets using their frozen registry and advisory observations.
    excluded = pd.read_csv(RES / "excluded_initial_decisions.csv")
    excluded_map = {decision_key(r): r["reason"] for r in excluded.to_dict("records")}
    excluded_details = []
    all_initial = []
    for d in candidates:
        if not (d["initial"] and d["publication_lag_days"] <= 30):
            continue
        r = outcome(d, cutoff)
        all_initial.append(r)
        if decision_key(d) not in excluded_map:
            continue
        state, times = registry.get((d["ecosystem"], d["package"]), (None, {}))
        v = version(d["ecosystem"], d["version"])
        later_boundary = None
        for s in d["series"][1:]:
            rec = compile_record(blobs.get(s["sha"]))
            p = rec["packages"].get((d["ecosystem"], d["package"]))
            if rec["status"] != "active" or not p or p["errors"]:
                continue
            fixed = {version(d["ecosystem"], x) for x in p["fixed"]}
            if fixed and v not in fixed:
                later_boundary = s
                break
        excluded_details.append(
            {
                k: d[k]
                for k in ("id", "ecosystem", "package", "version", "decision_time")
            }
            | {
                "reason": excluded_map[decision_key(d)],
                "reversed_under_record_only_interpretation": r["reversed"],
                "first_reversal_days": r["first_reversal_days"],
                "release_delay_days": (
                    (times[v] - d["decision_time"]) / DAY if v in times else None
                ),
                "later_fixed_boundary_replacement": later_boundary is not None,
                "replacement_commit": (
                    later_boundary["commit"] if later_boundary else ""
                ),
                "replacement_delay_days": (
                    (later_boundary["time"] - d["decision_time"]) / DAY
                    if later_boundary
                    else None
                ),
            }
        )
    exframe = pd.DataFrame(excluded_details)
    exframe.to_csv(RES / "registry_exclusion_audit.csv", index=False)
    report["registry_exclusions"] = []
    for reason, g in exframe.groupby("reason", sort=True):
        report["registry_exclusions"].append(
            {
                "reason": reason,
                "decisions": len(g),
                "record_only_reversals": int(
                    g.reversed_under_record_only_interpretation.sum()
                ),
                "later_boundary_replacements": int(
                    g.later_fixed_boundary_replacement.sum()
                ),
                "median_release_delay_days": (
                    float(g.release_delay_days.dropna().median())
                    if g.release_delay_days.notna().any()
                    else None
                ),
                "released_within_30_days": int(
                    ((g.release_delay_days > 0) & (g.release_delay_days <= 30)).sum()
                ),
            }
        )
    report["record_only_sensitivity"] = stats(
        all_initial, "All stable initial fixed boundaries without release validation"
    )

    # Evidence status is separate from a causal mechanism taxonomy.
    cases = pd.read_csv(RES / "case_traces.csv")
    evidence = []
    for r in cases.to_dict("records"):
        old, new = json.loads(r["old_affected"]), json.loads(r["new_affected"])
        a, b = annotations(old), annotations(new)
        conflict = bool(b) and any(
            simple_bound_membership(r["ecosystem"], r["version"], s) is False for s in b
        )
        main = main_index[decision_key(r)]
        status = (
            "Documented core/annotation conflict"
            if conflict
            else (
                "Subsequently withdrawn claim"
                if main["censor"] == "withdrawn"
                else "Other range revision; causal mechanism not adjudicated"
            )
        )
        evidence.append(
            {
                k: r[k]
                for k in (
                    "id",
                    "ecosystem",
                    "package",
                    "version",
                    "old_commit",
                    "new_commit",
                )
            }
            | {
                "annotation_anywhere_in_supported_history": r[
                    "custom_annotation_in_history"
                ],
                "annotation_at_transition": bool(a or b),
                "annotations_before": json.dumps(a),
                "annotations_after": json.dumps(b),
                "new_annotation_excludes_core_affected_target": conflict,
                "withdrawn_in_primary_followup": main["censor"] == "withdrawn",
                "evidence_status": status,
            }
        )
    ev = pd.DataFrame(evidence)
    ev.to_csv(RES / "reversal_evidence_audit.csv", index=False)
    report["reversal_evidence"] = {
        "status_counts": dict(Counter(ev.evidence_status)),
        "annotation_bearing_histories": int(
            ev.annotation_anywhere_in_supported_history.sum()
        ),
        "annotation_at_transition": int(ev.annotation_at_transition.sum()),
        "annotation_elsewhere_only": int(
            (
                ev.annotation_anywhere_in_supported_history
                & ~ev.annotation_at_transition
            ).sum()
        ),
        "direct_transition_conflicts": int(
            ev.new_annotation_excludes_core_affected_target.sum()
        ),
    }

    # Event-free risk sets and complete-horizon year summaries.
    risk = []
    for eco in ("All", "npm", "PyPI"):
        panel = [r for r in rows if eco == "All" or r["ecosystem"] == eco]
        for t in (0, 7, 30, 90, 180, 365, 730, 1095):
            risk.append(
                {
                    "ecosystem": eco,
                    "day": t,
                    "at_risk": sum(
                        r["followup_days"] >= t
                        and (not r["reversed"] or r["first_reversal_days"] >= t)
                        for r in panel
                    ),
                    "under_observation_including_reversed": sum(
                        r["followup_days"] >= t for r in panel
                    ),
                }
            )
    pd.DataFrame(risk).to_csv(RES / "risk_sets.csv", index=False)
    yearly = []
    for year in range(2022, 2026):
        rr = [
            r
            for r in rows
            if dt.datetime.fromtimestamp(r["decision_time"], dt.timezone.utc).year
            == year
        ]
        h = [r for r in rr if r["followup_days"] >= 180]
        y = {
            "year": year,
            "initial_decisions": len(rr),
            "complete_180_decisions": len(h),
            "complete_180_reversals": sum(
                r["reversed"] and r["first_reversal_days"] <= 180 for r in h
            ),
        }
        for eco in ("npm", "PyPI"):
            p = [r for r in h if r["ecosystem"] == eco]
            y[eco + "_decisions"] = len(p)
            y[eco + "_reversals"] = sum(
                r["reversed"] and r["first_reversal_days"] <= 180 for r in p
            )
        yearly.append(y)
    pd.DataFrame(yearly).to_csv(RES / "yearly_cohort_summary.csv", index=False)
    report["yearly_cohorts"] = yearly

    # Separate payload-delivery semantics from query-on-notification semantics.
    policy = []
    detail = []
    for delay in (3600, 86400):
        for mode in ("Historical payload", "Query latest at notification"):
            measures = []
            for r in rows:
                m = (
                    replay(r, 180, delay=delay)
                    if mode == "Historical payload"
                    else replay_notification_latest(r, 180, delay)
                )
                measures.append(m)
                detail.append(
                    {k: r[k] for k in ("id", "ecosystem", "package", "version")}
                    | m
                    | {"mode": mode, "delay_hours": delay / 3600}
                )
            f = pd.DataFrame(measures)
            policy.append(
                {
                    "mode": mode,
                    "delay_hours": delay / 3600,
                    "decisions": len(rows),
                    "rechecks": int(f.rechecks.sum()),
                    "alerts": int(f.alerts.sum()),
                    "stale_unaffected_days": float(f.stale_unaffected_days.sum()),
                    "stale_affected_days": float(f.stale_affected_days.sum()),
                    "outdated_notification_payloads": (
                        int(f.outdated_notification_payloads.sum())
                        if "outdated_notification_payloads" in f
                        else None
                    ),
                }
            )
    pd.DataFrame(policy).to_csv(RES / "delivery_semantics.csv", index=False)
    pd.DataFrame(detail).to_csv(
        RES / "delivery_semantics_decisions.csv.gz", index=False
    )
    report["delivery_semantics"] = policy
    phase = pd.read_csv(RES / "polling_phase_sensitivity.csv")
    report["polling_phase_summary"] = (
        phase.groupby("period_days")
        .stale_unaffected_days.agg(["min", "mean", "median", "max"])
        .to_dict("index")
    )
    (RES / "revision_summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(report, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
