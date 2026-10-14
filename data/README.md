# Production mover universe

`universe.csv` is the checked-in input used by `movers.load_universe()` to
construct the deterministic mover universe. It contains 125 ordered public
company/symbol records; the production configuration selects the first 120.
Its row order and contents are part of the production input and must not be
replaced with a reduced test list or reordered casually.

The file contains only ticker symbols, company names, and index-membership
booleans. It contains no credentials, private user data, or historical market
observations. Its upstream source and as-of date were not recorded in the
pre-existing local project copy; the membership flags should therefore be
treated as project input metadata, not a claim of current index membership.
The exact file is included because both production and offline tests require
the actual universe.
