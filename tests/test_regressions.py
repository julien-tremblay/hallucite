#!/usr/bin/env python3
"""Offline regression tests. No network, so they run in CI and in a hook.

Every case here is a defect that actually shipped. The live behaviour is covered by
`hallucite.py --selftest`, which does hit Crossref and arXiv.
"""
import ast
import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import hallucite as H  # noqa: E402

FAILS = []
HAL = str(pathlib.Path(__file__).resolve().parent.parent / "hallucite.py")
# Nothing in this process talks to a real registry, so nothing should wait for one. The
# arXiv client spaces requests three seconds apart, as arXiv asks, and without this every
# in-process arXiv lookup made the suite sleep for real. Tests that time things patch it.
H.time.sleep = lambda s: None


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


class Registry:
    """A fake registry standing in for H._get: Crossref works and search, doi.org, arXiv.

    Anything it does not know is a 404, which is what the real registries answer. The
    earlier tests each patched H._get with a one-off lambda, which is how the MISMATCH
    branch -- the point of the tool -- went without a single offline test."""

    def __init__(self, crossref=None, search=(), doi_org=None, arxiv=None, arxiv_raw=None,
                 arxiv_search=()):
        self.crossref = crossref or {}   # doi -> title, or a full Crossref `message` dict
        self.search = list(search)       # titles a Crossref bibliographic query returns
        self.doi_org = doi_org or {}     # doi -> CSL-JSON, for DOIs outside Crossref
        self.arxiv = arxiv or {}         # id -> title
        self.arxiv_raw = arxiv_raw       # a raw arXiv response body, to simulate outages
        self.arxiv_search = list(arxiv_search)  # titles an arXiv title search returns
        self.calls = []

    def __call__(self, url, accept="application/json"):
        self.calls.append(url)
        missing = urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if "api.crossref.org/works?" in url:
            items = [{"title": [t]} for t in self.search]
            return json.dumps({"message": {"items": items}}), 200
        if "api.crossref.org/works/" in url:
            doi = urllib.parse.unquote(url.split("/works/")[1].split("?")[0])
            rec = self.crossref.get(doi)
            if rec is None:
                raise missing
            return json.dumps({"message": {"title": [rec]} if isinstance(rec, str) else rec}), 200
        if url.startswith("https://doi.org/"):
            doi = urllib.parse.unquote(url[len("https://doi.org/"):])
            if doi not in self.doi_org:
                raise missing
            return json.dumps(self.doi_org[doi]), 200
        if "export.arxiv.org" in url:
            if self.arxiv_raw is not None:
                return self.arxiv_raw, 200
            if "search_query=" in url:
                entries = "".join(f"<entry><title>{t}</title></entry>" for t in self.arxiv_search)
                return f"<feed><title>ArXiv Query</title>{entries}</feed>", 200
            aid = urllib.parse.unquote(url.split("id_list=")[1].split("&")[0])
            entry = f"<entry><title>{self.arxiv[aid]}</title></entry>" if aid in self.arxiv else ""
            return f"<feed><title>ArXiv Query</title>{entry}</feed>", 200
        raise AssertionError(f"unexpected URL {url}")


@contextlib.contextmanager
def registry(reg):
    real_get, real_sleep = H._get, H.time.sleep
    H._get, H.time.sleep = reg, (lambda s: None)
    try:
        yield reg
    finally:
        H._get, H.time.sleep = real_get, real_sleep


def verify(reg, **ref):
    with registry(reg):
        return H.verify({"doi": "", "arxiv": "", "title": "", "year": "", "key": "k", **ref})


def run_main(files, *flags, reg=None):
    """Run the CLI in-process on {filename: text}; return (exit code, stdout)."""
    with tempfile.TemporaryDirectory() as d, registry(reg or Registry()):
        paths = []
        for name, text in files.items():
            paths.append(os.path.join(d, name))
            pathlib.Path(paths[-1]).write_text(text, encoding="utf-8")
        out, argv = io.StringIO(), sys.argv
        sys.argv = ["hallucite", *paths, *flags]
        try:
            with contextlib.redirect_stdout(out):
                H.main()
        except SystemExit as e:
            return e.code, out.getvalue()
        finally:
            sys.argv = argv
    raise AssertionError("main() returned without exiting")


# 1. Trailing punctuation is a prose habit, not part of the DOI. Verified 2026-09-01:
#    10.1038/nature14539 -> HTTP 200, "10.1038/nature14539." -> HTTP 404. Leaving the
#    period attached reported a real paper as FABRICATED, a false accusation under --gate.
for raw, want in [
    ("10.1038/nature14539.", "10.1038/nature14539"),
    ("10.1038/nature14539,", "10.1038/nature14539"),
    ("10.1038/nature14539);", "10.1038/nature14539"),
    ("<10.1038/nature14539>", "10.1038/nature14539"),
    ("  10.1038/nature14539  ", "10.1038/nature14539"),
    ("10.1038/nature14539", "10.1038/nature14539"),
]:
    check(f"clean_doi({raw!r})", H.clean_doi(raw) == want, f"got {H.clean_doi(raw)!r}")

# 2. Same, through the bibtex parser, which was the path that did NOT strip.
refs = H.parse_bib("@article{a,\n  title = {Deep learning},\n  doi = {10.1038/nature14539.}\n}\n")
check("bibtex doi field is normalised",
      len(refs) == 1 and refs[0]["doi"] == "10.1038/nature14539",
      f"got {refs and refs[0].get('doi')!r}")

# 3. The worst historical defect: the entry regex stopped before the final newline while
#    field() required one, so the LAST field of every entry was invisible. When `title`
#    came last it parsed empty, and every resolving DOI returned OK. That silently
#    disabled MISMATCH detection, which is the whole point of the tool.
refs = H.parse_bib("@article{b,\n  doi = {10.1038/nature14539},\n  title = {Deep learning}\n}\n")
check("last field of an entry is visible (title last)",
      len(refs) == 1 and refs[0]["title"].strip().lower() == "deep learning",
      f"got {refs and refs[0].get('title')!r}")

# 4. A public release ships one language. French leaked into user-visible output.
#    This test USED to grep the source for the four French words that one past fix had
#    removed. It passed while seven other French strings sat in user-visible output, because
#    it pinned the fixed instances instead of the property. Now it reads the string literals
#    with ast -- comments may discuss French, output may not -- and proves it can still fail.
src = (pathlib.Path(__file__).resolve().parent.parent / "hallucite.py").read_text()
FRENCH = re.compile(r"(?i)\b(resou\w*|erreur|reponse|tronque|titre|aucune|introuvable"
                    r"|lisible|concorde|fichier|agence(?!s? \(en)|irresoluble)\b")
lits = [n.value for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
fr = sorted({w for lit in lits for w in FRENCH.findall(lit)})
check("no French in user-visible strings", not fr, f"found {fr}")
check("...and that check can still fail",
      bool(FRENCH.findall("DOI resout a un titre DIFFERENT")))

# 5. A file whose citations live in a sibling file is the normal LaTeX layout. Judging
#    each input alone made paper.tex a hard PARSER FAILURE next to its own refs.bib.
#    This used to assert `"any_refs" in src`, which a mutation pass showed passes with the
#    pooling deleted, and with a parser failure counted soft. Both are now run.
TEX = "\\documentclass{article}\\begin{document}\\cite{lecun}\\end{document}\n"
BIB = "@article{lecun, title={Deep learning}, doi={10.1038/nature14539}}\n"
NATURE = Registry(crossref={"10.1038/nature14539": "Deep learning"})
rc, out = run_main({"paper.tex": TEX}, "--gate", reg=NATURE)
check("citations with zero parsed references fail the gate", rc == 1 and "PARSER FAILURE" in out,
      f"got exit {rc}")
rc, out = run_main({"paper.tex": TEX, "refs.bib": BIB}, "--gate", reg=NATURE)
check("...but not when a sibling input supplies the references", rc == 0, f"got exit {rc}: {out}")


# --- 2026-09-02 adversarial pass -------------------------------------------------------

# 6. Entry layout must not decide whether a reference exists. The old regex needed the
#    closing brace to start a line, so an indented `}`, a `}}` riding on the last field, and
#    a one-line entry were all INVISIBLE. Measured: a two-entry file whose second entry
#    carried a fabricated DOI and an indented brace passed --gate with exit 0. Silent
#    partial loss is precisely what this tool exists to prevent.
for name, text in [
    ("indented closing brace", "@article{a,\n  title = {T},\n  doi = {10.1/x}\n  }\n"),
    ("closing brace on the last field's line", "@article{a,\n  doi = {10.1/x},\n  title = {T}}\n"),
    ("entry on one line", "@article{a, title = {T}, doi = {10.1/x}}\n"),
]:
    check(f"parses: {name}", len(H.parse_bib(text)) == 1, f"got {len(H.parse_bib(text))}")
two = H.parse_bib("@article{a,\n title={T}\n}\n@article{b,\n title={U}\n  }\n")
check("no entry is silently dropped from a mixed file", len(two) == 2, f"got {len(two)}")

# 7. `title` matched inside `booktitle` and `journaltitle`, and whichever came first won, so
#    the PROCEEDINGS or JOURNAL name was checked against the DOI and a CORRECT reference was
#    reported MISMATCH. A parser that manufactures false positives fails a correct paper
#    under --gate, which is the same defect as one that passes a wrong one.
for name, field in [("booktitle", "booktitle"), ("journaltitle", "journaltitle"),
                    ("shorttitle", "shorttitle")]:
    r = H.parse_bib("@inproceedings{a,\n  %s = {The Venue Name},\n"
                    "  title = {The Real Title},\n  doi = {10.1/x}\n}\n" % field)
    check(f"{name} before title does not steal it",
          r and r[0]["title"] == "The Real Title", f"got {r and r[0]['title']!r}")

# 8. Pre-2008 DOIs legally contain < > # +. Wiley's SICI form is not exotic: 99 of 100
#    Angewandte Chemie records from 2000-2002 carry one. The character class truncated them
#    at the '<', and two verifiably real papers came back FABRICATED -- a false accusation,
#    the worst output this tool has. Verified against live Crossref 2026-09-02.
legacy = "10.1002/1521-3757(20010316)113:6<1113::aid-ange11130>3.0.co;2-c"
r = H.parse_inline(f"See {legacy} for details.")
check("legacy DOI survives inline extraction",
      r and r[0]["doi"] == legacy, f"got {r and r[0]['doi']!r}")
# A legacy DOI's own brackets are balanced, so an unbalanced closer came from the prose
# around it. Stripping closers unconditionally, as this did, truncated the identifier.
for name, prose in [("prose parentheses", f"(see {legacy}) and more"),
                    ("sentence period", f"See {legacy}."),
                    ("markdown autolink", f"<https://doi.org/{legacy}>"),
                    ("markdown link", f"[ref](https://doi.org/{legacy})")]:
    got = H.parse_inline(prose)
    check(f"legacy DOI survives {name}", got and got[0]["doi"] == legacy,
          f"got {got and got[0]['doi']!r}")

# 9. @misc entries park the DOI in `note`. The arXiv branch already scanned the whole entry;
#    the DOI branch did not, so a reference carrying a resolvable DOI in plain sight came
#    back UNCHECKABLE.
r = H.parse_bib("@misc{a,\n  author = {X},\n  note = {Proc. R. Soc. A. DOI:10.1098/rspa.2020.0063}\n}\n")
check("DOI in a note field is found", r and r[0]["doi"] == "10.1098/rspa.2020.0063",
      f"got {r and r[0]['doi']!r}")

# 10. AN ORACLE THAT DID NOT ANSWER MUST NOT CLEAR A REFERENCE. verify() detected outages by
#     looking for "error"/"HTTP" in a human sentence. A non-JSON response said neither, so a
#     fabricated DOI fell through to fuzzy title matching and a generic title scored a close
#     match against something unrelated -- returning OK. (The French "erreur" does not
#     contain "error" either.) The sentinel is checked, not guessed at.
import json as _json
_real = H._get
H._get = lambda u, accept="application/json": (
    ("<html>503 Service Unavailable</html>", 200) if "/works/" in u
    else (_json.dumps({"message": {"items": [{"title": ["A Plausible Nearby Title"]}]}}), 200))
cls, why = H.verify({"doi": "10.9999/fabricated", "arxiv": "", "year": "",
                     "title": "A Plausible Nearby Title", "key": "x"})
H._get = _real
check("a non-JSON registry response cannot produce OK", cls == "UNCHECKABLE", f"got {cls}: {why}")
check("...and it is flagged with the machine-readable sentinel", why.startswith("DOI " + H.NO_ORACLE))

# 11. A gate must fail closed. Offline, every reference came back UNCHECKABLE, that counted
#     as a soft pass, and --gate exited 0 having verified nothing at all -- green because the
#     oracle was down. A reference with no identifier to check is a different thing and stays
#     soft. Measured 2026-09-02 with the network blackholed: two fabricated DOIs, exit 0.
#     The network is cut by pointing every proxy variable at a closed port. This used to
#     build the environment with dict(**os.environ, https_proxy=...), which raises TypeError
#     on any machine that already exports https_proxy -- every corporate network -- and the
#     crash left the fixture behind in tests/. no_proxy is dropped so it cannot exempt a host.
PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
off = {k: v for k, v in os.environ.items() if k.lower() != "no_proxy"}
off.update({k: "http://127.0.0.1:9" for k in PROXY_VARS})
with tempfile.TemporaryDirectory() as d:
    bib = pathlib.Path(d, "gate.bib")
    bib.write_text("@article{a,\n  title = {T},\n  doi = {10.9999/nope}\n}\n")
    rc = subprocess.run([sys.executable, HAL, str(bib), "--gate"],
                        capture_output=True, env=off).returncode
check("--gate fails closed when no registry answers", rc == 1, f"got exit {rc}")

# 12. A misspelled flag was dropped silently, so --gates ran advisory and exited 0 while the
#     caller believed they were gating. And an unreadable path raised, exiting 1 -- which
#     under --gate is indistinguishable from "this bibliography contains fabrications".
#     The flag case used to pass `x.bib`, which does not exist, so it exited 2 for the
#     missing file and still passed with the flag check deleted. The file is real now, and
#     the same file without the typo must NOT exit 2.
with tempfile.TemporaryDirectory() as d:
    empty = pathlib.Path(d, "empty.bib")
    empty.write_text("% no entries\n")
    rc_typo = subprocess.run([sys.executable, HAL, str(empty), "--gates"], capture_output=True).returncode
    rc_ok = subprocess.run([sys.executable, HAL, str(empty), "--gate"], capture_output=True).returncode
check("an unknown flag is rejected, not ignored", rc_typo == 2 and rc_ok == 0,
      f"got {rc_typo} with the typo, {rc_ok} without")
check("an unreadable path exits 2, not 1",
      subprocess.run([sys.executable, HAL, "/nope/missing.bib", "--gate"],
                     capture_output=True).returncode == 2)


# --- round 2 --------------------------------------------------------------------------

# 13. norm() kept only [a-z0-9], which deletes every character of a title written in
#     Japanese, Chinese, Cyrillic, Greek, Arabic, Hebrew or Korean. Two IDENTICAL non-Latin
#     titles scored 0.00 and the reference was reported MISMATCH -- a hard failure, under
#     --gate, on a correct citation.
for t in ["\u91cf\u5b50\u8a08\u7b97\u306e\u57fa\u790e", "\u041a\u0432\u0430\u043d\u0442\u043e\u0432\u0430\u044f \u043a\u0440\u0438\u043f\u0442\u043e\u0433\u0440\u0430\u0444\u0438\u044f",
          "\u0398\u03b5\u03c9\u03c1\u03af\u03b1 \u03c4\u03c9\u03bd \u03c0\u03b1\u03b9\u03b3\u03bd\u03af\u03c9\u03bd", "\ud55c\uad6d\uc5b4 \uc81c\ubaa9", "\u0646\u0638\u0631\u064a\u0629 \u0627\u0644\u0643\u0645"]:
    check(f"a non-Latin title matches itself ({t[:6]})", H.title_match(t, t) >= 0.6,
          f"got {H.title_match(t, t):.2f}")
# Accents fold rather than vanish, so a LaTeX-escaped title still matches the registry's.
check("LaTeX-escaped accents match the registry's unicode",
      H.title_match(r'{\"U}ber die Quantenmechanik', "\u00dcber die Quantenmechanik") >= 0.6)
# ...and the fold must not make everything match everything.
check("an unrelated title still mismatches",
      H.title_match("A totally unrelated title about penguins on the moon",
                    "Continuous Variable Quantum Cryptography Using Coherent States") < 0.6)

# 14. `[^{}]` could not cross the inner braces of `\emph{On {BIC} states}`, so a title with
#     LaTeX case protection or inline math was dropped entirely and the reference lost its
#     MISMATCH check -- the same nesting defect the bibtex field() parser already fixed.
r = H.parse_bibitem(r"\bibitem{k} A., \emph{On {BIC} states in optics}, 2021.")
check("emph title with nested braces is read", r and "BIC" in r[0]["title"],
      f"got {r and r[0]['title']!r}")

# 15. The quoted form is unambiguous, but a 6-character floor silently dropped ``Chaos'' and
#     ``Two''. A reference with no parsed title gets no MISMATCH check at all.
r = H.parse_bibitem("\\bibitem{a} A, ``One,'' 2001.\n\\bibitem{b} B, ``Chaos'', Nature, 2002.")
check("short quoted titles are not dropped",
      [x["title"] for x in r] == ["One", "Chaos"], f"got {[x['title'] for x in r]}")
# The guards that made the floor look necessary must still hold.
check("`et al.` is still not mistaken for a title",
      H.parse_bibitem(r"\bibitem{k} S. Ma \emph{et al.}, ``The Era of 1-bit LLMs,'' 2024.")[0]["title"]
      == "The Era of 1-bit LLMs")
check("punctuation is still not mistaken for a title",
      H.parse_bibitem(r"\bibitem{k} A., ``--,'' 2001.")[0]["title"] == "")


# --- round 3: a dead identifier is not a dead reference -------------------------------

# 16. Unordered token overlap discarded word ORDER, so different papers scored 1.00:
#     "Learning to Rank for Information Retrieval" vs "Information Retrieval for Learning
#     to Rank", and "Attention Is All You Need" vs "Is Attention All You Need?". That is how
#     a wrong Crossref record was certified as the right one.
check("word order is not ignored",
      H.title_match("Learning to Rank for Information Retrieval",
                    "Information Retrieval for Learning to Rank") < 0.60,
      f"got {H.title_match('Learning to Rank for Information Retrieval', 'Information Retrieval for Learning to Rank'):.2f}")

# 17. Containment must RESCUE a real paper cited without the registry's long subtitle (the
#     string ratio alone puts it at 0.52 and would report MISMATCH) without CERTIFYING
#     anything: a title that merely begins with the cited one, such as the art-valuation
#     paper that puns on Vaswani, must stay below the 0.90 identity bar.
check("a long added subtitle is still a match",
      H.title_match("Deep Residual Learning",
                    "Deep Residual Learning: a very long supplementary subtitle here") >= 0.60)
for other in ["Attention Is All You Need: An Analysis Of The Valuation Of Art",
              "Is Attention All You Need?"]:
    got = H.title_match("Attention Is All You Need", other)
    check(f"containment cannot certify identity ({other[:28]!r})", got < 0.90, f"got {got:.2f}")
check("an exact title still certifies",
      H.title_match("Attention Is All You Need", "Attention Is All You Need") >= 0.90)

# 18. A DOI that resolves nowhere is not proof the PAPER is invented. ACM's 10.5555 range is
#     the standard case: 10.5555/3295222.3295349 is "Attention Is All You Need" and it 404s,
#     so a flat FABRICATED accused the most-cited paper in modern machine learning of not
#     existing. Consulting the title here is not the fallback verify() refuses: that covers
#     an oracle that FAILED to answer, this one answered definitively.
import json as _j2
_real_get = H._get


def _mock(doi_404=True, best_title=None, title_oracle_ok=True):
    def g(url, accept="application/json"):
        if "query.bibliographic" in url:
            if not title_oracle_ok:
                raise urllib_error.HTTPError(url, 503, "down", {}, None)
            items = [{"title": [best_title]}] if best_title else []
            return _j2.dumps({"message": {"items": items}}), 200
        if "export.arxiv.org" in url:
            return "<feed><title>ArXiv Query</title></feed>", 200
        raise urllib_error.HTTPError(url, 404, "Not Found", {}, None)
    return g


import urllib.error as urllib_error  # noqa: E402

REF = {"doi": "10.5555/3295222.3295349", "arxiv": "", "year": "",
       "title": "Attention Is All You Need", "key": "acm"}
H._get = _mock(best_title="Attention Is All You Need")
cls, why = H.verify(dict(REF))
check("a real paper with a dead DOI is BAD-DOI, not FABRICATED", cls == "BAD-DOI", f"got {cls}: {why}")
H._get = _mock(best_title="Something Else Entirely About Penguins")
cls, why = H.verify(dict(REF))
check("a dead DOI whose title matches nothing is still FABRICATED", cls == "FABRICATED", f"got {cls}")
H._get = _mock(title_oracle_ok=False)
cls, why = H.verify(dict(REF))
check("half a check is not a verdict: dead DOI + unreachable title oracle", cls == "UNCHECKABLE", f"got {cls}")
check("...and it fails the gate", H.NO_ORACLE in why, f"got {why}")
H._get = _real_get

# 19. BAD-DOI is a defect worth fixing, not an accusation: it must not count as hard. This
#     used to grep the source for one spelling of the counting line, so a second line
#     counting BAD-DOI hard passed it. The gate is run instead. --strict is the documented
#     way to fail on soft findings, and nothing tested that it does.
ACM = "@inproceedings{v, title={Attention Is All You Need}, doi={10.5555/3295222.3295349}}\n"
rescue = Registry(search=["Attention Is All You Need"])
rc, out = run_main({"acm.bib": ACM}, "--gate", reg=rescue)
check("BAD-DOI is counted soft, never hard", rc == 0 and "[BDOI]" in out, f"got exit {rc}: {out}")
rc, _ = run_main({"acm.bib": ACM}, "--strict", "--gate", reg=rescue)
check("...and --strict turns soft findings into a failing gate", rc == 1, f"got exit {rc}")

# 19b. The rescue bar is what stops a fabricated reference with a plausible title from being
#      laundered into a warning. Lowering it from 0.90 to 0.60 passed every test, because
#      the only non-matching fixture scored near zero. A title that merely CONTAINS the
#      cited one sits at the containment ceiling, 0.85, inside that gap.
cls, why = verify(Registry(search=["Attention Is All You Need: An Analysis Of The Valuation Of Art"]),
                  doi="10.5555/3295222.3295349", title="Attention Is All You Need")
check("a containment-only title match cannot rescue a dead DOI", cls == "FABRICATED", f"got {cls}: {why}")


# 20. The identity bar must sit ABOVE the containment ceiling everywhere, or ordered
#     containment alone certifies a match. check_title's bar was 0.75, which put it INSIDE
#     the band: on real data seven title-only references flipped SUSPECT -> OK at exactly
#     0.85, among them an 1812 Hegel volume matched against a modern paper named after it.
#     A reference carrying no identifier at all is the last place a containment-only match
#     should be enough.
check("the identity bar sits above the containment ceiling",
      H.IDENTITY > H._CONTAINMENT_CEILING,
      f"IDENTITY={H.IDENTITY} ceiling={H._CONTAINMENT_CEILING}")
H._get = _mock(best_title="Science of Logic and Its Reception in Twentieth Century Analytic Philosophy")
cls, why = H.verify({"doi": "", "arxiv": "", "year": "", "title": "Science of Logic", "key": "hegel"})
check("a title-only ref is not cleared by containment alone", cls == "SUSPECT", f"got {cls}: {why}")
H._get = _mock(best_title="Science of Logic")
cls, _ = H.verify({"doi": "", "arxiv": "", "year": "", "title": "Science of Logic", "key": "hegel"})
check("...but an exact title-only match still clears", cls == "OK", f"got {cls}")
H._get = _real_get


# --- round 4: found by sweeping a private corpus, not by a fixture -------------

# 21. `{\'e}`, `\'{e}`, `\'e` and `{\c c}` all mean one letter, but norm() mapped the
#     backslash and braces to spaces, so `S{\'e}minaire de g{\'e}om{\'e}trie` tokenised as
#     ["s","e","minaire","de","g","e","om","e","trie"] -- every accented word shattered.
#     A real SGA volume scored 0.53 against its own registry record and was reported
#     MISMATCH. French and German bibliographies are full of these.
check("LaTeX accent escapes do not shatter words",
      H.norm(r"S{\'e}minaire de g{\'e}om{\'e}trie")[:3] == ["seminaire", "de", "geometrie"],
      f"got {H.norm(chr(83) + chr(123) + chr(92) + chr(39) + 'e}minaire')[:3]}")
for form, plain in [(r"Poincar{\'e} recurrence", "Poincar\u00e9 recurrence"),
                    (r'{\"U}ber die Quantenmechanik', "\u00dcber die Quantenmechanik"),
                    (r"Fran{\c c}ois et le th{\'e}or{\`e}me", "Fran\u00e7ois et le th\u00e9or\u00e8me"),
                    (r"\v{S}koda's theorem", "\u0160koda's theorem")]:
    check(f"accent form {form[:14]!r} matches its plain text",
          H.title_match(form, plain) >= 0.90, f"got {H.title_match(form, plain):.2f}")
check("the fold does not make everything match",
      H.title_match("penguins on the moon", "Continuous Variable Quantum Cryptography") < 0.60)

# 22. Crossref records do occasionally carry an EMPTY title. chapoton_livernet_2001 is a
#     real IMRN 2001 paper with a correct DOI whose registry record has none. Comparing
#     against "" scores 0.00 and read as MISMATCH: the registry's gap became an accusation
#     against the author.
H._get = lambda u, accept="application/json": (
    _j2.dumps({"message": {"title": [""], "DOI": "10.1155/s1073792801000198"}}), 200)
cls, why = H.verify({"doi": "10.1155/s1073792801000198", "arxiv": "", "year": "",
                     "title": "Pre-Lie algebras and the rooted trees operad", "key": "c"})
check("an empty registry title is UNCHECKABLE, not MISMATCH", cls == "UNCHECKABLE", f"got {cls}: {why}")
H._get = _real_get


# --- round 5: audit 2026-09-28. The suite could not fail for most of what it guards ----
#
# A mutation pass planted 13 defects in hallucite.py, one at a time, and this suite passed
# every one of them: the MISMATCH threshold dropped to 0.05, check_arxiv returning OK for
# any id, the 429 retry deleted, arXiv extraction deleted, among others. The cases below
# exist so that each of those now goes red. tests/mutants.py re-plants them in CI.

# 23. THE POINT OF THE TOOL, and it had no offline test: a DOI that resolves to a different
#     paper. Every MISMATCH assertion lived in the live --selftest, which CI never runs.
cls, why = verify(NATURE, doi="10.1038/nature14539", title="Deep Learning")
check("a resolving DOI with the cited title is OK", cls == "OK", f"got {cls}: {why}")
cls, why = verify(NATURE, doi="10.1038/nature14539", title="Attention Is All You Need")
check("a resolving DOI with a different title is MISMATCH", cls == "MISMATCH", f"got {cls}: {why}")

# 24. Same, for a DOI outside Crossref (DataCite, Zenodo, arXiv DOIs), answered by doi.org.
ZEN = Registry(doi_org={"10.5281/zenodo.1": {"title": "A Survey of Bird Songs"}})
cls, why = verify(ZEN, doi="10.5281/zenodo.1", title="A Survey of Bird Songs")
check("a non-Crossref DOI with the cited title is OK", cls == "OK", f"got {cls}: {why}")
cls, why = verify(ZEN, doi="10.5281/zenodo.1", title="Attention Is All You Need")
check("a non-Crossref DOI with a different title is MISMATCH", cls == "MISMATCH", f"got {cls}: {why}")

# 25. arXiv had no offline test at all, so "every arXiv id is OK" passed the suite.
ARX = Registry(arxiv={"1706.03762": "Attention Is All You Need"})
cls, why = verify(ARX, arxiv="1706.03762", title="Attention is all you need")
check("an arXiv id with the cited title is OK", cls == "OK", f"got {cls}: {why}")
cls, why = verify(ARX, arxiv="1706.03762", title="Deep Residual Learning for Image Recognition")
check("an arXiv id with a different title is MISMATCH", cls == "MISMATCH", f"got {cls}: {why}")
cls, why = verify(ARX, arxiv="2101.99999", title="Attention Is All You Need")
check("an arXiv id with no record is FABRICATED", cls == "FABRICATED", f"got {cls}: {why}")

# 26. arXiv ids in \bibitem entries and in prose. Deleting either extractor passed.
r = H.parse_bibitem(r"\bibitem{v} A. Vaswani, ``Attention is all you need,'' arXiv:1706.03762, 2017.")
check("a \\bibitem arXiv id is extracted", r and r[0]["arxiv"] == "1706.03762", f"got {r}")
r = H.parse_inline("as shown in arXiv:1706.03762v5 and elsewhere")
check("an inline arXiv id is extracted", [x["arxiv"] for x in r] == ["1706.03762"], f"got {r}")

# 27. 429 and 503 mean "come back later". Deleting the retry passed the suite, and without it
#     a bibliography big enough to trip the rate limiter fails --gate for a reason that has
#     nothing to do with its references.
class _Resp:
    status = 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_script(*codes):
    """urlopen that raises HTTPError for each code in turn, then succeeds."""
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req.full_url)
        if len(seen) <= len(codes):
            raise urllib.error.HTTPError(req.full_url, codes[len(seen) - 1], "x",
                                         {"Retry-After": "0"}, None)
        return _Resp()
    return urlopen, seen


real_urlopen, real_sleep = H.urllib.request.urlopen, H.time.sleep
H.time.sleep = lambda s: None
try:
    H.urllib.request.urlopen, seen = _urlopen_script(429, 503)
    body, status = H._get("https://api.crossref.org/works/10.1/x")
    check("429 and 503 are retried", status == 200 and len(seen) == 3, f"{len(seen)} attempts")
    H.urllib.request.urlopen, seen = _urlopen_script(404)
    try:
        H._get("https://api.crossref.org/works/10.1/x")
        check("a 404 is not retried", False, "no exception raised")
    except urllib.error.HTTPError as e:
        check("a 404 is not retried", e.code == 404 and len(seen) == 1, f"{len(seen)} attempts")
finally:
    H.urllib.request.urlopen, H.time.sleep = real_urlopen, real_sleep

# 28. The parser fixtures built into hallucite.py only ran under the live --selftest.
check("the built-in parser fixtures pass offline", H.selftest_parsers())


# --- round 6: a title the parser had to guess is evidence of nothing ------------------
#
# The network layer refuses to accuse on missing evidence. The parsers did not: a wrong
# title guess went straight into the comparison, and a wrong title against a correct DOI
# is a MISMATCH -- a hard failure on a correct reference. Found by audit 2026-09-28.

# 29. The .bbl that BibTeX writes for natbib users. The venue sits in \emph{}, the title in
#     the first \newblock, and the parser took the \emph{}: every reference with a DOI came
#     back MISMATCH against "Nature" or "Proceedings of the IEEE Conference on ...".
PLAINNAT = r"""\begin{thebibliography}{3}
\bibitem[LeCun et~al.(2015)LeCun, Bengio, and Hinton]{lecun}
Yann LeCun, Yoshua Bengio, and Geoffrey Hinton.
\newblock Deep learning.
\newblock \emph{Nature}, 521\penalty0 (7553):\penalty0 436--444, 2015.
\newblock \doi{10.1038/nature14539}.

\bibitem[He et~al.(2016)He, Zhang, Ren, and Sun]{he}
Kaiming He, Xiangyu Zhang, Shaoqing Ren, and Jian Sun.
\newblock Deep residual learning for image recognition.
\newblock In \emph{Proceedings of the IEEE Conference on Computer Vision and Pattern
  Recognition}, pages 770--778, 2016.
\newblock \doi{10.1109/cvpr.2016.90}.

\bibitem[Bishop(2006)]{bishop}
Christopher~M. Bishop.
\newblock \emph{Pattern Recognition and Machine Learning}.
\newblock Springer, 2006.
\end{thebibliography}"""
refs = H.parse_bibitem(PLAINNAT)
check("plainnat .bbl: titles come from the first \\newblock, not the \\emph{} venue",
      [r["title"] for r in refs] == ["Deep learning", "Deep residual learning for image recognition",
                                     "Pattern Recognition and Machine Learning"],
      f"got {[r['title'] for r in refs]}")
BBL_REG = Registry(crossref={"10.1038/nature14539": "Deep learning",
                             "10.1109/cvpr.2016.90": "Deep Residual Learning for Image Recognition",
                             "10.1103/physrevlett.88.057902":
                             "Continuous Variable Quantum Cryptography Using Coherent States"})
got = [verify(BBL_REG, **{k: r[k] for k in ("doi", "title", "title_guess")})[0] for r in refs[:2]]
check("...and a correct plainnat .bbl is OK", got == ["OK", "OK"], f"got {got}")
swapped = dict(refs[0], doi="10.1103/physrevlett.88.057902")
cls, _ = verify(BBL_REG, **{k: swapped[k] for k in ("doi", "title", "title_guess")})
check("...while a swapped DOI in it is still MISMATCH", cls == "MISMATCH", f"got {cls}")

# 30. ACM and REVTeX .bbl files tag the title explicitly. REVTeX puts the JOURNAL in
#     \emph{\bibinfo{journal}{...}}, which must not be guessed as a title either.
r = H.parse_bibitem(r"\bibitem{a} \bibfield{author}{\bibinfo{person}{Y. LeCun}}."
                    r" \newblock \bibinfo{title}{Deep learning}. \newblock"
                    r" \bibinfo{journal}{\emph{Nature}} (2015).")
check("\\bibinfo{title} is read", r and r[0]["title"] == "Deep learning", f"got {r and r[0]['title']!r}")
r = H.parse_bibitem(r"\bibitem{a} \bibfield{author}{A. B.}, \emph{\bibinfo{journal}{Phys. Rev. Lett.}}"
                    r" \textbf{\bibinfo{volume}{88}}, 057902 (2002)")
check("REVTeX's \\emph{\\bibinfo{journal}} is not guessed as a title", r and r[0]["title"] == "",
      f"got {r and r[0]['title']!r}")

# 31. \" is an umlaut, not a quotation mark. `Schr\"odinger and G\"odel` parsed as the title
#     `odinger and K.~G\`, which read MISMATCH against the paper's DOI.
r = H.parse_bibitem(r"\bibitem{s} E.~Schr\"odinger and K.~G\"odel, Die gegenw\"artige Situation,"
                    r" Naturwiss. 23, 807 (1935), doi:10.1007/bf01491891.")
check("umlaut escapes are not read as quotes", r and "odinger" not in r[0]["title"],
      f"got {r and r[0]['title']!r}")
r = H.parse_bibitem(r'\bibitem{s} E.~Schr\"odinger, "Die gegenw\"artige Situation," Naturwiss. (1935).')
check("...and a quoted title may contain one", r and r[0]["title"] == r'Die gegenw\"artige Situation',
      f"got {r and r[0]['title']!r}")

# 32. \emph{} holds the title in some styles and the journal in others, and nothing in the
#     entry says which. A guess may raise SUSPECT; it must never raise MISMATCH.
r = H.parse_bibitem(r"\bibitem{x} A. Author, \emph{Physical Review Letters} \textbf{88}, 057902 (2002),"
                    r" doi:10.1103/physrevlett.88.057902.")
check("an \\emph{} title is marked as a guess", r and r[0]["title_guess"], f"got {r}")
PRL = Registry(crossref={"10.1103/physrevlett.88.057902":
                         "Continuous Variable Quantum Cryptography Using Coherent States"})
cls, why = verify(PRL, doi=r[0]["doi"], title=r[0]["title"], title_guess=True)
check("a guessed title that disagrees is SUSPECT, not MISMATCH", cls == "SUSPECT", f"got {cls}: {why}")
rc, out = run_main({"refs.tex": r"\begin{thebibliography}{1}" + "\n" + r"\bibitem{x} A. Author,"
                    r" \emph{Physical Review Letters} \textbf{88}, 057902 (2002),"
                    r" doi:10.1103/physrevlett.88.057902." + "\n" + r"\end{thebibliography}"},
                   "--gate", reg=PRL)
check("...so it does not fail the gate", rc == 0 and "[SUSP]" in out, f"got exit {rc}: {out}")

# 33. BibTeX requires `{\"U}` in a quoted field precisely because a bare `\"` would end it.
#     The field parser stopped at that `"` anyway: the title parsed as `{\`, scored 0.00,
#     and a correct reference to Goedel's 1931 paper read MISMATCH.
GODEL = "Über formal unentscheidbare Sätze der Principia Mathematica und verwandter Systeme I"
r = H.parse_bib('@article{g, title = "{\\"U}ber formal unentscheidbare S{\\"a}tze der Principia '
                'Mathematica und verwandter Systeme I", doi = {10.1007/BF01700692}}')
check("a quoted field keeps going past a braced \\\"", r and r[0]["title"].endswith("Systeme I"),
      f"got {r and r[0]['title']!r}")
cls, why = verify(Registry(crossref={"10.1007/bf01700692": GODEL}), doi=r[0]["doi"], title=r[0]["title"])
check("...and the reference is OK", cls == "OK", f"got {cls}: {why}")

# 34. @string macros were never expanded, so `title = t1` was compared as the title "t1".
r = H.parse_bib("@string{dl = {Deep learning}}\n@string{sub = dl # \": A Review\"}\n"
                "@article{a, title = dl, doi = {10.1/a}}\n@article{b, title = sub}\n"
                "@article{c, title = undefined_macro, doi = {10.1/c}}\n")
check("@string macros are expanded, including # concatenation",
      [x["title"] for x in r] == ["Deep learning", "Deep learning: A Review", ""],
      f"got {[x['title'] for x in r]}")

# 35. @article(key, ...) is legal BibTeX and was silently dropped from a mixed file. An `@`
#     inside a field value must not open a phantom entry either.
r = H.parse_bib('@article{a, title={One}}\n@article(b, title = "Two (and a half)", doi={10.9999/x})\n'
                "@misc{c, title={Three}, note={mail me@home{} please}}\n")
check("parenthesis-delimited entries are parsed",
      [(x["key"], x["title"]) for x in r] == [("a", "One"), ("b", "Two (and a half)"), ("c", "Three")],
      f"got {[(x['key'], x['title']) for x in r]}")

# 36. A title with no comparable characters is no title. It scored 0.00 and read MISMATCH.
cls, why = verify(NATURE, doi="10.1038/nature14539", title="{\\")
check("a title that normalises to nothing is not compared", cls == "OK", f"got {cls}: {why}")


# --- round 7: the arXiv path, where nothing had been ported ----------------------------
#
# Every guard added to the Crossref path -- a 200 that is not data is an outage, an id is
# only an id where it is cited as one, a dead identifier is not a dead paper -- was missing
# from the arXiv path. Found by audit 2026-09-28.

# 37. An arXiv id is taken only where the text CITES one. The bibtex parser searched the whole
#     entry for anything id-shaped whenever "arxiv" appeared in it: an IEEE Xplore URL gave
#     document/8765432, a Zenodo URL record/1234567, and a real paper read FABRICATED.
for label, entry in [
        ("an IEEE Xplore URL", "url={https://ieeexplore.ieee.org/document/8765432}, note={on arXiv}"),
        ("a Zenodo URL", "url={https://zenodo.org/record/1234567}, note={also on arXiv}"),
        ("the digits of a DOI", "doi={10.1145/3292500.3330701}, note={arXiv version}"),
        ("a non-arXiv eprint", "eprinttype={hdl}, eprint={1234.56789}, note={arXiv}")]:
    r = H.parse_bib("@misc{a, title={X}, %s}" % entry)
    check(f"{label} is not an arXiv id", r and r[0]["arxiv"] == "", f"got {r and r[0]['arxiv']!r}")
for label, entry, want in [
        ("eprint with prefix and version", "eprint={arXiv:1706.03762v5}", "1706.03762"),
        ("archiveprefix + eprint", "archiveprefix={arXiv}, eprint={hep-th/9901001}", "hep-th/9901001"),
        ("Google Scholar's journal field", "journal={arXiv preprint arXiv:1706.03762}", "1706.03762"),
        ("an arxiv.org URL", "url={https://arxiv.org/abs/1706.03762}", "1706.03762"),
        ("an arXiv DOI", "doi={10.48550/arXiv.1706.03762}", "1706.03762")]:
    r = H.parse_bib("@misc{a, title={X}, %s}" % entry)
    check(f"{label} gives the arXiv id", r and r[0]["arxiv"] == want, f"got {r and r[0]['arxiv']!r}")
# Without digit boundaries a typo is truncated into a DIFFERENT, real id: arXiv:1706.037621
# became 1706.03762, and the reference was then judged against somebody else's paper.
for typo in ["arXiv:1706.037621", "arXiv:12345.6789"]:
    got = H.parse_inline(typo)
    check(f"a malformed id is not truncated into a real one ({typo})", got == [], f"got {got}")
got = [x["arxiv"] for x in H.parse_inline("preprints: https://arxiv.org/abs/1706.03762v2 and "
                                          "https://arxiv.org/pdf/1810.04805.pdf")]
check("arxiv.org URLs in prose are references", got == ["1706.03762", "1810.04805"], f"got {got}")

# 38. A 200 that is not an Atom feed is an outage. It carried no <entry>, which read as "no
#     record", and a real paper came back FABRICATED while arXiv was showing a rate-limit page.
cls, why = verify(Registry(arxiv_raw="<html><body>Rate exceeded.</body></html>"),
                  arxiv="1706.03762", title="Attention Is All You Need")
check("an arXiv 200 that is not a feed is UNCHECKABLE, not FABRICATED",
      cls == "UNCHECKABLE" and H.NO_ORACLE in why, f"got {cls}: {why}")

# 39. arXiv reports a malformed id as an entry titled "Error". Read as a record, an inline id
#     with no title to compare -- the only kind prose has -- came back OK.
ERR = ("<feed><entry><id>http://arxiv.org/api/errors#incorrect_id_format_for_2413.99999</id>"
       "<title>Error</title><summary>incorrect id format for 2413.99999</summary></entry></feed>")
cls, why = verify(Registry(arxiv_raw=ERR), arxiv="2413.99999")
check("arXiv's error entry is not a record", cls == "FABRICATED", f"got {cls}: {why}")

# 40. An empty feed is asked again once before it becomes an accusation, and every arXiv
#     request goes over https (the old plain-http query let anyone on the path rewrite it).
reg = Registry()
verify(reg, arxiv="2101.99999")
arx_calls = [u for u in reg.calls if "arxiv" in u]
check("an empty arXiv answer is re-asked once", len(arx_calls) == 2, f"got {len(arx_calls)}")
check("arXiv is queried over https", arx_calls and all(u.startswith("https://") for u in arx_calls),
      f"got {arx_calls}")

# 41. arXiv asks for one request every three seconds; everything was paced at 0.25 s.
naps, real_sleep, real_mono = [], H.time.sleep, H.time.monotonic
H.time.sleep, H.time.monotonic = naps.append, (lambda: 1000.0)
H._arxiv_last[0] = 999.0
real_get, H._get = H._get, (lambda url, accept=None: ("<feed></feed>", 200))
try:
    H._arxiv_get("id_list=1706.03762")
finally:
    H._get, H.time.sleep, H.time.monotonic = real_get, real_sleep, real_mono
check("consecutive arXiv requests are three seconds apart", naps == [2.0], f"slept {naps}")

# 42. The subject class of an old-style id is not part of it: math.AG/0512013 is math/0512013.
cls, why = verify(Registry(arxiv={"math/0512013": "Some Algebraic Geometry"}),
                  arxiv="math.AG/0512013", title="Some Algebraic Geometry")
check("an old-style id with a subject class resolves", cls == "OK", f"got {cls}: {why}")

# 43. A dead DOI next to a live arXiv id. The arXiv id was never asked, and a real preprint
#     cited with a mistyped DOI came back FABRICATED with its own arXiv id in the entry.
BERT = "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
ref = dict(doi="10.5555/1234567.7654321", arxiv="1810.04805", title=BERT)
cls, why = verify(Registry(arxiv={"1810.04805": BERT}), **ref)
check("a dead DOI with a live arXiv id is BAD-DOI", cls == "BAD-DOI", f"got {cls}: {why}")
cls, why = verify(Registry(arxiv={}), **ref)
check("...a dead DOI with a dead arXiv id is FABRICATED", cls == "FABRICATED", f"got {cls}: {why}")
cls, why = verify(Registry(arxiv_raw="<html>down</html>"), **ref)
check("...and with arXiv unreachable it is half a check", cls == "UNCHECKABLE" and H.NO_ORACLE in why,
      f"got {cls}: {why}")

# 44. The rescue asked only Crossref, which holds few of the conference papers that carry a
#     dead ACM 10.5555 DOI. arXiv holds most of them. A mistyped arXiv id is rescued the
#     same way a dead DOI is; it used to be FABRICATED with no title check at all.
cls, why = verify(Registry(arxiv_search=[BERT]), doi="10.5555/1234567.7654321", title=BERT)
check("a dead DOI whose title is on arXiv is BAD-DOI", cls == "BAD-DOI" and "arXiv match" in why,
      f"got {cls}: {why}")
cls, why = verify(Registry(arxiv_search=[BERT]), arxiv="1810.99999", title=BERT)
check("a dead arXiv id whose title is real is BAD-DOI", cls == "BAD-DOI", f"got {cls}: {why}")
cls, why = verify(Registry(arxiv_raw="<html>down</html>"), doi="10.5555/1234567.7654321", title=BERT)
check("a rescue with one source down and no match is half a check",
      cls == "UNCHECKABLE" and H.NO_ORACLE in why, f"got {cls}: {why}")

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 1)
