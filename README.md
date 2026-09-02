# hallucite

Find fabricated and mismatched citations. Deterministic, no LLM, no API key, no install.

Language models invent references. They invent plausible authors, plausible titles, and
DOIs with the right shape that point at nothing, or at a different paper entirely. The
second kind is worse, because it survives a careful read.

`hallucite` resolves every reference in a `.bib`, `.tex`, or `.md` file against the
authoritative registries (Crossref for DOIs and titles, arXiv for eprints) and tells you
which ones do not exist.

```
$ hallucite paper.bib

== paper.bib (42 refs) ==
  [ ok ] vaswani2017        DOI resolves, title match 0.99
  [FABR] chen2007fiber      DOI 10.1109/JLT.2007.899999 does not resolve at doi.org
  [MISM] wuttke2003noise    DOI resolves to a DIFFERENT title (match 0.21)
  [SUSP] smith2019book      no close Crossref match for title
  [??? ] internal2024       no DOI, arXiv id, or usable title

summary: 2 hard (fabricated/mismatch), 2 soft (suspect/uncheckable)
```

## Install

There is nothing to install. One file, Python 3.9+, standard library only.

```
curl -O https://raw.githubusercontent.com/julien-tremblay/hallucite/main/hallucite.py
python3 hallucite.py paper.bib
```

Optionally set `CROSSREF_MAILTO` to your email to join Crossref's polite pool, which is
faster and more reliable. It works without it.

## Use it as a gate

```
hallucite --gate paper.bib      # exit 1 if anything is FABRICATED or MISMATCH,
                                # or if a registry could not be reached
hallucite --strict --gate *.bib # also fail on SUSPECT and on unidentifiable refs
```

Without `--gate` the tool is advisory and always exits 0; read the summary line. Exit 2
means a usage error (unknown flag, unreadable file), never a verdict.

Drop it in a pre-commit hook or CI step and a fabricated reference stops being something
you find out about from a reviewer.

## What the classes mean

| Class | Meaning | Gate |
|---|---|---|
| `FABRICATED` | DOI 404s, or an arXiv id with no record | hard fail |
| `MISMATCH` | Resolves, but to a **different title** | hard fail |
| `SUSPECT` | Title-only reference with no close Crossref match | warn |
| `UNCHECKABLE` | No DOI, arXiv id, or usable title | warn |
| `UNCHECKABLE` | A registry could not be reached | **hard fail** under `--gate` |
| `OK` | Resolved and the title matches | pass |

## Design commitments

**It will not falsely accuse you.** If the registry is unreachable, a reference is reported
`UNCHECKABLE`, never `FABRICATED`, and the tool refuses to fall back to fuzzy title matching
to fill the gap. A verifier that cries fraud during a network outage is worse than no
verifier, because you stop trusting it exactly when it is right.

**The gate fails closed.** If no registry answered, `--gate` exits 1 and says so. Counting
an unreachable oracle as a soft pass meant a fully offline run went green having verified
nothing at all, which is the same reassuring green as a clean bibliography. A reference that
simply carries no identifier is a different thing, and stays soft.

**A parser failure is not a clean bill of health.** If a file plainly contains citations and
zero are parsed, that is reported as a hard failure rather than "0 fabricated." Silent
degradation into a reassuring green is the specific failure this tool exists to prevent.

**Deterministic beats a model here.** The HALLMARK study found a deterministic DOI/bibtex
verifier (F1 0.908) outperforms the best zero-shot LLM (0.840) at catching fabricated
citations, and that giving the model tool access made it *worse*. There is no model in this
program, so there is nothing to hallucinate.

## Known limitations

- **Some legitimately published work has a DOI that does not resolve at doi.org.** ACM's
  `10.5555/*` proceedings range is the common example: `10.5555/3295222.3295349` returns 404
  and will be reported `FABRICATED`. Check hard findings before acting on them.
- Books, theses, standards, and non-indexed venues often have no DOI and land in `SUSPECT`
  or `UNCHECKABLE`. That is a prompt to look, not a verdict.
- **Titles shorter than two characters, and `\bibitem` entries whose title is neither
  quoted nor emphasised, parse without a title.** The DOI is still checked; the
  title comparison simply does not run, and the reference reads `OK` on resolution alone.
- **It catches invented and swapped references, not subtly altered ones.** Title comparison
  is lexical, so "Attention Is Not All You Need" scores 0.93 against "Attention Is All You
  Need" and passes, as does Recognition -> Segmentation. Authors, venue and year are parsed
  but never compared. If your concern is a real paper cited with mangled metadata rather
  than a paper that does not exist, this is not the tool.
- Title matching is fuzzy (difflib ratio). A heavily abbreviated title can read as a
  `MISMATCH`.
- It checks that a reference *exists and matches*. It cannot check that the reference
  *supports the claim it is attached to*.

## Tests

```
python3 hallucite.py --selftest     # live, hits Crossref and arXiv
python3 tests/test_regressions.py   # offline, locks in fixed defects
```

## License

MIT.
