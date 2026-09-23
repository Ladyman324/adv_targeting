# Contact-backed working locations

The map's sales territory follows the best supported working office, not
necessarily the SEC branch filing. SEC source files are unchanged.

1. A current firm roster wins when its person's email is unique among approved
   contacts, its firm CRD agrees with a current SEC advisor-firm employment,
   and it publishes exactly one office for that person.
2. Otherwise, a confirmed ACT! contact can provide an office only through its
   approved ACT ID, matching email, and matching SEC firm employment.
3. A valid state, ZIP, street, and coordinate are required. Published coordinates
   are checked against the ZIP area; existing exact-address geocodes are reused.
   New addresses are sent to Census only with --census. Remaining addresses
   may be sent to Google with an explicit bounded --google-max-calls N.
   Unplaced/conflicting records retain their SEC placement and uncertainty flag.
4. The original SEC branch record is never overwritten. The working-location
   source is carried on map pins (SEC/firm roster/ACT!) and shown in the profile.

After refreshing contacts with python src/build_contacts.py, run
python src/contact_work_locations.py --census --google-max-calls 175 only
when new external geocoding is approved and needed. Census and Google results
are cached in ignored data/interim files; routine python src/rebuild_webapp.py
reuses those caches offline, rebuilds placement, all state pins and national
artifacts, then fails if any selected office is missing or placed in the wrong
state. That same full rebuild also regenerates names, field tiles, search
indexes, and stamped static assets, so no separate refresh commands are needed.

The review file is data/output/contact_work_locations.csv. In particular,
needs_free_geocode means no trustworthy coordinate was accepted; it does
not authorize substituting a city or ZIP centroid for a street address.
Every cross-state working-office move relative to the SEC-only placement is
listed in data/output/contact_work_territory_moves.csv with both states and
streets, but no contact email or phone.
