# Advisor Map Recommendations

## Executive summary

The app is a strong territory-exploration prototype, but data correctness and
sales workflow should be addressed before expanding the visual layer.

No `claude.md` or `CLAUDE.md` file was present when this review was performed.
The review itself made no application changes; the implementation described
below was enacted afterward on July 25, 2026.

## Implementation status — enacted July 25, 2026

The requested recommendations are now implemented:

- **Firm identity is CRD-canonical.** State and national firm dictionaries,
  filters, comparisons, popups, rosters, and search use the legal firm's CRD as
  the key. Same-name entities remain separate and show their CRD.
- **National aggregation is exact at the firm-office level.** The national
  layer now emits one record for every legal-firm/physical-office combination.
  Shared addresses retain every firm, motion, score, and advisor-placement
  count; physical-office IDs keep office totals distinct from firm-office rows.
- **Provenance and quality are visible.** Generated metadata supplies the SEC
  feed date, map build date, coverage, omitted rows, and geocode-precision mix.
  The state and territory views include a placement-quality multi-select and
  definitions for rooftop, approximate, and nearest-address placement.
- **Geography can be selected explicitly.** A freehand lasso works in national,
  state, and territory scopes and restricts markers, counts, and firm lists to
  the selected polygon. Completing the lasso now fits the map to its bounds.
- **Search is national.** Firm name/CRD search is available from the national
  firm layer; a lazy-loaded advisor index supports advisor name/CRD search and
  remains nationwide from every state and territory scope. Results identify
  their sales territory and flag advisors outside the current territory.
  Selecting an advisor loads that advisor's sales territory, preserves
  compatible opportunity filters, clears only filters that hide the advisor,
  switches back to Advisor view, isolates that advisor's mapped office records,
  opens the profile popup, and announces the territory switch and each cleared
  filter. The advisor focus appears as a removable active-filter chip. Advisor
  navigation sets the destination zoom independently of the asynchronous
  marker-cluster queue, so repeated cross-territory searches reliably replace
  stale office popups and reach the selected profile.
- **Filter state is explicit.** Active filters appear as individually removable
  chips with a single Reset all action. The existing scope-persistence rule for
  targeting filters remains visible instead of implicit.
- **List limits are visible.** The firm list states whether it is showing all
  matches or the first 250 of the total.
- **Advisor duplicates are clarified.** State results are grouped one row per
  advisor, include advisor CRD and firm, and summarize multiple office locations;
  choosing a multi-office result fits all of that advisor's matching offices.
- **Count semantics are explicit.** State and territory totals distinguish
  distinct advisors, physical offices, and firms. National totals are labeled
  advisor placements, physical offices, and firms. The current-viewport,
  filtered full-scope, and drawing-cap explanations now live in an information
  popover beside the count instead of consuming permanent vertical space. The
  national-layer explanation likewise sits in an information popover beside
  the page heading.
- **Scope and product opportunity use one visual system.** A compact 3-by-2 KPI
  grid shows **Advisors**, **Offices**, and **Firms**, followed by **AUM**,
  **Equities**, and **Funds/ETFs**. Firm AUM and product pools allocate by each
  firm's share of distinct mapped advisors in detailed views; product pools use
  ADV Item 5.K percentages applied to non-pooled regulatory AUM.
  The intentionally lightweight national layer uses firm-office advisor
  placements as its geographic proxy. An adjacent information popover explains
  the calculation, asset definitions, coverage, 100% allocation cap, and why
  the estimate is neither actual holdings nor discretion-adjusted.
- **AUM and firm comparison are additive.** The AUM presets support familiar
  single selection plus Ctrl/Cmd/Shift-click unions, work in national as well
  as territory/state scope, and use regulatory AUM carried in the compact
  national CRD dictionary. Firms in view use the same additive gesture. Each
  selected firm receives a stable comparison color across firm rows, advisor
  pins, national office markers, rosters, and segmented shared-office circles.
  Changing AUM at national scope exits selected-firm focus with a notice so the
  requested AUM segment always repopulates the opportunity heatmap and resets
  the map to the national extent rather than leaving it zoomed into the prior
  firm's city. Generated-data requests carry a build version, and the national
  AUM payload bypasses the browser cache, preventing new JavaScript from being
  paired with a stale pre-AUM national firm dictionary.
- **Firm-list prioritization is sales-readable.** Each firm row now shows its
  advisor count first and Relevant AUM second, where Relevant AUM is the firm's
  Equities plus Funds/ETFs pools. The two sort choices use the identical order
  and terminology. The internal opportunity score remains available elsewhere
  for analytical calibration but no longer occupies this prospecting list.
- **Tier 1 map navigation follows familiar conventions.** Locations, firms, and
  advisors share a single Details drawer. Clickable nouns open the corresponding
  object, Back restores the prior object and map position, Close or Escape exits,
  and clicking empty map space clears focus. Marker clicks and search results use
  the same screens. Firm detail is non-destructive; geography changes only through
  explicit map actions, while advisor search retains its disclosed territory and
  filter-relaxation behavior.
- **The national visualization is opportunity-weighted.** The default national
  view spatially bins firm-office records, combines advisor placements with firm
  opportunity scores, and normalizes to the current viewport's 12th–97th
  percentile range so dense markets do not collapse into one saturated color.
  Clickable firm-office circles remain over the heatmap but now use one neutral
  accent color; selecting a firm supplies the only categorical highlight.
  Product-fit categories remain available in the analytical data but no longer
  consume map, legend, or filter space. Platform or home-office access remains
  a separate firm attribute.
  A session-persistent **Continental U.S. only**
  control defaults on, filters national and multi-state territory results to the
  Lower 48 plus DC, and keeps West fitted to its continental states. Selecting
  Alaska, Hawaii, Puerto Rico, or the U.S. Virgin Islands directly turns the
  control off with a notice.
  **Selects outside managers** also defaults on and is available in national,
  state, and territory scopes. The compact national CRD dictionary carries the
  ADV 5.G(7) flag so the national heatmap, counts, and firms-in-view list all
  honor the filter. Continental preference persists within the browser session;
  outside-manager selection begins on whenever the application launches.
- **One global search handles location and identity.** The single top-level
  field returns grouped city, ZIP, state, advisor, firm, and CRD results. A
  location loads its sales territory and centers on the result; an advisor loads
  the correct territory and visible pin; a firm works nationwide from any
  scope. Clearing or selecting a location immediately hides the results. Each
  detailed-map render receives a fresh, cancellable marker-batch queue so a
  large prior territory cannot leave the next territory's pins registered but
  invisible or continue writing after its cluster layer is removed.
- **Firm search is territory-aware and firm focus is actionable.** A firm that
  is already present in the loaded detail scope stays in that scope. A firm
  found in exactly one sales territory loads that territory directly, switches
  to office view, fits all of its offices, and opens a sole-office popup. Firms
  spanning multiple territories open a national firm focus instead of silently
  choosing a salesperson. That focus suppresses the heatmap, renders every
  mapped office for the selected CRD without the normal national drawing cap,
  fits the full footprint, and keeps a sole-office popup open after map fitting.
- **Advisor profiles connect to regulatory detail.** Individual popups display
  firm regulatory AUM in compact notation and provide distinct links to the
  advisor's individual IAPD record and the firm's CRD-keyed IAPD record.
  National firm-office popups also link directly to the firm's IAPD record.
- **A sales-oriented firm overview is available everywhere a firm appears.**
  Advisor popups, national firm-office popups, and firm-list Details actions
  open a CRD-keyed drawer. It emphasizes factual product-relevant dollar pools,
  keeps home-office/platform access separate, and exposes abbreviated firm
  RAUM, discretion, client mix, asset mix, mapped people and offices, the firm
  website, and IAPD. Heuristic category and score displays were removed; a
  product suggestion appears only when the evidence is one-sided.
  The overview also provides a direct **Show advisors on map** action when the
  territory is unambiguous, a sales-territory coverage breakout for multi-
  territory firms, territory buttons that load all mapped advisors for that
  firm, and a **Show all mapped offices** national action.
  Office-view popups list every represented legal firm in a scrollable section,
  with a **Firm overview** action for each CRD; address/building rosters expose
  the same firm-level action.
- **Product-relevant dollars use the correct ADV denominator.** The SMA pool is
  the implied value of exchange-traded equity securities and the mutual-fund
  pool is the implied value of registered investment company / BDC securities.
  Both use the Item 5.K non-pooled denominator: total RAUM less Item 5.D assets
  for investment companies, business development companies, and pooled
  investment vehicles. The drawer labels the results as estimates, explains
  that reported percentages are approximate, and does not multiply them by the
  discretionary share because the filing supplies no asset-type-by-discretion
  cross-tab.
- **Ownership limits are explicit.** Related-control flags from the bulk firm
  feed are shown, while the drawer explains that named Schedule A/B owners are
  not present in the current SEC bulk XML and directs users to IAPD.
- **The default interface is sales-focused.** Advisor experience, registration,
  registration reach, and map-position quality live under Advanced filters;
  provenance moved under Data information; lasso
  moved onto the map; the firm list can collapse; and the empty Active filters
  area disappears. Firm AUM now uses complete presets: under $100M, $100M–$1B,
  $1B–$10B, $10B–$100B, and over $100B. The national visualization selector,
  product-category filter, and product legend were removed from the primary
  workflow. The five AUM presets plus All are arranged as a compact three-column
  by two-row control, while multi-firm comparison remains available through
  modifier-click rather than occupying a separate control.
  The left rail omits decorative ADV Targeting and Firm section labels. The
  outside-manager switch sits directly below Continental U.S. only; Firm AUM
  uses a single muted header with an AUM-only Clear action; and the firms-in-view
  interaction instruction is omitted to preserve vertical space.
- **Advisor clusters use continuous density sizing.** The former green/yellow/
  orange threshold colors and three near-identical preset sizes were replaced
  with one neutral accent and a continuous amplified-log diameter scale. The
  fixed 1–15,000 domain uses `20 + 52 × (log10(count) / log10(15,000))³`, bounded at 20–72
  pixels. Cubing the normalized log compresses low and mid counts while
  preserving the largest metros, creating more differentiation without size
  jumps. Circles below 36 pixels use an 11-pixel count label; larger circles use
  12 pixels. Ordinary fill strength is 72%; selected-firm colors remain at 84%,
  including segmented clusters when several selected firms overlap. The border
  is 1.5 pixels and the lighter shadow preserves basemap labels. Standalone
  advisor pins use a clearer white outline.

The generated-data workflow is repeatable with `src/rebuild_webapp.py`, and
cross-artifact CRD, office, state-total, and search-index invariants are checked
by `src/validate_webapp_data.py`.

## Highest-priority improvements

### 1. Preserve firm identity by CRD

The pipeline intentionally creates unique firm names for duplicate legal
entities, but the state-map export removes the CRD suffix and groups records by
the cleaned name:

- `src/score.py:174`
- `src/export_geojson.py:22`
- `src/export_geojson.py:87`

The data contains 123 legal firms sharing 57 names, producing same-name
collisions in 50 states. Some same-named entities have different opportunity
scores and sales motions.

**Recommendation:** Use firm CRD as the canonical key throughout the map.
Display the CRD where needed to distinguish entities. If sales wants firms
combined under a recognizable brand, create a separate parent-brand field and
make that rollup explicit.

### 2. Correct the national firm aggregation

The national export assigns each office only its dominant firm and dominant
motion (`src/export_national.py:50`). Approximately 89,600 advisor placements
are at multi-firm addresses. Consequently, national firm counts and filters can
attribute advisors from minority firms at an address to the dominant firm.

**Recommendation:** Export one record per firm-office combination. Alternatively,
position the national layer strictly as a geographic-density overview, remove
precise firm rankings from that scope, and require state or territory drill-down
for firm-level analysis.

### 3. Treat the opportunity score as a hypothesis

The score's fit calibration is currently based on six hand-labeled firms
(`src/score.py:42`). That is a useful starting hypothesis, but it is not enough
evidence to treat the result as a validated prediction of sales potential.

The pipeline calculates percentile within sales motion
(`score_pct_in_motion`), but the map sorts and displays the raw opportunity
score. Raw scores allow large platforms to crowd out firms serving a different
sales motion.

**Recommendations:**

- Capture CRM outcomes such as contact attempts, meetings, qualified
  opportunities, pipeline value, and wins.
- Evaluate conversion and precision among the top-ranked firms for each motion.
- Surface percentile within sales motion alongside the raw score.
- Show score components and review flags as first-class evidence.
- Describe the score as a targeting signal until it has been validated against
  a meaningful sample of sales outcomes.

### 4. Show data provenance and quality

Current placement coverage is strong: 535,001 of 536,313 branch rows are placed,
or approximately 99.76%. Placement precision is less uniform:

- Approximately 72% rooftop
- Approximately 25% Census approximate
- Approximately 3% nearest-address fallback

The app identifies approximate locations in individual popups, but users cannot
see overall coverage or filter by placement quality.

**Recommendations:**

- Show the SEC source date, application refresh date, and expected refresh
  cadence.
- Provide a data-quality or coverage panel.
- Add a geocoding-precision legend and optional precision filter.
- Explain what “rooftop,” “approximate,” and “nearest address” mean.
- Show how many records were omitted because they could not be placed.

## Make the app operational for sales

### Add a prospect workflow

The app supports exploration, but it does not yet support the actions that
normally follow prospect discovery.

Consider adding:

- Saved prospect lists
- Territory and account ownership
- Contact status or sales stage
- Notes, last activity, and next action
- Assigned salesperson
- CRM synchronization
- CSV or Excel export

The ideal transition is from “I found an opportunity” to “I assigned it,
recorded the next action, and can find it again.”

### Add explicit geographic selection

The current “in view” behavior makes geographic results dependent on viewport
size and zoom.

Consider adding:

- Radius search around a city, ZIP, or selected point
- Drawn circle or polygon selection
- Lasso selection
- Defined metro areas
- Route planning and ordered stops

This would make questions such as “show prospects within 40 miles of Atlanta”
repeatable and shareable.

### Add national search

Advisor search is only available after a state or territory has been loaded.
Sales users may not know where an advisor is located.

Add a lightweight national search index for:

- Firm name
- Firm CRD
- Advisor name
- Advisor CRD
- City and ZIP

The result can then load the appropriate state and open the selected record.

### Build a substantive firm comparison

Selecting firms currently recolors their map locations but provides little
side-by-side analysis.

The enacted firm overview now supplies the underlying per-firm facts and sales
recommendation. A true side-by-side comparison view remains a possible later
extension; it is intentionally not required for the territory-sales workflow.

A comparison panel should include:

- Firm name and CRD
- Sales motion
- Opportunity score and motion percentile
- Size and fit components
- Regulatory AUM
- Retail and HNW client mix
- Average account size
- Selection of other advisers
- Wrap sponsor versus portfolio-manager role
- Advisor and office counts
- Geographic footprint
- Review or manager-risk flags

### Make filter state explicit

Some filters persist across scope changes while others reset. This makes it
difficult to understand why an expected firm or advisor is absent.

Add:

- A persistent active-filter summary
- A single “Reset all” action
- A visible indication of default filters
- Consistent scope-change behavior
- An explanation when missing values are excluded by a filter
- Shareable URLs containing scope, viewport, filters, and selections

### Expose product-relevant fields already available upstream

Useful additions include:

- Target versus non-target classification
- Firm type
- Review reason
- Average account size and SMA viability
- Retail-client and retail-asset mix
- Wrap sponsor versus wrap portfolio manager
- Fund-share allocation
- Percentile within sales motion
- Firm website and firm-level IAPD/ADV link

The Form ADV 5.G(7) control should be labeled as a reported firm-level signal,
not a guarantee that the firm is receptive to a specific outside manager or
product.

## Usability and interaction improvements

### Make list limits visible

The firm list displays only the first 250 results (`webapp/app.js:780`) without
showing that the list has been truncated.

Show “250 of N,” add pagination, or virtualize the full list.

### Clarify advisor-search duplicates

Advisor results may contain visually identical rows when the same advisor is
associated with multiple offices. The result rows do not show enough address
detail to distinguish them.

Display the street address, firm CRD, and office relationship. Consider a
one-row-per-advisor view with expandable offices, plus an optional
one-row-per-office view.

### Improve accessibility

Motion chips, firm rows, and advisor-result rows are clickable `div` elements
without keyboard roles or focus behavior. Search-result containers lack
listbox/live-region semantics, and the map has no accessible name.

Recommended changes include:

- Use native buttons or links for interactive rows
- Support Tab, Enter, Space, and arrow-key navigation
- Add visible focus styles
- Add persistent labels for search fields instead of relying on placeholders
- Announce dynamic result counts
- Provide an accessible map description and a table alternative
- Avoid communicating firm comparisons through color alone

### Clarify count semantics

The app uses several different counting concepts:

- National advisor placements
- Distinct advisors in state or territory scope
- Offices
- Firms in the current viewport
- Rendered national offices, which may be capped

Place a definition or tooltip beside each count. Separate total-in-scope KPIs
from counts in the current map viewport.

The initial national view also centers on the contiguous United States, so its
viewport total does not necessarily equal the full national/territory total.

### Improve national visualization

The national layer draws up to 4,000 of the largest offices in view. This is
reasonable for performance but can visually overemphasize large platforms and
hide the opportunity distribution.

Consider:

- Hexbin or density rendering
- Opportunity-weighted heat maps
- Separate views for office density and target-firm density
- A toggle between advisor placements, offices, firms, and estimated
  opportunity
- Insets or explicit navigation for Alaska, Hawaii, Puerto Rico, and the U.S.
  Virgin Islands

### Support mobile and field use

If tablets or phones are important, replace the tall scrolling sidebar with a
collapsible filter drawer and a persistent results summary.

For unreliable networks, consider:

- Self-hosting or caching Leaflet dependencies
- Cached map assets where licensing permits
- Downloadable territory data
- A progressive web app or limited offline mode

### Handle loading and failures explicitly

Territories load multiple state files together. Missing state files can be
silently omitted in some cases, and quickly switching scopes can allow an older
request to complete after a newer selection.

Add:

- Per-state territory loading progress
- Partial-data warnings
- Retry controls
- Cancellation or stale-request protection
- Clear failure messages that identify the missing jurisdiction

## Existing strengths

The following provide a solid foundation:

- Separate lightweight national and detailed state/territory layers
- Compact state data files
- Advisor and office map modes
- Marker clustering
- City, ZIP, and state search
- Sales-territory scopes
- Firm multi-selection and map coloring
- Building and address rosters
- Direct advisor IAPD links
- Approximate-location warnings
- Distinct size and fit score components
- Advisor experience, registration, and geographic-reach filters

## Product questions

Answers to these questions would materially affect prioritization:

1. Is the primary job territory planning, trip planning, or choosing the next
   firm to call?
2. Which CRM does the sales team use, and should the map write activity back to
   it?
3. Is the actionable sales opportunity fundamentally a firm, branch office, or
   individual advisor?
4. Should multiple legal entities under one brand be approached as one sales
   account or kept separate?
5. Will representatives primarily use laptops, tablets, or phones?
6. Is offline or poor-connectivity use important?
7. What refresh cadence is expected for SEC firm and individual data?
8. Which downstream sales outcomes are available for validating the score?
