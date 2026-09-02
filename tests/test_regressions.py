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

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 1)
