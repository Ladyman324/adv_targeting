# Morgan Stanley contact refresh

Run the three public-source stages in this order:

    python src/morgan_stanley_async.py
    python src/morgan_office_async.py
    python src/morgan_team_async.py --concurrency 2 --batch-size 20 --delay-ms 2000

The first stage writes financial advisors and preserves two distinct URL
claims: Profile URL is the individual's page and Team Page URL is Yext's
c_teamPagesURL. The two fields must never be substituted for one another.

The second stage keeps branches and complexes outside the person roster. It
also writes a deduplicated index of the team entities published inside branch
records. That index contributes page discovery and an independent set of
published team email addresses.

The final stage fetches each unique team page through temporary, cookie-free
contexts in the existing Chrome debug process, parses structured person cards,
and checkpoints into the versioned team_pages_v2 cache. Each bounded wave gets
a new context so the crawler does not touch user tabs or cookies and does not
exhaust Morgan's cumulative per-session request budget. A host-wide failure
still stops the run. Bootstrap connection resets receive bounded one-minute
retries, and rerunning the same command resumes only unresolved pages.

No supplemental team member is assigned a CRD by the crawler. The ordinary
Morgan firm, exact-email, name, team, and location gates must prove any link to
an SEC advisor. Shared emails and ambiguous name/team results remain
unresolved.

Before replacing the roster, the final stage:

- refuses a reduction from the prior dated roster's team-member coverage;
- writes a before/after refresh comparison;
- cross-checks branch-published email sets against parsed page emails; and
- retains non-success page outcomes in the coverage report.

After the roster is complete, rebuild contacts and run
src/morgan_team_app_coverage.py. Review its ambiguous and unresolved files
before publishing generated application data.
