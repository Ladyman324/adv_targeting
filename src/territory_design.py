"""Where should seven wholesalers live, and what should each cover?

This is a facility-location question, not a map-colouring one. The inputs are
where the addressable opportunity actually sits; the outputs are seven home
bases and a state-by-state territory assignment.

WHAT COUNTS AS OPPORTUNITY
Not advisor headcount. A one-person Edward Jones office and a $2B RIA team are
one pin each, and treating them alike would pull every home base toward
wherever retail brokerage is densest. The weight used here is the firm's
RELEVANT AUM -- equities plus funds/ETFs, the two buckets EIC's products
actually compete for -- allocated evenly across that firm's mapped placements,
and counted only at firms reporting Item 5.G(7), which is the population that
hires outside managers at all. That is the same definition the map's default
filter uses, so the analysis and the tool agree.

METHOD
  1. Weighted k-means (k=7) over placement coordinates, many restarts.
  2. Snap each centroid to the nearest real metro -- a centroid is a point in
     a field, and nobody lives at a weighted mean.
  3. Assign WHOLE STATES to the nearest centre. Splitting a state across two
     wholesalers is operationally messy and makes commission attribution
     ambiguous, so the territory unit stays the state.
  4. Report the opportunity balance, and compare against the current seven.

WHAT THIS DOES NOT MODEL
Travel time. k-means minimises straight-line distance, but a wholesaler's real
cost is trips and connections, so an airline hub can beat a geometrically
central town. Alaska and Hawaii are excluded from the clustering -- eleven
timezone-hours of Pacific would drag a centre into the ocean -- and appended to
whichever territory takes the west coast. Treat the output as a strong prior to
argue with, not an answer.
"""
from __future__ import annotations

import json
import math
import pathlib

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
WEB = ROOT / "webapp" / "data"

K = 7
RESTARTS = 40
SEED = 20260804
OFFSHORE = {"AK", "HI", "PR", "VI", "GU"}

# EIC's current assignment, for comparison
CURRENT = {
    "West": "AK AZ CA HI ID MT NV OR UT WA WY",
    "Southwest": "AR CO KS LA NM OK TX",
    "Midwest": "IA IL IN MI MN MO ND NE OH SD WI",
    "Southeast": "AL GA KY MS NC SC TN VA WV",
    "Mid-Atlantic": "DC DE MD NJ NY PA",
    "Northeast": "CT MA ME NH RI VT",
    "Florida/PR": "FL PR",
}


def load_points() -> pd.DataFrame:
    """One row per mapped placement: coordinates, state, and opportunity weight."""
    profiles = json.loads((WEB / "firm_profiles.json").read_text(encoding="utf-8"))["profiles"]
    # relevant AUM per placement, for firms that hire outside managers
    per_placement = {}
    for crd, p in profiles.items():
        if not p.get("selects"):
            continue
        placements = p.get("mapped_placements") or 0
        relevant = (p.get("equity_implied") or 0) + (p.get("fund_implied") or 0)
        if placements > 0 and relevant > 0:
            per_placement[crd] = relevant / placements

    rows = []
    for path in sorted(WEB.glob("pins_??.json")):
        state = path.stem.split("_")[1]
        layer = json.loads(path.read_text(encoding="utf-8"))
        crds = [str(firm[7]) for firm in layer["firms"]]
        for pin in layer["pins"]:
            weight = per_placement.get(crds[pin[2]])
            if weight:
                rows.append((pin[0], pin[1], state, weight))
    return pd.DataFrame(rows, columns=["lon", "lat", "state", "weight"])


def project(lon, lat, lat0):
    """Equirectangular. Degrees of longitude shrink with latitude, so treating
    lon/lat as a plane would stretch the north and bias every centre."""
    return np.column_stack([lon * math.cos(math.radians(lat0)), lat])


def weighted_kmeans(xy, w, k, restarts, seed):
    rng = np.random.default_rng(seed)
    best, best_inertia = None, np.inf
    for _ in range(restarts):
        # k-means++ style seeding, weighted: far-and-heavy points get picked
        centres = [xy[rng.choice(len(xy), p=w / w.sum())]]
        for _ in range(k - 1):
            d2 = np.min(((xy[:, None, :] - np.array(centres)[None]) ** 2).sum(-1), axis=1)
            prob = d2 * w
            centres.append(xy[rng.choice(len(xy), p=prob / prob.sum())])
        centres = np.array(centres)
        for _ in range(120):
            d2 = ((xy[:, None, :] - centres[None]) ** 2).sum(-1)
            labels = d2.argmin(1)
            moved = False
            for i in range(k):
                m = labels == i
                if not m.any():
                    continue
                new = (xy[m] * w[m, None]).sum(0) / w[m].sum()
                if not np.allclose(new, centres[i]):
                    centres[i], moved = new, True
            if not moved:
                break
        inertia = float((((xy - centres[labels]) ** 2).sum(-1) * w).sum())
        if inertia < best_inertia:
            best, best_inertia = (centres.copy(), labels.copy()), inertia
    return best


def nearest_metro(lon, lat, cities, min_advisors=400):
    """A centroid is a point in a field. Snap to the nearest place of real size."""
    best, best_d = None, np.inf
    for city, entries in cities.items():
        for state, clat, clon, n in entries:
            if n < min_advisors or state in OFFSHORE:
                continue
            d = (clat - lat) ** 2 + ((clon - lon) * math.cos(math.radians(lat))) ** 2
            if d < best_d:
                best, best_d = (city.title(), state, clat, clon, n), d
    return best


def balanced_assign(state_pts, centres, lat0, tolerance=1.06, rounds=25):
    """Assign whole states to centres with an equal-opportunity cap.

    Plain k-means minimises distance and ignores workload, which on this data
    hands one wholesaler 34% of the relevant AUM and another 2.9%. A territory
    is a person's year, so capacity is the binding constraint and distance is
    what you optimise subject to it. Each territory may hold at most
    `tolerance` x an equal share; states take their nearest centre that still
    has room, and centres are recomputed between rounds.
    """
    sxy = project(state_pts["lon"].to_numpy(), state_pts["lat"].to_numpy(), lat0)
    weights = state_pts["weight"].to_numpy()
    cap = weights.sum() / len(centres) * tolerance
    assign = None
    for _ in range(rounds):
        d2 = ((sxy[:, None, :] - centres[None]) ** 2).sum(-1)
        order = np.argsort(-weights)          # place the heavy states first
        load = np.zeros(len(centres))
        assign = np.full(len(state_pts), -1)
        for i in order:
            for c in np.argsort(d2[i]):
                if load[c] + weights[i] <= cap:
                    assign[i], load[c] = c, load[c] + weights[i]
                    break
            else:                              # nothing has room: least overloaded
                c = int(np.argmin(load + d2[i] * 0))
                assign[i], load[c] = c, load[c] + weights[i]
        new = centres.copy()
        for c in range(len(centres)):
            m = assign == c
            if m.any():
                new[c] = (sxy[m] * weights[m, None]).sum(0) / weights[m].sum()
        if np.allclose(new, centres):
            break
        centres = new
    return assign, centres


def report(title, state_pts, assign, centres, lat0, cities, offshore_here, K):
    state_pts = state_pts.copy()
    state_pts["cluster"] = assign
    west = int(state_pts.loc[state_pts["state"] == "CA", "cluster"].iloc[0])
    total = state_pts["weight"].sum()
    print()
    print(title)
    print(f"{'BASE':<26}{'STATES':<46}{'REL. AUM':>10}{'SHARE':>8}{'PINS':>9}")
    print("-" * 99)
    rows = []
    for c in range(K):
        members = state_pts[state_pts["cluster"] == c].sort_values("weight", ascending=False)
        if not len(members):
            continue
        states = list(members["state"]) + (offshore_here if c == west else [])
        metro = nearest_metro(centres[c][0] / math.cos(math.radians(lat0)), centres[c][1], cities)
        base = f"{metro[0]}, {metro[1]}" if metro else "-"
        share = members["weight"].sum() / total * 100
        rows.append(share)
        print(f"{base:<26}{' '.join(states):<46}"
              f"{members['weight'].sum() / 1e9:>9.0f}B{share:>7.1f}%{int(members['pins'].sum()):>9,}")
    print(f"Balance: largest {max(rows):.1f}%, smallest {min(rows):.1f}% "
          f"(ratio {max(rows) / min(rows):.2f}x)")


def main() -> None:
    points = load_points()
    onshore = points[~points["state"].isin(OFFSHORE)].copy()
    lat0 = float(onshore["lat"].mean())
    xy = project(onshore["lon"].to_numpy(), onshore["lat"].to_numpy(), lat0)
    w = onshore["weight"].to_numpy()

    print(f"{len(points):,} mapped placements at 5.G(7) firms "
          f"({len(onshore):,} onshore) | ${points['weight'].sum() / 1e12:.2f}T relevant AUM")

    centres, labels = weighted_kmeans(xy, w, K, RESTARTS, SEED)
    onshore["cluster"] = labels

    cities = json.loads((WEB / "geo_index.json").read_text(encoding="utf-8"))["cities"]

    # whole states to the nearest centre, weighted by where the state's own
    # opportunity actually sits
    state_pts = onshore.groupby("state").apply(
        lambda d: pd.Series({
            "lon": np.average(d["lon"], weights=d["weight"]),
            "lat": np.average(d["lat"], weights=d["weight"]),
            "weight": d["weight"].sum(),
            "pins": len(d),
        })).reset_index()
    sxy = project(state_pts["lon"].to_numpy(), state_pts["lat"].to_numpy(), lat0)
    state_pts["cluster"] = ((sxy[:, None, :] - centres[None]) ** 2).sum(-1).argmin(1)

    # offshore states join whichever territory owns the west coast
    offshore_here = sorted(set(points["state"]) & OFFSHORE - {"PR"})
    fl = state_pts.loc[state_pts["state"] == "FL", "cluster"]

    report("A. DISTANCE-OPTIMAL (k-means; workload ignored)",
           state_pts, state_pts["cluster"].to_numpy(), centres, lat0,
           cities, offshore_here, K)

    balanced, bal_centres = balanced_assign(state_pts, centres.copy(), lat0)
    report("B. OPPORTUNITY-BALANCED (equal books, distance minimised within that)",
           state_pts, balanced, bal_centres, lat0, cities, offshore_here, K)

    cur = {}
    for name, states in CURRENT.items():
        members = state_pts[state_pts["state"].isin(states.split())]
        cur[name] = members["weight"].sum()
    cur_total = sum(cur.values())
    print(f"\nCurrent seven, same measure:")
    for name, value in sorted(cur.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<16}{value / 1e9:>8.0f}B{value / cur_total * 100:>7.1f}%")
    print(f"  ratio largest:smallest {max(cur.values()) / min(cur.values()):.2f}x")


if __name__ == "__main__":
    main()
