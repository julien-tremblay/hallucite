# Changelog

## 2026-09-28: audit fixes

An audit ran the tool against a mock registry, a local HTTP server and planted defects. It
reproduced twelve ways a real, correctly cited reference came back `FABRICATED` or
`MISMATCH`, and several ways the gate passed when it should not. Most traced to two causes:

- **The parsers did not share the network layer's caution.** A registry that failed to answer
  could never accuse, but a title the parser had guessed wrong went straight into the
  comparison, and a wrong title against a correct DOI is a `MISMATCH`.
- **Guards added to one path were never ported to its siblings.** The non-JSON guard and the
  empty-title guard protected Crossref, but not arXiv or doi.org.

Everything below is locked by a test that fails without the fix, and `tests/mutants.py`
re-plants each defect to prove it.

### False accusations removed

| Input | Was | Now |
|---|---|---|
| A plainnat `.bbl`: the `\emph{}` venue was read as the title | `MISMATCH` on every reference with a DOI | titles from the first `\newblock` |
| `Schr\"odinger ... G\"odel`: umlaut escapes read as quotation marks | `MISMATCH` | `\"` is not a quote |
| `title = "{\"U}ber ..."`: the quoted field ended inside the braces | `MISMATCH` (title `{\`) | braces are respected |
| `title = mymacro`: `@string` macros never expanded | `MISMATCH` against "mymacro" | expanded, with `#` concatenation |
| Any `\emph{}` title guess that disagreed with the registry | `MISMATCH` | `SUSPECT`: a guess is not evidence |
| An IEEE Xplore or Zenodo URL, or a DOI's digits, taken as an arXiv id | `FABRICATED` | ids only where cited as one |
| arXiv answering 200 with a rate-limit page | `FABRICATED` | `UNCHECKABLE`, fails the gate |
| doi.org redirecting to a publisher page that 404s | `FABRICATED` for a registered DOI | existence asked of the Handle API |
| A DataCite record with an empty title | `MISMATCH` | `UNCHECKABLE` (soft) |
| A dead DOI next to a live arXiv id in the same entry | `FABRICATED` | `BAD-DOI` |
| A dead DOI or arXiv id on a paper Crossref does not hold | `FABRICATED` | rescue asks arXiv too |
| `doi = {http://dx.doi.org/...}`, `doi:`, `DOI:` | `FABRICATED` unless rescued | read |
| `10.1162/tacl\_a\_00349` (escaped underscores: TACL, Neural Computation) | `FABRICATED` unless rescued | unescaped |
| A title with no comparable characters | `MISMATCH` at 0.00 | treated as no title |

### The gate no longer passes when it should not

- A natbib or biblatex paper (`\citep`, `\citet`, `\parencite`, `\autocite`, `\textcite`,
  `\cite[p.~3]{}`) run without its `.bib` printed "no citation markers" and **exited 0 under
  `--gate`**. It is a hard parser failure now, as promised.
- A file mixing BibTeX with other references was parsed as BibTeX only, and the rest vanished.
  So did `@article(...)` entries, and `@ARTICLE` outside a `.bib`.
- In Markdown and bare `.tex`, `OK` meant only "this identifier exists", so a DOI pointing at a
  different paper passed silently. Those lines are now marked `[identifier only]` and counted.

### The 0.90 bar that rescues a real paper from a dead DOI

These never decided `MISMATCH`, but they kept real titles below the bar that separates
`BAD-DOI` from `FABRICATED`:

- `difflib`'s autojunk scored a one-word edit to a 250-character title at 0.54.
- Registry markup (`CO<sub>2</sub>`, JATS, MathML, `&amp;`) against TeX (`CO$_2$`, `$\alpha$`).
- `{\o}`, `{\ss}`, `{\L}`, `\'{\i}` shattered their words, and braces split `{BERT}ology`.
- Only `title[0]` was read; Crossref's separate `subtitle` and `original-title` are compared now.

### Robustness

- JSON of the wrong shape, one reference's exception, or a non-UTF-8 stdout (Windows in CI)
  crashed the whole run with exit 1. Each is now contained to one `UNCHECKABLE` line.
- Timeouts and dropped connections are retried like 429 and 503.
- arXiv is queried over https, three seconds apart as its API asks.
- A registered DOI whose agency offers no metadata used to fail `--gate` on every run. It is a
  permanent gap and is soft now.
- A DOI with a `..` path segment is not sent, because the server would resolve it into a
  request for a different DOI.

### Tests and measurement

- The offline suite crashed on any machine exporting `https_proxy` and left a file in
  `tests/`. Three of its checks could not fail for the reason they named. Thirteen planted
  defects all passed it; none do now.
- The false-positive number was "never, on 740 references". That was 370 works checked twice,
  bounded below about 0.8% at 95% confidence, drawn with an undisclosed `has-full-text:false`
  filter, partly from code not in the repository, and measured before these fixes.
  `bench/README.md` now says so.
- `bench/` has three new arms (`deformed.bib`, `rescue.bib`, `raw.bbl`) that reach the
  classes above. On a synthetic manifest the previous tool fails two of them. They have not
  been run against live Crossref.

### Not verified here

The audit environment could not reach Crossref, doi.org or arXiv. The Handle API path, the
arXiv error-entry format and the arXiv title search follow those services' documented
behaviour, and are covered offline by fakes. Run `python3 hallucite.py --selftest` (case
`t7` exercises the Handle API) and the benchmark before relying on them.
