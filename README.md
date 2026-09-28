# hallucite

Find fabricated and mismatched citations. Deterministic, no LLM, no API key, no install.

Language models invent references. They invent plausible authors, plausible titles, and
DOIs with the right shape that point at nothing, or at a different paper entirely. The
second kind is worse, because it survives a careful read.

`hallucite` resolves every reference in a `.bib`, `.bbl`, `.tex`, or `.md` file against
the authoritative registries (Crossref and doi.org for DOIs, arXiv for eprints, Crossref and
arXiv for titles) and tells you which ones do not exist. A file may mix forms: BibTeX
entries, `\bibitem` entries and bare identifiers are all read.

```
$ hallucite paper.bib

== paper.bib (42 refs) ==
  [ ok ] devlin2019bert     DOI resolves, title match 0.99
  [FABR] chen2007fiber      DOI 10.1109/JLT.2007.899999 does not resolve at doi.org
  [MISM] wuttke2003noise    DOI resolves to a DIFFERENT title (match 0.21)
  [BDOI] vaswani2017        paper is real, DOI 10.5555/3295222.3295349 resolves nowhere
  [SUSP] smith2019book      no close Crossref match for title
  [??? ] internal2024       no DOI, arXiv id, or usable title

summary: 2 hard (fabricated/mismatch), 3 soft (suspect/uncheckable)
```

## Install

There is nothing to install. One file, Python 3.9+, standard library only. Both claims are
checked on every push: CI runs the offline suite on 3.9 and 3.12 and asserts that the module
imports nothing outside the standard library.

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
means a usage error (unknown flag, unreadable file), never a verdict. A malformed registry
answer, a slow one, a terminal that cannot print a title, or a bug on one reference does not
end the run: that reference is reported `UNCHECKABLE` with the reason, fails `--gate` like any
check that did not run, and the rest are still checked.

Drop it in a pre-commit hook or CI step and a fabricated reference stops being something
you find out about from a reviewer.

## What the classes mean

| Class | Meaning | Gate |
|---|---|---|
| `FABRICATED` | Identifier resolves nowhere **and** no record matches the title | hard fail |
| `MISMATCH` | Resolves, but to a **different title** | hard fail |
| `BAD-DOI` | The paper is real; the identifier resolves nowhere | warn |
| `SUSPECT` | Title-only reference with no close Crossref match, or an identifier that resolves to a title disagreeing with one the parser could only guess | warn |
| `UNCHECKABLE` | No DOI, arXiv id, or usable title | warn |
| `UNCHECKABLE` | A registry could not be reached, or a request could not be sent safely | **hard fail** under `--gate` |
| `OK` | Resolved and the title matches | pass |

## How often does it accuse you wrongly

Measured once, on 2026-09-02: no false accusation on 370 real works, each checked twice. Zero
in 370 does not make the rate zero; it bounds it below about 0.8% per reference at 95%
confidence. That measurement predates the fixes of 2026-09-28, which changed the parsers, the
DOI path and the title scoring, so it needs a re-run before it describes the current tool.

`bench/` draws a random sample of real works from Crossref, so every reference in it is real
by construction and any hard finding is a false accusation with a diagnosis attached. It
takes about ten minutes and needs no key.

| Set (measured 2026-09-02) | works | False positives |
|---|---|---|
| registry titles verbatim | 370 | **0** |
| the same works, deformed the way real bibliographies are | 370 | **0** |

The sample covers four decades and four Crossref types, and includes 22 DOIs carrying the
pre-2008 `<>#+` suffix charset and 90 non-Latin titles. The deformed set adds LaTeX-escaped
accents, dropped subtitles, case changes, a trailing period on the DOI, and an indented
closing brace. Two caveats on how it was drawn: it filtered on `has-full-text:false`, which
likely under-samples the large publishers and was not disclosed until 2026-09-28, and the 112
legacy and non-Latin works came from sampling code that is not in this repository.

A control arm runs alongside so the measurement can fail. Without it, a verifier that
returned `OK` unconditionally would score perfectly. It fired: 38 of 40 invented references
caught with **none passed as `OK`**, and 36 of 40 real papers with deliberately broken DOIs
correctly rescued as `BAD-DOI` rather than accused.

**What it could not see.** Both measured sets cite the registry's own title, so registry
markup (`CO<sub>2</sub>`) was only ever compared with itself, a subtitle Crossref stores
apart was never cited, the rescue of a dead DOI was only tried on verbatim titles, and no
`.bbl` was ever parsed. The 2026-09-28 audit found false accusations in every one of those
places. `bench/` now builds three more arms that reach them: deformed titles, dead DOIs with
deformed titles, and the `.bbl` BibTeX writes for natbib users. On a four-work synthetic
manifest, offline, the 2026-09-02 tool scores zero on the two published arms and still calls
three of the four real papers `FABRICATED` in the rescue arm and `MISMATCH` in the `.bbl` arm.
The new arms have not been run against live Crossref yet.

**What this does not cover, and it is the important part.** Every sampled work is
Crossref-registered, so its DOI always resolves. The measurement structurally cannot reach
the class where `FABRICATED` false positives actually live: work registered with another
agency, or not registered at all. The control arm is the closest proxy and put the residual
at 4 in 40 when a real paper's identifier is dead, failing on generic titles ("Nephrology
news") and on records Crossref's own search does not rank in its top ten. **False negatives
are not measured at all.**

Full method and caveats: [`bench/README.md`](bench/README.md).

## Design commitments

**It will not falsely accuse you.** If the registry is unreachable, a reference is reported
`UNCHECKABLE`, never `FABRICATED`, and the tool refuses to fall back to fuzzy title matching
to fill the gap. A verifier that cries fraud during a network outage is worse than no
verifier, because you stop trusting it exactly when it is right.

**A dead identifier is not a dead reference.** Publishers mistype, retire and never register
DOIs for work that plainly exists, so a DOI or arXiv id that resolves nowhere is checked
against its title, in Crossref and on arXiv, and against any other identifier in the same
entry, before any verdict is passed. If the paper is real, you get `BAD-DOI`, which says fix
the identifier, not `FABRICATED`, which says you made this up. ACM's `10.5555/*` range is the
case that matters: `10.5555/3295222.3295349` is *Attention Is All You Need*, and it 404s.
Conference papers that carry these DOIs are mostly not in Crossref at all, which is why arXiv
is asked too. The bar for that rescue is 0.90 rather than the 0.60 used elsewhere, because a
fabricated reference almost always carries a plausible title and a loose bar would launder
exactly what this tool exists to catch. If one of the registries asked cannot answer and the
others found nothing, the result is `UNCHECKABLE`, not a verdict.

**The gate fails closed.** If no registry answered, `--gate` exits 1 and says so. Counting
an unreachable oracle as a soft pass meant a fully offline run went green having verified
nothing at all, which is the same reassuring green as a clean bibliography. A reference that
simply carries no identifier is a different thing, and stays soft.

**An identifier is only read where it is cited as one.** An arXiv id comes from an `eprint`
field, an `arXiv:` prefix, an arxiv.org URL or an arXiv DOI, never from whatever id-shaped
text an entry contains. Scanning the whole entry turned an IEEE Xplore URL into the arXiv id
`document/8765432`, and a real paper into `FABRICATED`.

**A registry's own gaps are not accusations.** A Crossref or DataCite record with an empty
title, or a registered DOI whose agency offers no readable metadata, reports `UNCHECKABLE`
and says why. Comparing a cited title against an empty one scores 0.00, which used to read
as `MISMATCH`. These gaps are permanent, so they do not fail `--gate`: a gate that fails on
every run for a reason nobody can fix gets switched off.

**Whether a DOI exists is asked of the DOI system.** For a DOI Crossref does not hold, the
DOI system's Handle API says whether it is registered at all. This used to be inferred from
content negotiation at doi.org, which answers by redirecting, sometimes to the publisher's
own page; a publisher's 404 on a moved page then read as an unregistered DOI, and a real one
came back `FABRICATED`.

**Typography is not a different title.** `CO$_2$` and `CO<sub>2</sub>`, `F\'{\i}sica` and
`Física`, `{BERT}ology` and `BERTology` are compared as the same words: TeX accents, letters,
math and braces, and the registry's HTML, JATS and MathML markup, are folded away first. A
registry's subtitle and original-language title are compared too. These differences never
decided `MISMATCH` at 0.60, but they kept real titles below the 0.90 bar that rescues a
paper from a dead DOI, which is the difference between `BAD-DOI` and `FABRICATED`.

**A guessed title is not evidence.** In a `\bibitem`, `\emph{}` holds the title in some
styles and the journal or "et al." in others, and nothing in the entry says which. A title
the parser had to guess can raise `SUSPECT`, never `MISMATCH`. Titles it can locate
unambiguously (a `title` field, ``` ``quoted'' ```, `\bibinfo{title}`, the first `\newblock`
of a `.bbl`) still can. Before this, the `.bbl` BibTeX writes for natbib users had its
`\emph{}` venue read as the title, and every reference carrying a DOI came back `MISMATCH`.

**A parser failure is not a clean bill of health.** If a file plainly contains citations
(`\cite` and every natbib or biblatex variant, `\bibliography`, `\bibitem`, a BibTeX entry)
and zero references are parsed from any input, that is reported as a hard failure rather
than "0 fabricated." Silent
degradation into a reassuring green is the specific failure this tool exists to prevent.

**Deterministic beats a model here.** The HALLMARK study found a deterministic DOI/bibtex
verifier (F1 0.908) outperforms the best zero-shot LLM (0.840) at catching fabricated
citations, and that giving the model tool access made it *worse*. There is no model in this
program, so there is nothing to hallucinate.

## Known limitations

- Books, theses, standards, and non-indexed venues often have no DOI and land in `SUSPECT`
  or `UNCHECKABLE`. That is a prompt to look, not a verdict.
- **In Markdown, and in `.tex` outside a bibliography, only identifiers are read**: DOIs,
  `arXiv:` ids and arxiv.org links. With no title to compare, `OK` means the identifier
  exists, not that it is the paper you meant, so a DOI pointing at a different paper passes.
  Every such line is marked `[identifier only]` and the summary counts them. Put your
  references in BibTeX or `\bibitem` form to get the title check.
- **Titles shorter than two characters, and `\bibitem` entries whose title is not quoted,
  tagged with `\bibinfo{title}`, set off by `\newblock` or emphasised, parse without a
  title.** The DOI is still checked; the title comparison simply does not run, and the
  reference reads `OK` on resolution alone. An emphasised title is a guess and can only
  ever produce `SUSPECT`.
- **It catches invented and swapped references, not subtly altered ones.** Title comparison
  is lexical, so "Attention Is Not All You Need" scores 0.93 against "Attention Is All You
  Need" and passes, as does Recognition -> Segmentation. Authors, venue and year are parsed
  but never compared. If your concern is a real paper cited with mangled metadata rather
  than a paper that does not exist, this is not the tool.
- Title matching is fuzzy (difflib ratio). A heavily abbreviated title can read as a
  `MISMATCH`.
- arXiv asks API clients for one request every three seconds, and the tool obeys, so a
  bibliography with fifty arXiv references takes a few minutes.
- It checks that a reference *exists and matches*. It cannot check that the reference
  *supports the claim it is attached to*.

## What I would like you to break

This is the useful part of opening it. Feedback that changes the tool is worth far more than
a star, and these are the places I already believe it is weakest. If you confirm any of them
with a concrete case, that is a bug report I can act on.

1. **Run it on a bibliography full of non-Crossref DOIs** (DataCite, an institutional
   repository, Zenodo, a national aggregator). The false-positive measurement cannot reach
   that class at all, so this is the largest genuinely unknown area.
2. **Run it on a bibliography that is not in English.** 90 non-Latin titles were tested one
   at a time against their own registry records. No real Japanese, Chinese, Russian or Greek
   bibliography has been run through it end to end.
3. **Tell me the thresholds are wrong.** A title scores 0..1; 0.60 separates `OK` from
   `MISMATCH` and 0.90 is the bar for saying two titles are the same work. Both were chosen
   to satisfy cases I had, not derived from anything.
4. **Break the parsers.** RIS, Zotero and Mendeley exports, biblatex `@online` and
   `crossref`-inherited fields are untested. `.bbl` files from the standard BibTeX styles,
   ACM's and REVTeX's `\bibinfo` form, `@string` macros and `@article(...)` entries have
   fixtures now, but only hand-built ones. A file that plainly
   contains references and yields zero is reported as a parser failure, but a file that
   yields *some* is not, and silent partial loss is the defect this tool exists to prevent.
5. **Argue with `BAD-DOI`.** Splitting "this identifier is dead" from "this paper does not
   exist" may be over-generous. A fabricated reference carrying a plausible real title gets
   downgraded from `FABRICATED` to a warning, and I am not certain 0.90 is high enough.
6. **Tell me it should compare authors and years.** It parses both and compares neither.
   Several other tools do, and that is the whole "real paper, mangled metadata" class this
   one misses by design.

Open an issue with the input that broke it, or a failing case in `tests/`. Disagreement
about the design is as welcome as a bug; see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Tests

```
python3 tests/test_regressions.py   # offline, no network, ~1s. This is what CI runs.
python3 hallucite.py --selftest     # live, hits Crossref and arXiv, ~10s
```

Every case in the offline suite is a defect that actually shipped. It runs on every push, on
Python 3.9 and 3.12. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the false-positive
measurement and what a useful patch looks like.

## License

MIT.
