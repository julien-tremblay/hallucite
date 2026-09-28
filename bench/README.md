# False-positive measurement

The one number that matters for a tool whose promise is "it will not falsely accuse you",
and the one this repository did not have until 2026-09-02.

```
python3 bench/sample.py manifest.json     # draw real works from Crossref
python3 bench/build.py manifest.json .    # every arm below
python3 hallucite.py raw.bib
python3 hallucite.py perturbed.bib
python3 hallucite.py deformed.bib
python3 hallucite.py raw.bbl
python3 hallucite.py rescue.bib           # want BAD-DOI on every line
python3 hallucite.py control.bib          # must produce hard findings
```

Every work in the sample is real by construction: Crossref returned it, so the DOI is
registered and the title is the registry's own. On the arms built from correct DOIs the
correct output is zero `FABRICATED` and zero `MISMATCH`, and any hit is a false accusation
with a diagnosis attached.

| Arm | What it is | Correct output |
|---|---|---|
| `raw.bib` | registry title verbatim | no hard finding |
| `perturbed.bib` | LaTeX-escaped accents, dropped subtitles, case changes, a trailing period on the DOI, an indented closing brace | no hard finding |
| `deformed.bib` | registry markup written as TeX (`CO<sub>2</sub>` → `CO$_2$`), Crossref's separate subtitle cited as part of the title, TeX letter commands (`{\o}`, `{\ss}`, `\'{\i}`) | no hard finding |
| `raw.bbl` | the works as the `.bbl` BibTeX writes for natbib users: title in the first `\newblock`, venue in `\emph{}` | no hard finding |
| `rescue.bib` | the deformed title with a broken DOI | `BAD-DOI` on every line |
| `control.bib` | broken DOI with the real title (want `BAD-DOI`), broken DOI with an invented title (want `FABRICATED`) | hard findings; without them the run is vacuous |

A raw round-trip compares exact strings to themselves and is close to a tautology.
`control.bib` exists so the run can fail: without it, a verifier that returned `OK`
unconditionally would score a perfect false-positive rate.

`deformed.bib`, `rescue.bib` and `raw.bbl` were added on 2026-09-28. An audit that day found
false accusations in every class the first three arms cannot produce, because their titles
are the registry's own. Registry markup was only compared with itself, a subtitle Crossref
stores apart was never cited, and the dead-DOI rescue was only tried on verbatim titles,
which is where the 0.90 bar rather than the 0.60 one decides. `raw.bib`, `perturbed.bib` and
`control.bib` keep their exact recipe and random sequence, so the published run can be
rebuilt from its manifest.

`tests/test_regressions.py` builds every arm from a four-work synthetic manifest and runs the
verifier over them offline, so the machinery cannot rot unnoticed. On that manifest the
2026-09-02 tool scores zero on `raw.bib` and `perturbed.bib`, and calls three of the four
real papers `FABRICATED` in `rescue.bib` and `MISMATCH` in `raw.bbl`. That is an
illustration, not a measurement.

## Result, 2026-09-02

370 works, each checked twice. That is 370 observations of the same works under two
treatments, not 740 independent references. 258 were stratified across four decades and four
Crossref types, plus 22 with the pre-2008 `<>#+` suffix charset and 90 with non-Latin titles.

| Set | works | False positives |
|---|---|---|
| raw | 370 | **0** |
| perturbed | 370 | **0** |

| Control arm | n | Result |
|---|---|---|
| broken DOI + real title (want `BAD-DOI`) | 40 | 36 rescued, 4 called `FABRICATED` |
| broken DOI + invented title (want `FABRICATED`) | 40 | 38 caught, 2 `UNCHECKABLE`, **0 passed as OK** |

Zero events in 370 bounds the per-reference false-positive rate below 3/370, about 0.8%, at
95% confidence (the rule of three). It does not show the rate is zero.

### What limits that result

- **It measured an older tool.** The fixes of 2026-09-28 changed the parsers, how a DOI's
  existence is decided, the arXiv path and the title scoring. The numbers above describe the
  tool as of 2026-09-02. Re-run before quoting them for the current one.
- **The sample was filtered.** `sample.py` asked Crossref for `has-full-text:false`, which
  keeps only records with no full-text link and so likely under-samples the large publishers
  that deposit them. That was not disclosed until 2026-09-28. The filter is gone from
  `sample.py`; a new run will draw a different, less skewed sample.
- **It cannot be rebuilt from this repository.** `sample.py` draws at most 260 works. The 22
  legacy-DOI and 90 non-Latin works came from sampling code that was never committed, and
  neither was the manifest. The next run should commit both.
- **The new arms have not been run against live Crossref.**

## What this does not measure

**Every work in the sample is Crossref-registered, so its DOI always resolves.** The
measurement therefore cannot reach the class where `FABRICATED` false positives actually
live: work registered with another agency, or not registered at all. ACM's `10.5555/*` range
is the case that broke this tool once already. The control arm is the closest proxy, and it
put the residual at 4 in 40 when a real paper's identifier is dead: the title rescue failed
for generic titles ("Nephrology news") and for records Crossref's own search does not rank
in the top ten. The rescue now also asks arXiv, which that run predates.

**False negatives are not measured at all.** That needs labelled fabrications; `CiteAudit`
(6086 instances) and `CiteCheck` (982) are the published options.
