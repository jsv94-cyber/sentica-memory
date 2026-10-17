# Retrieval baseline

Recorded from `tests/sentica/test_m8_eval.py::test_retrieval_eval_matches_baseline`
on the seeded Harbor Glass and Northline Diesel businesses. Each cell is the
mean, across that category's questions, of hybrid keyword-plus-vector
reciprocal-rank-fusion recall. Recall at k is the percent of gold conclusion
ids present in the first k results. An abstention question scores 100 only
when the search returns no rows. A forbidden id anywhere in the results
scores 0 for both columns. CI fails when a category falls more than 2 points
below this table, or when the isolation red-team reports any leak.

Embeddings are hashed character n-grams of the text (widths 3, 4, and 5,
SHA-256 buckets, L2-normalized). Vector neighbors farther than cosine
distance 0.65 are dropped before the fusion.

| Category | Recall@5 | Recall@10 |
| --- | --- | --- |
| information_extraction | 0 | 0 |
| multi_session_reasoning | 100 | 100 |
| temporal_reasoning | 100 | 100 |
| knowledge_updates | 0 | 0 |
| abstention | 100 | 100 |

## What each score measures

- information_extraction: two questions whose tokens are disjoint from every
  seeded fact, so the keyword channel has no hit. The n-gram distance from
  each query to its gold fact is above 0.65, so the vector channel returns
  nothing. Hybrid recall is 0 at 5 and at 10.
- multi_session_reasoning: two questions that share tokens with both gold
  facts, and those facts live in two sessions. Keyword search places both
  gold facts inside the top 10. Recall is 100 at 5 and at 10.
- temporal_reasoning: two questions that share tokens with the current fact
  and with the older fact. The `since` bound leaves the older fact out, and
  the current fact is inside the top 10. Recall is 100 at 5 and at 10.
- knowledge_updates: two questions whose tokens are disjoint from every
  seeded fact. The n-gram distance to the replacement fact is above 0.65,
  so the search returns nothing and the replacement is absent from the top
  10. Recall is 0 at 5 and at 10.
- abstention: two questions with no keyword hit whose nearest n-gram
  neighbor is farther than 0.65. An empty result scores 100 at 5 and at 10.

`test_vector_only_recall_is_not_perfect` measures a different quantity: the
nearest n-gram neighbor, ignoring the keyword channel and the 0.65 cutoff.
That test requires at least one question whose nearest fact is outside the
gold set.

## Latency

Local unit suite on the seeded business, separate from the 10,000-row brief
measured in M6. These figures are a record, not a fail threshold. Recorded
with the n-gram embeddings above.

- Cold: 14.0 ms
- Warm p95: 16.8 ms

## Isolation

Leaks: 0
