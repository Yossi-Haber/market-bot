# Replay schema compatibility fixture v1

These compact snapshots are synthetic schema fixtures, not historical market
data and not evidence about the dates used by the test. The CI-safe replay test
changes only the fixture's `market_date` field to exercise two historical-date
paths. Assertions cover successful schema consumption/date propagation only;
no market result, ranking, or historical conclusion is asserted.

The separate `local_historical_replay` test retains coverage against actual
local `output/history` artifacts when those private artifacts are available.
