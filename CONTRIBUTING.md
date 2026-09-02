# Contributing

Criticism is more useful to this project than code right now, and disagreement about the
design is as welcome as a bug report.

## The most valuable thing you can send

**An input that produces a wrong verdict.** Paste the entry, say what you expected, say what
you got. A `.bib` snippet of five lines is worth more than a paragraph describing it.

Two verdicts are worth much more than the others:

- A **false accusation**: `FABRICATED` or `MISMATCH` on a reference that is real and
  correctly cited. This is the failure mode the tool is built to avoid and it has shipped
  six of them. If you have one, it is the highest-value report there is.
- A **false clearance**: `OK` on a reference that is invented or points at a different
  paper. Harder to notice, and completely unmeasured. See "What I would like you to break"
  in the README.

Neither needs a fix attached.

## Running things

```
python3 tests/test_regressions.py   # offline, no network, ~1s. This is what CI runs.
python3 hallucite.py --selftest     # live, hits Crossref and arXiv, ~10s
```

The false-positive measurement, about ten minutes and no API key:

```
python3 bench/sample.py manifest.json
python3 bench/build.py manifest.json .
python3 hallucite.py raw.bib
python3 hallucite.py perturbed.bib
python3 hallucite.py control.bib      # must produce hard findings, or the run is vacuous
```

If your sample gives a non-zero false-positive rate where the README claims zero, that is a
finding and I want to see it. Include `manifest.json` so it can be reproduced.

## If you send a patch

**Every fix needs a test that fails without it.** Not a test that exercises the area, one
that goes red when the fix is reverted. Most of this project's worst defects passed a test
that could not have caught them, including a "no French in user-visible strings" check that
grepped for the four words a previous fix had already removed while seven others sat in the
output. `tests/test_regressions.py` is a plain script, no framework, and every case in it
carries a comment naming the defect it locks down. Match that.

Comments are held to one rule: a comment earns its line if deleting it would let someone
reintroduce a bug or take a wrong turn the code cannot warn them about. That is why some
lines here carry three sentences of history and most carry none.

Style: standard library only. The single-file, no-install, no-API-key property is the point
of the tool, and CI enforces it. Nothing that needs a network call belongs in the offline
suite, because a check that fails when a registry is rate-limited gets switched off.

## Scope

In scope: correctness of the verdicts, parser coverage, registries beyond Crossref and
arXiv, better measurement.

Out of scope: anything that calls a language model. The premise is that a deterministic
verifier beats one, and a model in the loop is a thing that can hallucinate.
