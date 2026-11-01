# Batchability spike (#78)

Can we predict, from the text alone, that a client-relevance statement
returns **exactly one value without erroring**, so it can safely share a
tuple Fast Query `((e1), (e2), ...)` with others? This folder is the
measurement and a classifier written to be donated to
[bigfix-relevance-analyzer](https://github.com/jgstew/bigfix-relevance-analyzer).
None of it ships in the wheel; the client package never imports the analyzer
(the analyzer depends on the client).

| file | what | needs |
| --- | --- | --- |
| `batchability.py` | the classifier: `batchability(text, *, dialect=CLIENT, platform=None) -> Batchability` | stdlib + analyzer |
| `test_batchability.py` | its unit tests, one per rule | analyzer + pytest |
| `build_corpus.py` | extracts the corpus from bigfix-content, plus edge cases | analyzer |
| `corpus.jsonl` | the 545 statements | |
| `run_spike.py` | evaluates each statement alone and tupled with a sentinel, through this library | this repo |
| `score.py` | joins verdicts with outcomes | analyzer |
| `scores.md` | the result | |

Raw results (`results/`) are not committed: they hold host names, paths and
environment variables of the machines they ran on.

## Method

1. **Corpus.** `build_corpus.py` over bigfix-content `24dd4bd`: every
   fixlet/analysis/group `relevance`, `analysis-property` and
   `global-property` the analyzer extracts as client relevance, deduplicated,
   at most 40 files per directory (the 751 near-identical Dell BIOS fixlets
   are sampled, seed 78), plus 70 hand-picked edge cases. 545 statements.
2. **Ground truth.** `run_spike.py` evaluates every statement twice through
   `evaluate_many` (the qna stdin batch path) with qna 11.0.7.61:
   alone, and as `((E), "bfrcr-78-sentinel")`. A statement is **batchable
   ("one")** when alone it answered exactly one value with no error, and the
   tuple came back as exactly one answer that splits on `", "` into that
   value and the sentinel, with no part holding `,`, `(` or `)`.
   Otherwise its outcome is `error`, `empty`, `plural` or `split`;
   `env-error` marks errors the container itself causes (no SMBIOS/DMI
   tables; Rocky's image cannot set up the RPM inspector), which no real
   endpoint shares and which are left out of the precision table.
3. **Hosts.** macOS 15.7 (x86_64, over SSH with `become`), Rocky Linux 9.3
   and Ubuntu 26.04 (arm64 containers, native on an M1 Pro, Docker 29.7.2).
   A Windows Server 2022 run over SSH was attempted and abandoned: its qna
   hung on the first chunk (see the PR).
4. **Dev / holdout.** Rules were tuned against even-numbered statements only;
   odd ones are the holdout.

Probes that decided rules (qna 11.0.7, Rocky container):

- A tuple item displays exactly as it does alone, unless it holds `,`, `(` or
  `)`: then it is wrapped, `"a, b"` → `( a, b )`, `"a)"` → `(( a) )`.
  REST Fast Query against a Windows agent showed the same wrapping.
- `exists x` / `number of x` absorb only `x`'s *outermost* step being missing
  (`exists file "/nope"` is False), not a missing object beneath it:
  `exists (size of file "/nope")` and `exists files of folder "/nope"` error.
- An error inside a `whose` predicate filters the item out.
- `a | b` answers for `a`; `(folder "/a" | folder "/b")` still errors.
- An undefined inspector errors even after `false and`, but not in an `if`
  branch that is not taken.
- Empty aggregates: `concatenation` gives `""`, `sum` `0`; `maximum` and
  `unique value` error.

## Result

See `scores.md`. With the endpoint's platform known, a "safe" verdict was
right **97.1%** of the time (561 of 578), catching **84.4%** of the
statements that really were batchable; on the holdout half alone, 96.7%.
Every miss is an error the tables cannot foresee: 14 inspectors that need the
agent's client context ("No inspector context." under qna, and under Fast
Query without `UseClientContext`), and 3 inspectors the tables list for a
platform whose qna lacks them. Without the platform, precision drops to
76.4%, almost all from Windows-only inspectors on other platforms. The
analyzer's plurality alone (`SINGULAR`) is 65.4%, and the root's shape alone
(`exists` / `number of` / a comparison / `and` `or` `not`) 73.8%.

### Caveats

- The counts are (statement, host) pairs: each statement counts once per
  host, so the 578 "safe" verdicts cover 263 distinct statements.
- Ground truth is qna (its stdin batch path), tupled with **one** sentinel
  in last position. It does not exercise Fast Query itself, ten-item tuples,
  or an item that is not last; that the client's split check holds for
  those rests on the display rule above, which REST Fast Query matched in a
  handful of live queries.
- qna's context is the one Fast Query uses without `UseClientContext`; with
  it, the 14 "No inspector context." misses would most likely answer.
- An evaluation that timed out counts as `error`, which can only lower the
  number of truly batchable statements (none did in these runs).

## Run it

```bash
# classifier tests; -c /dev/null skips this repo's pytest config (it wants xdist),
# and the override lifts the repo's exclude-newer quarantine for the analyzer
uv run --no-project --python 3.12 --exclude-newer-package 'bigfix-relevance-analyzer=0 days' \
    --with 'bigfix-relevance-analyzer==1.43.0' --with pytest \
    python -m pytest -c /dev/null tools/batchability
```
