# Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                  # everything, ~2.5 min
.venv/bin/python -m pytest tests/test_garmin_parsing.py tests/test_web.py   # ~2s, no database
```

Each test that needs a database gets its **own freshly migrated one**, created
and dropped around it. That costs a second or so per test and buys the thing
that makes the suite worth trusting: these tests assert on counts and averages
over whole tables, and a suite where one test's leftovers move another's numbers
teaches you to distrust it.

By default a `postgres:17-alpine` container is started on a free port for the
session and removed afterwards, so Docker needs to be on PATH. Point
`DIET_TEST_DSN` at a cluster you already have to skip that — it must be one you
do not mind losing, since the schema `ALTER`s cluster-wide roles and the
fixtures create and drop databases.

| file | needs | what it is for |
|---|---|---|
| `test_garmin_parsing.py` | nothing | Garmin's shape is not a contract. A missing field must yield nothing, never a guess. |
| `test_schema.py` | database | RLS is the entire tenant boundary; the CHECKs are the only thing between a plausible wrong number and the record. |
| `test_poller.py` | database | The ingest path, with the network seam faked at `garmin.fetch_*`. |
| `test_days.py` | database | The figures the summary is built from — targets, ignored days, partial expenditure. |
| `test_api.py` | database, server | Headers, cookie flags and the goal-scoped day window, over the wire. |
| `test_mcp.py` | database, server | The tool surface over a real bearer token, and that no tool takes a user id. |
| `test_oauth.py` | database, server | Discover, register, authorize, consent, exchange, refresh. |
| `test_web.py` | node | The page's own arithmetic, lifted out of `index.html` by `web_harness.js`. |

## What these are for

Not coverage. Every one of them exists because something was wrong here once,
or because a property is load-bearing enough that breaking it silently would be
expensive. Where a test defends a specific past bug, its docstring says which —
that context is the reason not to "simplify" the assertion later.

The suite has been checked against deliberate regressions rather than assumed to
work. Reverting `energy_out_partial` to its old definition, dropping a table
from the RLS loop, removing the pre-goal exclusion, or letting activity calories
into the energy view each fail exactly the test written for them. A test that
cannot fail is worse than no test, so if you add one, break the code on purpose
and watch it go red before you trust it.

## The Garmin seam

Nothing here talks to Garmin. `garmin.fetch_day` / `fetch_activities` is the
line between their HTTP and our code, and the fakes replace exactly that: above
it everything is real, including the database writes. A live account would make
the suite depend on someone's actual sleep, spend requests against a rate limit
that has bitten this deployment before, and go red during a Garmin outage.
