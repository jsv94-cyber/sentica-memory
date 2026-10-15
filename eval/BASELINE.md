# Retrieval baseline

Recorded from the first full run of `tests/sentica/test_m8_eval.py` on the
seeded Harbor Glass and Northline Diesel businesses. Scores are percents.
CI fails when a category falls more than 2 points below this table, or when
the isolation red-team reports any leak.

| Category | Recall@5 | Recall@10 |
| --- | --- | --- |
| information_extraction | 100 | 100 |
| multi_session_reasoning | 100 | 100 |
| temporal_reasoning | 100 | 100 |
| knowledge_updates | 100 | 100 |
| abstention | 100 | 100 |

## Latency

Local unit suite on the seeded business, separate from the 10,000-row brief
measured in M6. These figures are a record, not a fail threshold.

- Cold: 7.1 ms
- Warm p95: 7.0 ms

## Isolation

Leaks: 0
