"""Seven territories, seven real home cities, and a few permitted fly-to states.

Extends territory_design.py with the two constraints that make the answer
usable rather than theoretical:

  1. A wholesaler lives in a REAL metro -- one of the thirty largest by
     opportunity -- not at a weighted centroid in a field.
  2. Up to three of them may cover ONE state detached from the rest of their
     patch. That single relaxation is what breaks the structural deadlock: the
     top seven states hold 52% of all opportunity, so a strictly contiguous
     seven-way split cannot balance without absurd pairings (the unconstrained
     balanced solver put Florida with Massachusetts).

EFFORT, NOT AUM
Balancing relevant AUM alone is wrong, and the previous run showed why: the
Mid-Atlantic holds 24.9% of opportunity within a 48-mile median radius while
the Southwest holds 13.2% spread over 284 miles. A dense territory should carry
MORE assets, because travel is not eating the week. So territories are balanced
on an effort index:

    effort = relevant AUM x (1 + weighted mean miles / 300)

The 300-mile constant is a judgement, not a measurement: it sets how much a
day's travel is worth against a day's calls. Change it and the shape changes.

Contiguity is enforced from a state adjacency map. A territory may be one
connected blob, or one blob plus a single detached state -- and at most three
territories may take that second form.
"""
from __future__ import annotations

import json
import math
import pathlib

import numpy as np
import pandas as pd

from territory_design import load_points, WEB, OFFSHORE

TOP_CITIES = 30
K = 7
MAX_DETACHED = 3          # how many reps may hold one fly-to state
MILES_PER_DAY = 300.0     # travel-to-calls exchange rate in the effort index
MIN_SEPARATION = 175.0    # miles between candidate bases, so 30 metros are 30 PLACES
EFFORT_BAND = 1.35        # max:min effort allowed; balance is a limit, not the goal
SEED = 20260804

# Shared land borders. Offshore states have none and are attached by rule.
ADJ = {
    "AL": "FL GA MS TN", "AZ": "CA NM NV UT", "AR": "LA MO MS OK TN TX",
    "CA": "AZ NV OR", "CO": "AZ KS NE NM OK UT WY", "CT": "MA NY RI",
    "DE": "MD NJ PA", "DC": "MD VA", "FL": "AL GA", "GA": "AL FL NC SC TN",
    "ID": "MT NV OR UT WA WY", "IL": "IA IN KY MO WI", "IN": "IL KY MI OH",
    "IA": "IL MN MO NE SD WI", "KS": "CO MO NE OK", "KY": "IL IN MO OH TN VA WV",
    "LA": "AR MS TX", "ME": "NH", "MD": "DC DE PA VA WV", "MA": "CT NH NY RI VT",
    "MI": "IN OH WI", "MN": "IA ND SD WI", "MS": "AL AR LA TN",
    "MO": "AR IA IL KS KY NE OK TN", "MT": "ID ND SD WY", "NE": "CO IA KS MO SD WY",
    "NV": "AZ CA ID OR UT", "NH": "MA ME VT", "NJ": "DE NY PA", "NM": "AZ CO OK TX UT",
    "NY": "CT MA NJ PA VT", "NC": "GA SC TN VA", "ND": "MN MT SD",
    "OH": "IN KY MI PA WV", "OK": "AR CO KS MO NM TX", "OR": "CA ID NV WA",
    "PA": "DE MD NJ NY OH WV", "RI": "CT MA", "SC": "GA NC", "SD": "IA MN MT ND NE WY",
    "TN": "AL AR GA KY MO MS NC VA", "TX": "AR LA NM OK", "UT": "AZ CO ID NM NV WY",
    "VT": "MA NH NY", "VA": "DC KY MD NC TN WV", "WA": "ID OR",
    "WV": "KY MD OH PA VA", "WI": "IA IL MI MN", "WY": "CO ID MT NE SD UT",
}


def miles(lat1, lon1, lat2, lon2):
    dlat = np.radians(np.asarray(lat2) - lat1)
    dlon = np.radians(np.asarray(lon2) - lon1)
    a = (np.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * np.cos(np.radians(np.asarray(lat2)))
         * np.sin(dlon / 2) ** 2)
    return 3959 * 2 * np.arcsin(np.sqrt(a))


def candidate_cities(points, n=TOP_CITIES):
    """The n metros with the most opportunity, as home-base candidates."""
    index = json.loads((WEB / "geo_index.json").read_text(encoding="utf-8"))["cities"]
    rows = []
    for city, entries in index.items():
        for state, lat, lon, count in entries:
            if state in OFFSHORE or count < 150:
                continue
            rows.append((city.title(), state, lat, lon))
    cities = pd.DataFrame(rows, columns=["city", "state", "lat", "lon"])
    # opportunity within 60 miles of the city -- a metro, not a municipality
    weight = []
    for row in cities.itertuples():
        d = miles(row.lat, row.lon, points["lat"].values, points["lon"].values)
        weight.append(points["weight"].values[d <= 60].sum())
    cities["metro_weight"] = weight
    # Spatially de-duplicate. Ranking by 60-mile catchment alone returned
    # thirty New York suburbs -- Florham Park, Short Hills, Princeton,
    # Weehawken -- because each captures the same agglomeration. Every base is
    # then in one metro, and the optimiser served California from New Jersey.
    # Take the heaviest city, suppress everything within MIN_SEPARATION, repeat.
    cities = cities.sort_values("metro_weight", ascending=False).reset_index(drop=True)
    picked = []
    for row in cities.itertuples():
        if any(miles(row.lat, row.lon, [p2.lat], [p2.lon])[0] < MIN_SEPARATION
               for p2 in picked):
            continue
        picked.append(row)
        if len(picked) == n:
            break
    return pd.DataFrame([{
        "city": r.city, "state": r.state, "lat": r.lat, "lon": r.lon,
        "metro_weight": r.metro_weight} for r in picked])


def state_frame(points):
    grouped = points.groupby("state").apply(lambda d: pd.Series({
        "lat": np.average(d["lat"], weights=d["weight"]),
        "lon": np.average(d["lon"], weights=d["weight"]),
        "weight": d["weight"].sum(),
        "pins": len(d),
    }))
    return grouped.reset_index()


def components(states, adj):
    """Connected components of a state set under land adjacency."""
    remaining, out = set(states), []
    while remaining:
        stack, group = [remaining.pop()], set()
        while stack:
            s = stack.pop()
            group.add(s)
            for nb in adj.get(s, "").split():
                if nb in remaining:
                    remaining.discard(nb)
                    stack.append(nb)
        out.append(group)
    return out


def score(assign, states, weights, dist, k):
    """MINIMISE total travel, SUBJECT TO an effort band and legal shapes.

    The earlier formulation minimised the effort *ratio* directly, where
    effort = AUM x (1 + miles / 300). That is perverse: a light territory can be
    balanced up by piling travel onto it, and the optimiser duly served
    Connecticut from southern California and Alabama from Scottsdale while
    reporting a 1.04x spread. Balance is a constraint, not something to buy
    with someone's windscreen time -- so travel is the objective and the band
    is the limit.
    """
    effort, travel, detached, illegal = [], 0.0, 0, 0
    for c in range(k):
        m = assign == c
        if not m.any():
            return np.inf, None                  # an empty territory is not a design
        w = weights[m]
        weighted_miles = float((dist[m, c] * w).sum())
        travel += weighted_miles
        effort.append(w.sum() * (1 + weighted_miles / w.sum() / MILES_PER_DAY))
        comps = components([states[i] for i in np.where(m)[0]], ADJ)
        if len(comps) == 1:
            continue
        sizes = sorted(len(c2) for c2 in comps)
        if len(comps) == 2 and sizes[0] == 1:
            detached += 1
        else:
            illegal += len(comps) - 1
    # Penalties, not rejections. Returning infinity for every violation left the
    # hill-climb with no gradient -- nothing it could reach was finite, so the
    # search never started and returned no design at all. A large finite cost
    # keeps the ordering (feasible always beats infeasible) while still telling
    # the climb which direction is less wrong.
    effort = np.array(effort)
    # Normalise travel to weighted MEAN MILES (~200) before adding penalties.
    # Raw travel is in dollar-miles, about 4.4e15; penalties of 1e12 were four
    # orders of magnitude too small to bite, so the search minimised travel and
    # ate the imbalance -- 34% of the book against 3%, at a reported 9.79x.
    mean_miles = travel / weights.sum()
    penalty = illegal * 1e6 + max(0, detached - MAX_DETACHED) * 1e6
    over = effort.max() / effort.min() - EFFORT_BAND
    if over > 0:
        penalty += over * 600          # ~3 mean miles per 0.005 of overage
    return mean_miles + penalty, effort


def assign_states(dist, weights, k, states, rng, tries=260, max_passes=4):
    """Greedy nearest, then hill-climb single-state moves on the effort ratio.

    max_passes caps the climb: an uncapped climb over 50 states x 7 clusters,
    run inside an outer search over base sets, is billions of evaluations.
    """
    best_assign, best_cost = None, np.inf
    for t in range(tries):
        if t == 0:
            assign = dist.argmin(1)
        else:
            assign = rng.integers(0, k, len(weights))
        cost, _ = score(assign, states, weights, dist, k)
        improved, passes = True, 0
        while improved and passes < max_passes:
            improved = False
            passes += 1
            for i in range(len(weights)):
                original = assign[i]
                for c in range(k):
                    if c == original:
                        continue
                    assign[i] = c
                    trial, _ = score(assign, states, weights, dist, k)
                    if trial < cost - 1e-9:
                        cost, improved = trial, True
                        original = c
                    else:
                        assign[i] = original
        if cost < best_cost:
            best_assign, best_cost = assign.copy(), cost
    return best_assign, best_cost


def main() -> None:
    points = load_points()
    onshore = points[~points["state"].isin(OFFSHORE)]
    cities = candidate_cities(onshore)
    sf = state_frame(onshore)
    states = list(sf["state"])
    weights = sf["weight"].to_numpy()

    print(f"Top {len(cities)} metros by opportunity within 60 miles:")
    for row in cities.head(12).itertuples():
        print(f"  {row.city + ', ' + row.state:<24}${row.metro_weight / 1e9:,.0f}B")

    dist_all = np.column_stack([
        miles(c.lat, c.lon, sf["lat"].values, sf["lon"].values)
        for c in cities.itertuples()])

    rng = np.random.default_rng(SEED)
    n = len(cities)
    # Local search over which seven metros host a rep, from several starts.
    # Cheap evaluation inside the search, thorough evaluation once at the end.
    best = (np.inf, None)
    for start in range(6):
        current = list(rng.choice(n, K, replace=False))
        cur_cost = assign_states(dist_all[:, current], weights, K, states, rng,
                                 tries=2, max_passes=3)[1]
        for _ in range(12):
            improved = False
            for slot in range(K):
                for cand in range(n):
                    if cand in current:
                        continue
                    trial = current.copy()
                    trial[slot] = cand
                    cost = assign_states(dist_all[:, trial], weights, K, states, rng,
                                         tries=1, max_passes=2)[1]
                    if cost < cur_cost - 1e-9:
                        current, cur_cost, improved = trial, cost, True
            if not improved:
                break
        if cur_cost < best[0]:
            best = (cur_cost, current.copy())

    cost, chosen = best
    if chosen is None:
        raise SystemExit("no feasible design found -- widen EFFORT_BAND or MAX_DETACHED")
    assign, cost = assign_states(dist_all[:, chosen], weights, K, states, rng, tries=200)
    _, effort = score(assign, states, weights, dist_all[:, chosen], K)
    total = weights.sum()
    offshore_here = sorted(set(points["state"]) & OFFSHORE - {"PR"})
    ca_group = int(assign[states.index("CA")])
    fl_group = int(assign[states.index("FL")])

    print(f"\nBEST SEVEN BASES  (effort ratio {cost:.2f}x)")
    print(f"{'BASE':<22}{'STATES':<48}{'AUM':>8}{'SHARE':>7}{'MEAN MI':>9}{'FLY-TO':>9}")
    print("-" * 104)
    for c in range(K):
        m = assign == c
        group = [states[i] for i in np.where(m)[0]]
        comps = components(group, ADJ)
        island = ""
        if len(comps) == 2:
            island = sorted(min(comps, key=len))[0]
        city = cities.iloc[chosen[c]]
        extra = offshore_here if c == ca_group else ([] if c != fl_group else [])
        if c == fl_group and "PR" in set(points["state"]):
            extra = extra + ["PR"]
        w = weights[m]
        mean_mi = (dist_all[m, chosen[c]] * w).sum() / w.sum()
        shown = sorted(group) + extra
        print(f"{city.city + ', ' + city.state:<22}{' '.join(shown):<48}"
              f"{w.sum() / 1e9:>7.0f}B{w.sum() / total * 100:>6.1f}%{mean_mi:>8.0f}{island:>9}")
    print(f"\nEffort index spread {effort.max() / effort.min():.2f}x "
          f"(AUM alone {max(weights[assign == c].sum() for c in range(K)) / min(weights[assign == c].sum() for c in range(K)):.2f}x)")


if __name__ == "__main__":
    main()
