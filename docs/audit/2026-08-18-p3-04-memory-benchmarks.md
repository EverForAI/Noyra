# P3-04 Memory Benchmark Runs

Date: 2026-08-18

These are read-only local runs against the upstream datasets. The raw benchmark files were
downloaded to the system temporary directory and were not committed to the repository.

## LoCoMo

- Repository: `https://github.com/snap-research/LoCoMo`
- Git revision: `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`
- File: `data/locomo10.json`
- SHA-256: `553cd5a15e25f2ceccc6ed185221eba645080c93e5b91087560a91aa5961f365`
- Dataset size: 10 conversations, 1,986 QA records, 5,882 dialogue-turn memories.
- Retrieval scope: 1,982 QA records with explicit evidence dialogue IDs; four category-3 records
  intentionally have no evidence IDs and were excluded from evidence retrieval scoring.
- Noyra retrieval limit: 8 memories.
- Hit rate: `0.520182`.
- Evidence recall@8: `0.397513`.
- Mean reciprocal rank: `0.353670`.
- Elapsed retrieval time: `226965.421 ms`.

## LongMemEval Oracle

- Repository: `https://github.com/xiaowu0162/LongMemEval`
- Git revision: `9e0b455f4ef0e2ab8f2e582289761153549043fc`
- File: `longmemeval_oracle.json` from
  `https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned`
- SHA-256: `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c`
- Dataset size: 500 questions, 10,960 chat-turn memories.
- Retrieval scope: session-level evidence retrieval with an 8-memory limit.
- Session recall@8: `0.686709`.
- Hit rate: `0.808000`.
- Mean reciprocal rank: `0.654526`.
- Elapsed retrieval time: `156520.633 ms`.

## Interpretation And Limits

The runs prove that the bounded Noyra retriever can consume pinned public datasets and emit
reproducible retrieval metrics with upstream provenance. They are retrieval-only measurements;
they do not claim answer-generation accuracy. The committed fixture remains reduced and
license-safe. LongMemEval S/M, answer-level QA evaluation, and corpus-size quality/latency/memory/
cost curves remain required before P3-04 can be promoted to `verified`.
