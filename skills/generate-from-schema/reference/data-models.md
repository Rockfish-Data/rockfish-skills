# Data models: time-series vs tabular

> Condensed from [`docs/data-models.md`](https://docs.rockfish.ai/data-models.html) in the
> documentation repo. Not a verbatim copy — restructured for schema authoring, with a
> closing section mapping the model onto `column_category_type`. Re-check against the
> source when that page changes.

Rockfish supports two operational data models. Choosing the right one is the first schema
decision, because it determines every column's `column_category_type` and whether the
entity needs a `Timestamp`.

## Time-series

Data collected over time. Each entity instance is a **session**: a collection of **events**
ordered by a **timestamp** column.

| Time-series column type | Description | Maps to |
| --- | --- | --- |
| Metadata | Values that describe the session and are constant across its events. | `ColumnCategoryType.METADATA` |
| Measurement | Values that change over time within the session, usually quantitative. | `ColumnCategoryType.MEASUREMENT` |
| Timestamp | When each event was collected. | `Entity.timestamp=Timestamp(column_name=...)` |
| Session key | Unique identifier per session. Optional. | A metadata column, usually `ID` or `UNIQUE` |

Examples: web analytics (page views per user), IoT sensor readings, stock prices.

### Worked example — financial transactions

| customer | age | gender | category | amount | fraud | timestamp |
| --- | --- | --- | --- | --- | --- | --- |
| C2222 | 25 | F | food | 70.84 | 1 | 2023-08-01 09:10:07 |
| C1111 | 40 | M | transportation | 35.13 | 0 | 2023-08-01 09:12:51 |
| C2222 | 25 | F | transportation | 28.26 | 0 | 2023-08-01 09:27:30 |
| C1111 | 40 | M | food | 64.99 | 0 | 2023-08-01 09:39:17 |

Each transaction is an event; each customer's transactions form a session. Two sessions here.

| Type | Columns | Why |
| --- | --- | --- |
| Metadata | `age`, `gender` | Describe the customer performing the transactions. |
| Measurement | `category`, `amount`, `fraud` | Describe each individual transaction. |
| Timestamp | `timestamp` | When the transaction happened. |
| Session key | `customer` | The customer's unique identifier. |

As a schema: a `transactions` entity with `cardinality` = number of customers,
`timestamp=Timestamp(column_name="timestamp")`, `customer`/`age`/`gender` as independent
metadata columns, and `category`/`amount`/`fraud` as stateful measurement columns
(`STATE_MACHINE` for a behavioral category sequence, `TIMESERIES` for a numeric amount).

## Tabular

Rows and columns with no temporal dimension. Each entity instance is a **record**, and
**all columns are metadata**.

Examples: customer profiles, product inventory, sales records.

| Age range | Sex | Reason for incident | Body Temperature | Heart Rate | SBP | DBP | Hypertension |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 60<70 | M | Slip | 97 | 80 | 140 | 90 | Yes |
| 30<40 | F | Loss of balance | 96 | 78 | 145 | 95 | Yes |

Each patient incident is a record; three records means three rows. As a schema: one entity,
`cardinality=3`, every column `INDEPENDENT` + `METADATA`, and **no** `Timestamp` — an entity
with a timestamp must have at least one measurement column, and vice versa.

## Choosing between them

The same raw data can be modeled either way; pick based on the downstream task. For a
netflow dataset:

- Predicting a per-flow `type` with an ML model → **tabular**: each flow is a record, all
  columns metadata, 6 flows = 6 records.
- Analyzing patterns over time → **time-series**: each flow is an event, each
  `(srcip, dstip, srcport, dstport, proto)` connection is a session, with `pkt`, `byt`,
  `type`, `td` as measurements and `ts` as the timestamp. No session key column exists in
  this data, which is fine — the session key is optional.

Note: a dataset with only one session (a single group of metadata values) is treated as
tabular, since there are no distinct sessions to learn from.

## Consequences for schema authoring

- **Tabular entity**: no `Timestamp`, all columns `METADATA`, `column_type` ∈
  `INDEPENDENT` / `DERIVED` / `FOREIGN_KEY`. Rows = `cardinality`.
- **Time-series entity**: `Timestamp` required, at least one `MEASUREMENT` column, which
  must be `STATEFUL` (`TIMESERIES` / `STATE_MACHINE`) or `DERIVED`. Row count depends on
  *which* stateful domain you used — a `Timestamp` puts the entity on the time grid, it does
  not mean every tick is filled:
    - **`TIMESERIES` only → dense.** One row per instance per tick, so
      rows = `cardinality × ticks`. A metric like CPU usage has a value at every moment.
    - **`STATE_MACHINE` → sparse.** Each session starts at a random tick and occupies
      consecutive ticks only until it reaches a terminal state, so
      rows ≈ `cardinality × mean walk length` — independent of how wide the window is.
      A 4-step session is 4 rows whether the window holds 5 ticks or 500. Applying
      `cardinality × ticks` here can overestimate by one or two orders of magnitude.
- **Metadata is generated once per instance**, before timestamp expansion, which is why a
  metadata column can never depend on a measurement column. The reverse is allowed: each
  metadata value is present on every row of its session.
- **A mixed schema is normal.** Reference/dimension entities (a device catalog, a customer
  list) are tabular; fact/event entities (readings, sessions, transactions) are time-series,
  and they link through `entity_relationships`.

## Sample real datasets

Useful for sanity-checking a schema against real shape:

- [finance.csv](https://docs142.rockfish.ai/tutorials/finance.csv) — time-series
- [pcap.csv](https://docs142.rockfish.ai/tutorials/pcap.csv) — time-series
- [fall_detection.csv](https://docs142.rockfish.ai/tutorials/fall_detection.csv) — tabular
- [spotify-2023-short.csv](https://docs142.rockfish.ai/tutorials/spotify-2023-short.csv) — tabular
