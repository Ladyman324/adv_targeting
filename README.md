# ADV Targeting

Turns the SEC Form ADV bulk download and IAPD individual compilation feed into
a CRD-keyed prospect dataset and interactive map for selling EIC equity SMAs
and the EIC mutual fund.

```powershell
python run.py
python src\export.py
python src\rebuild_webapp.py
python src\validate_webapp_data.py
```

The pipeline writes all firms to `data/output/firms.csv` and `firms.parquet`.
Nothing is silently discarded: every legal firm keeps its CRD, firm type,
evidence, review flag, opportunity score, and product fit.

## Product opportunity

| Category | Meaning | Sales use |
|---|---|---|
| `sma_led` | Stronger evidence for an exchange-traded-equity SMA opportunity | Lead with equity SMAs |
| `eicix_led` | Stronger evidence in registered-fund / BDC securities | Lead with the mutual fund |
| `both_products` | Both product-fit scores clear the starting threshold | Present both products |
| `low_fit` | Does not clear a product threshold or is not a distributor prospect | Review before outreach |

`platform_access` and `sales_channel` describe how to approach the account
(territory versus home office). They are deliberately separate from the product
recommendation.

The map does not expose these product categories as filters or marker colors.
They remain analytical fields for future calibration; the salesperson sees the
underlying product-relevant dollar pools in the firm overview instead.

## Reading the scores

`opportunity_score` combines scale and outside-manager evidence. The separate
`sma_fit_score` and `fund_fit_score` are transparent starting heuristics, not
purchase probabilities. Their weights and the initial threshold live in
`src/score.py` and `config.py` and should be calibrated against CRM outcomes.

The firm overview also shows implied product-relevant pools. These apply ADV
Item 5.K percentages to the reported non-pooled denominator and are estimates,
not reported holdings. They are not adjusted by discretionary share because ADV
does not cross-tab discretion and asset category.

The map header presents a structured six-card summary: **Advisors**, **Offices**,
and **Firms**, followed by geographically allocated **AUM**, **Equities**, and
**Funds/ETFs**. Detailed maps allocate each firm's figures by its share of
distinct mapped advisors; the lightweight national map uses firm-office advisor
placements as its geographic proxy. Compact information controls document count
semantics, formulas, definitions, limitations, and current firm coverage.

Firm search preserves the current detailed territory when possible. A firm in
one sales territory routes directly to its mapped offices; a multi-territory
firm opens a national CRD-specific office focus. The firm overview can then load
all mapped advisors in a selected sales territory or show every mapped office
for the firm nationally.

Firm AUM bands can be combined with Ctrl/Cmd/Shift-click and apply to both
national and detailed maps. Firms in view can be selected the same way and are
assigned distinct map colors. Advisor search creates a removable advisor focus,
always returns to Advisor view, and isolates the selected advisor's mapped
office records. Every firm listed in an office popup or address roster links to
its CRD-keyed firm overview.

The **Firms in view** list displays viewport advisor count followed by firm-level
**Relevant AUM** (Equities plus Funds/ETFs). Its sort control follows that same
left-to-right order: **advisors**, then **relevant AUM**. Firms without both ADV
asset components show an unavailable value rather than a partial total.

Map navigation uses one familiar Details drawer for locations, firms, and
advisors. Clicking a map object opens that object; clicking a firm, advisor, or
address opens it in the same drawer; Back restores the preceding object and map
position; Close, Escape, or an empty-map click clears the detail focus. Search
results enter the same drawer. Viewing a firm is non-destructive—territory and
filters change only through an explicit map action or when an advisor search must
load the advisor's territory.

The national, state, and territory views all support **Selects outside
managers** from ADV 5.G(7). It and **Continental U.S. only** are enabled on a
fresh launch; the continental choice persists for the browser session after a
user changes it.

## Key files

- `config.py` — source paths and thresholds
- `src/adv.py` — verified Form ADV mappings and feature derivation
- `src/score.py` — firm type, opportunity score, product fit, and access channel
- `src/rebuild_webapp.py` — rebuild state, national, search, and firm-profile data
- `src/validate_webapp_data.py` — verify cross-file CRD and count invariants
- `docs/field_audit.md` — verified field mappings and known limits
- `docs/map_recommendations.md` — product review and enacted implementation record
- `docs/trust_company_research.md` — isolated, non-production trust-company
  source-comparison pipeline and review contract
## Microsoft 365 email

The authenticated web app includes a draft-first Microsoft Graph composer for
single contacts and saved call lists, with per-recipient editing, approved
attachments, validation, a durable paced send queue, immutable Outlook IDs,
and application-level safety controls. Local development uses a non-sending
Graph mock.

Deployment, Entra permissions, configuration, document/template provisioning,
and the NDR webhook boundary are documented in
[`docs/microsoft_graph_email.md`](docs/microsoft_graph_email.md).