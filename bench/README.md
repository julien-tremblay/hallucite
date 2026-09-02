# False-positive measurement

The one number that matters for a tool whose promise is "it will not falsely accuse you",
and the one this repository did not have until 2026-09-02.

```
python3 bench/sample.py manifest.json     # draw real works from Crossref
python3 bench/build.py manifest.json .    # raw.bib, perturbed.bib, control.bib
python3 hallucite.py raw.bib
python3 hallucite.py perturbed.bib
python3 hallucite.py control.bib
```

Every work in `raw.bib` and `perturbed.bib` is real by construction: Crossref returned it,
so the DOI is registered and the title is the registry's own. The correct output is zero
`FABRICATED` and zero `MISMATCH`, and any hit is a false accusation with a diagnosis
attached.

`perturbed.bib` is the measurement that matters. A raw round-trip compares exact strings to
themselves and is close to a tautology; the perturbed set deforms the same works the way
real bibliographies actually differ from the registry: LaTeX-escaped accents, dropped
subtitles, case changes, a trailing period on the DOI, an indented closing brace.

`control.bib` exists so the run can fail. Without it, a verifier that returned `OK`
unconditionally would score a perfect false-positive rate.

## Result, 2026-09-02

370 works: 258 stratified across four decades and four Crossref types, plus 22 with the
pre-2008 `<>#+` suffix charset and 90 with non-Latin titles.

| Set | n | False positives |
|---|---|---|
| raw | 370 | **0 (0.00%)** |
| perturbed | 370 | **0 (0.00%)** |

| Control arm | n | Result |
|---|---|---|
| broken DOI + real title (want `BAD-DOI`) | 40 | 36 rescued, 4 called `FABRICATED` |
| broken DOI + invented title (want `FABRICATED`) | 40 | 38 caught, 2 `UNCHECKABLE`, **0 passed as OK** |

## What this does not measure

**Every work in the sample is Crossref-registered, so its DOI always resolves.** The
measurement therefore cannot reach the class where `FABRICATED` false positives actually
live: work registered with another agency, or not registered at all. ACM's `10.5555/*` range
is the case that broke this tool once already. The control arm is the closest proxy, and it
puts the residual at 4 in 40 when a real paper's identifier is dead: the title rescue fails
for generic titles ("Nephrology news") and for records Crossref's own search does not rank
in the top ten.

**False negatives are not measured at all.** That needs labelled fabrications; `CiteAudit`
(6086 instances) and `CiteCheck` (982) are the published options.
