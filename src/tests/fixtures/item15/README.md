Real stored Observation payloads for tests/test_readiness.py (item 15: readiness for UNRESOLVED values), unmodified,
from the `ready` Observation records of the stored runs (`runs/*/records/Observation__*/final.json`):

- `value_unresolved`: committed as `ready` although `value` is UNRESOLVED (run 20260919T040723_06df825d: 36 of its
  108 ready Observations were like this).
- `variable_name_unresolved`: `ready` with an UNRESOLVED `variable_name` (8 across the stored runs).
- `only_temporal_unresolved`: `value` and `variable_name` resolved, only `temporal_info` UNRESOLVED (186 of the 206
  stored ready Observations have an unresolved `temporal_info`) -- these stay ready (protocol Section 10.1).
- `fully_resolved`: nothing unresolved.
