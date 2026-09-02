#!/usr/bin/env python3
"""Offline regression tests. No network, so they run in CI and in a hook.

Every case here is a defect that actually shipped. The live behaviour is covered by
`hallucite.py --selftest`, which does hit Crossref and arXiv.
"""
import ast
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import hallucite as H  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


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
check("multi-input pooling is implemented", "any_refs" in src)


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
HERE = pathlib.Path(__file__).resolve().parent
bib = HERE / "_gate_tmp.bib"
bib.write_text("@article{a,\n  title = {T},\n  doi = {10.9999/nope}\n}\n")
off = dict(**__import__("os").environ, http_proxy="http://127.0.0.1:9",
           https_proxy="http://127.0.0.1:9")
rc = subprocess.run([sys.executable, str(HERE.parent / "hallucite.py"), str(bib), "--gate"],
                    capture_output=True, env=off).returncode
check("--gate fails closed when no registry answers", rc == 1, f"got exit {rc}")
bib.unlink()

# 12. A misspelled flag was dropped silently, so --gates ran advisory and exited 0 while the
#     caller believed they were gating. And an unreadable path raised, exiting 1 -- which
#     under --gate is indistinguishable from "this bibliography contains fabrications".
HAL = str(pathlib.Path(__file__).resolve().parent.parent / "hallucite.py")
check("an unknown flag is rejected, not ignored",
      subprocess.run([sys.executable, HAL, "x.bib", "--gates"], capture_output=True).returncode == 2)
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

# 19. BAD-DOI is a defect worth fixing, not an accusation: it must not count as hard.
src2 = (pathlib.Path(__file__).resolve().parent.parent / "hallucite.py").read_text()
check("BAD-DOI is counted soft, never hard",
      'elif cls in ("SUSPECT", "UNCHECKABLE", "BAD-DOI")' in src2
      and '"BAD-DOI"' not in src2.split("if cls in (")[1].split(")")[0])

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 1)
