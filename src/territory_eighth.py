"""Add an eighth wholesaler without moving the seven already in post.

The seven existing bases are FIXED at where those people actually live, not at
the optimiser's ideal metros. Relocating a household is not a territory
decision, so the only free variables are (a) which metro the eighth person
lives in and (b) how the fifty states redistribute across eight patches.

Objective and constraints are inherited from territory_bases: minimise
opportunity-weighted travel, subject to an effort band and to territories being
contiguous apart from at most three single-state fly-ins. With eight people the
fair share falls from 14.3% to 12.5%.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from territory_design import load_points, OFFSHORE, CURRENT
import territory_bases as tb

# Where the current seven actually live
INCUMBENTS = [
    ("Steve Halley",    "Redwood City, CA",   37.4999, -122.2419),
    ("Tate Lambeth",    "Dallas, TX",         32.8533,  -96.8073),
    ("Steve Zimmerman", "West Bloomfield, MI", 42.5418,  -83.3678),
    ("Matt Keeter",     "Atlanta, GA",        33.8637,  -84.3772),
    ("Keith Telesca",   "Villanova, PA",      40.0351,  -75.3438),
    ("Dennis McKinney", "Milton, MA",         42.2654,  -71.0516),
    ("Sam Borland",     "Boynton Beach, FL",  26.5286,  -80.1163),
]
K = 8


def main() -> None:
    points = load_points()
    onshore = points[~points["state"].isin(OFFSHORE)]
    sf = tb.state_frame(onshore)
    states = list(sf["state"])
    weights = sf["weight"].to_numpy()
    total = weights.sum()

    candidates = tb.candidate_cities(onshore, n=tb.TOP_CITIES)
    fixed = np.array([[lat, lon] for _, _, lat, lon in INCUMBENTS])
    dist_fixed = np.column_stack([
        tb.miles(lat, lon, sf["lat"].values, sf["lon"].values) for lat, lon in fixed])

    rng = np.random.default_rng(tb.SEED)
    best = (np.inf, None, None)
    for cand in candidates.itertuples():
        # skip a candidate that is effectively an existing base
        if min(tb.miles(cand.lat, cand.lon, [f[0]], [f[1]])[0] for f in fixed) < 120:
            continue
        d = np.column_stack([
            dist_fixed, tb.miles(cand.lat, cand.lon, sf["lat"].values, sf["lon"].values)])
        assign, cost = tb.assign_states(d, weights, K, states, rng, tries=40, max_passes=6)
        if cost < best[0]:
            best = (cost, cand, assign)

    cost, city, assign = best
    _, effort = tb.score(assign, states, weights,
                         np.column_stack([dist_fixed,
                                          tb.miles(city.lat, city.lon,
                                                   sf["lat"].values, sf["lon"].values)]), K)

    names = [n for n, _, _, _ in INCUMBENTS] + [f"NEW HIRE"]
    homes = [h for _, h, _, _ in INCUMBENTS] + [f"{city.city}, {city.state}"]
    d_all = np.column_stack([dist_fixed,
                             tb.miles(city.lat, city.lon, sf["lat"].values, sf["lon"].values)])

    offshore_here = sorted(set(points["state"]) & OFFSHORE - {"PR"})
    ca = int(assign[states.index("CA")])
    fl = int(assign[states.index("FL")])

    print(f"EIGHTH BASE: {city.city}, {city.state}   "
          f"(${city.metro_weight / 1e9:,.0f}B within 60 miles)")
    print(f"weighted mean {cost:.0f} miles to opportunity\n")
    print(f"{'WHO':<17}{'HOME':<20}{'STATES':<42}{'AUM':>8}{'SHARE':>7}{'MI':>6}")
    print("-" * 100)
    for c in range(K):
        m = assign == c
        group = sorted(states[i] for i in np.where(m)[0])
        extra = (offshore_here if c == ca else []) + (["PR"] if c == fl else [])
        w = weights[m]
        mean_mi = (d_all[m, c] * w).sum() / w.sum()
        print(f"{names[c]:<17}{homes[c]:<20}{' '.join(group + extra):<42}"
              f"{w.sum() / 1e9:>7.0f}B{w.sum() / total * 100:>6.1f}%{mean_mi:>6.0f}")
    print(f"\nEffort spread {effort.max() / effort.min():.2f}x | "
          f"AUM spread {max(weights[assign == c].sum() for c in range(K)) / min(weights[assign == c].sum() for c in range(K)):.2f}x "
          f"| fair share now {100 / K:.1f}%")

    # what actually moves
    print("\nSTATES THAT CHANGE HANDS")
    now = {s: name for name, ss in CURRENT.items() for s in ss.split()}
    moves = []
    for i, s in enumerate(states):
        was, becomes = now.get(s, "-"), names[assign[i]]
        keep = {"Steve Halley": "West", "Tate Lambeth": "Southwest",
                "Steve Zimmerman": "Midwest", "Matt Keeter": "Southeast",
                "Keith Telesca": "Mid-Atlantic", "Dennis McKinney": "Northeast",
                "Sam Borland": "Florida/PR"}
        if keep.get(becomes) != was:
            moves.append((s, was, becomes, weights[i] / total * 100))
    for s, was, becomes, share in sorted(moves, key=lambda r: -r[3]):
        print(f"  {s}  {was:<14} -> {becomes:<17}{share:>5.1f}% of national opportunity")
    print(f"  {len(moves)} states move")


if __name__ == "__main__":
    main()
