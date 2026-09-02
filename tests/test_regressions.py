#!/usr/bin/env python3
"""Offline regression tests. No network, so they run in CI and in a hook.

Every case here is a defect that actually shipped. The live behaviour is covered by
`hallucite.py --selftest`, which does hit Crossref and arXiv.
"""
import pathlib
import re
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
src = (pathlib.Path(__file__).resolve().parent.parent / "hallucite.py").read_text()
fr = re.findall(r'(?i)(irresoluble|toutes les agences|introuvable|aucune reference)', src)
check("no French in user-visible strings", not fr, f"found {fr}")

# 5. A file whose citations live in a sibling file is the normal LaTeX layout. Judging
#    each input alone made paper.tex a hard PARSER FAILURE next to its own refs.bib.
check("multi-input pooling is implemented", "any_refs" in src)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 1)
