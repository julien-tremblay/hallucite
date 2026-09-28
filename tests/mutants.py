#!/usr/bin/env python3
"""Plant known defects in hallucite.py, one at a time, and require the offline suite to fail.

A test that cannot fail is worse than none, because it is counted. On 2026-09-28 a mutation
pass planted 13 defects -- the MISMATCH threshold dropped to 0.05, check_arxiv returning OK
for every id, the 429 retry deleted -- and tests/test_regressions.py passed all 13. Three of
its checks could not fail for the reason they named. This script keeps that from recurring:
each entry below is a real defect, and it must turn the suite red.

A mutant whose target text no longer exists is an error, not a pass. When a refactor moves
the code, update the mutant so it still plants the same defect.

    python3 tests/mutants.py        # ~20s, offline
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (what the defect is, text to find in hallucite.py, what to replace it with)
MUTANTS = [
    ("unknown flags silently ignored",
     "    if bad:\n", "    if False:\n"),
    ("a parser failure counted soft",
     "                hard += 1\n            elif has_markers:",
     "                soft += 1\n            elif has_markers:"),
    ("inputs judged alone, not pooled",
     "if has_markers and not any_refs:", "if has_markers:"),
    ("BAD-DOI counted hard",
     "            icon = {", "            if cls == \"BAD-DOI\":\n                hard += 1\n            icon = {"),
    ("--strict ignored by the gate",
     "(strict and soft)", "(False and soft)"),
    ("gate passes when no registry answered",
     "1 if hard or degraded or", "1 if hard or"),
    ("Crossref MISMATCH bar lowered to 0.05",
     "(\"OK\", f\"DOI resolves, title match {r:.2f}\")\n        if r >= 0.6",
     "(\"OK\", f\"DOI resolves, title match {r:.2f}\")\n        if r >= 0.05"),
    ("non-Crossref MISMATCH never reported",
     "(\"OK\", f\"DOI resolves (non-Crossref agency), title match {r:.2f}\")\n        if r >= 0.6",
     "(\"OK\", f\"DOI resolves (non-Crossref agency), title match {r:.2f}\")\n        if True"),
    ("BAD-DOI rescue bar lowered to 0.60",
     "    if best is not None and best >= IDENTITY:\n        return best, found, \"Crossref\"",
     "    if best is not None and best >= 0.60:\n        return best, found, \"Crossref\""),
    ("title-only bar lowered to 0.60",
     "        if best >= IDENTITY\n", "        if best >= 0.60\n"),
    ("check_arxiv: every id is OK",
     "        return _unresolvable(f\"arXiv:{aid}: {reason}\", f\"arXiv:{aid}\", claimed_title)",
     "        return \"OK\", \"mutant\""),
    ("check_arxiv: MISMATCH never reported",
     "(\"OK\", f\"arXiv resolves, title match {r:.2f}\")\n        if r >= 0.6",
     "(\"OK\", f\"arXiv resolves, title match {r:.2f}\")\n        if True"),
    ("429/503 not retried",
     "if e.code not in (429, 503) or attempt == 2:", "if True:"),
    ("\\bibitem arXiv ids not extracted",
     "        ma = ARXIV_CITED.search(body)", "        ma = None"),
    ("inline arXiv ids not extracted",
     "    for a in sorted(cited):", "    for a in []:"),
    ("trailing period kept on a DOI",
     "if d[-1] in \".,;:'\\\"\":", "if d[-1] in \",;:'\\\"\":"),
    # --- step 2 of the 2026-09-28 audit: a guessed title never accuses
    ("\\\" read as a quotation mark",
     "m = re.search(r'(?<!\\\\)\"(.{2,300}?)(?<!\\\\)\"', body, re.S)",
     "m = re.search(r'\"(.{2,300}?)\"', body, re.S)"),
    (".bbl \\newblock title ignored",
     "    if r\"\\newblock\" in body:\n        seg", "    if False:\n        seg"),
    ("\\bibinfo{title} ignored",
     "    bi = re.search(r\"\\\\bibinfo\\s*\\{title\\}\\s*\\{\", body)", "    bi = None"),
    ("a guessed title can MISMATCH",
     "if cls == \"MISMATCH\" and ref.get(\"title_guess\"):", "if False:"),
    ("a quoted value ends at a braced quote",
     "elif c == '\"' and close == '\"' and depth == 0:", "elif c == '\"' and close == '\"':"),
    ("an undefined macro becomes the title",
     "            elif raw_words:\n                parts.append(word)\n            else:\n                return None",
     "            else:\n                parts.append(word)"),
    ("@string macros not recorded",
     "                    macros[sm.group(1).lower()] = val", "                    pass"),
    ("a title with no comparable characters is compared",
     "title = ref[\"title\"] if norm(ref[\"title\"]) else \"\"", "title = ref[\"title\"]"),
    ("parenthesis-delimited entries skipped",
     "r\"@(\\w+)\\s*([{(])\"", "r\"@(\\w+)\\s*([{])\""),
    # --- step 3: the arXiv path fails closed
    ("an arXiv 200 that is not a feed reads as 'no record'",
     '    if "<feed" not in body:', "    if False:"),
    ("arXiv's error entry read as a record",
     'records = [e for e in entries if "/api/errors" not in e and _entry_title(e)]',
     "records = [e for e in entries if _entry_title(e)]"),
    ("arXiv queried over plain http",
     '_ARXIV_API = "https://export', '_ARXIV_API = "http://export'),
    ("arXiv requests not spaced",
     "    if wait > 0:\n        time.sleep(wait)", "    if False:\n        time.sleep(wait)"),
    ("anything id-shaped in an entry is an arXiv id",
     "            am = ARXIV_CITED.search(body)",
     '            am = re.search(ARXIV_RE, body) if "arxiv" in body.lower() else None'),
    ("a malformed arXiv id truncated into a real one",
     r'ARXIV_RE = r"(\d{4}\.\d{4,5}(?!\d)|', r'ARXIV_RE = r"(\d{4}\.\d{4,5}|'),
    ("a dead DOI never asks the entry's arXiv id",
     'if cls == "FABRICATED" and ref["arxiv"]:', "if False:"),
    ("the rescue never asks arXiv",
     "    abest, afound = _arxiv_title_lookup(title)", '    abest, afound = 0.0, ""'),
    ("an empty arXiv feed is final at once",
     "    for _attempt in range(2):", "    for _attempt in range(1):"),
    ("old-style subject class sent to arXiv",
     r'    qid = re.sub(r"^([a-z-]+)\.[A-Za-z]{2}/", r"\1/", aid)', "    qid = aid"),
    ("half a rescue is treated as a verdict",
     "    if abest is None:\n        return None, afound, None", "    if False:\n        return None, afound, None"),
    # --- step 4: DOI existence comes from the Handle API
    ("a landing-page failure read as an unregistered DOI",
     '        return "UNCHECKABLE", NO_TITLE + (f"{doi} is registered outside Crossref, but its "',
     '        return _unresolvable("dead", doi, claimed_title)\n        return "UNCHECKABLE", NO_TITLE + (f"{doi} is registered outside Crossref, but its "'),
    ("a metadata gap fails the gate forever",
     'NO_TITLE + (f"{doi} is registered outside Crossref', 'NO_ORACLE + (f"{doi} is registered outside Crossref'),
    ("the Handle API's responseCode 100 ignored",
     "    if code == 100:  # the Handle System", "    if False:  # the Handle System"),
    ("an empty non-Crossref title is compared",
     "    if not found.strip():\n        # The empty-title guard below", "    if False:\n        # The empty-title guard below"),
    ("a DOI with a '..' segment is sent",
     "    if _DOT_SEGMENT.search(doi):", "    if False:"),
    # --- step 5: DOIs are read the way people write them
    ("LaTeX escapes kept inside DOIs",
     '    text = _TEX_ESCAPE.sub(lambda m: m.group(1) or "_", text or "")', '    text = text or ""'),
    ("the doi field taken verbatim",
     '        dois = find_dois(field("doi", raw_words=True)) or find_dois(body)',
     '        dois = [clean_doi(field("doi", raw_words=True)).lower()] or find_dois(body)'),
    # --- step 6: citation markers, dispatch, identifier-only passes
    ("natbib and biblatex cite commands not recognised",
     r'    r"\\[A-Za-z]*cite[A-Za-z]*\*?\s*(?:\[[^\]]*\]\s*){0,2}\{"', r'    r"\\cite\{"'),
    ("one parser per file",
     '    refs += [r for r in parse_inline(text) if (r["doi"] or r["arxiv"]) not in seen]',
     "    pass"),
    ("BibTeX entry types matched case-sensitively",
     '_BIB_ENTRY = re.compile(r"@(?:" + "|".join(_ENTRY_TYPES) + r")\\s*[{(]", re.I)',
     '_BIB_ENTRY = re.compile(r"@(?:" + "|".join(_ENTRY_TYPES) + r")\\s*[{(]")'),
    ("an identifier-only pass not labelled",
     '                why += "  [identifier only: no cited title to compare]"', "                pass"),
    # --- step 7: title scoring
    ("difflib autojunk back on",
     '" ".join(ta), " ".join(tb), autojunk=False)', '" ".join(ta), " ".join(tb))'),
    ("registry markup compared as text",
     '    s = _untex(html.unescape(_MARKUP.sub("", s or "")))', '    s = _untex(s or "")'),
    ("TeX math left unfolded",
     '    s = re.sub(r"\\$([^$]{0,200})\\$", _math, s)', "    pass"),
    ("TeX letter commands left in",
     "    s = _TEX_LETTER.sub(lambda m: _TEX_LETTERS[m.group(1)], s)", "    pass"),
    ("braces split words",
     '    return s.replace("{", "").replace("}", "")', '    return s.replace("{", " ").replace("}", " ")'),
    ("subtitles ignored",
     '    out = titles + ([f"{titles[0]}: {subs[0]}"] if titles and subs else [])', "    out = list(titles)"),
    ("original-language titles ignored",
     '    out += strs(rec.get("original-title"))', "    pass"),
    ("CONTROL: a no-op edit (must survive)",
     "        return \"UNCHECKABLE\", degraded + \" -- refusing", "        pass\n        return \"UNCHECKABLE\", degraded + \" -- refusing"),
]
# The CONTROL is deliberately harmless (`pass` before the same return). It proves the
# harness can report a survivor at all; a harness that reports "caught" for everything
# would otherwise look exactly like a good suite.
EXPECTED_SURVIVORS = {"CONTROL: a no-op edit (must survive)"}


def run_suite(src):
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "tests").mkdir()
        shutil.copy(ROOT / "tests" / "test_regressions.py", pathlib.Path(d) / "tests")
        (pathlib.Path(d) / "hallucite.py").write_text(src)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, str(pathlib.Path(d) / "tests" / "test_regressions.py")],
                              capture_output=True, text=True, env=env).returncode


def main():
    orig = (ROOT / "hallucite.py").read_text()
    if run_suite(orig) != 0:
        print("baseline: the suite fails on unmodified hallucite.py; fix that first")
        return 1
    bad = []
    for name, find, repl in MUTANTS:
        if orig.count(find) != 1:
            print(f"  [STALE ] {name}: target text found {orig.count(find)} times, expected 1")
            bad.append(name)
            continue
        caught = run_suite(orig.replace(find, repl)) != 0
        expected = name not in EXPECTED_SURVIVORS
        ok = caught == expected
        label = "caught" if caught else "SURVIVED"
        print(f"  [{label:8}] {name}" + ("" if ok else "   <-- unexpected"))
        if not ok:
            bad.append(name)
    print(f"\n{'ALL MUTANTS BEHAVE' if not bad else str(len(bad)) + ' PROBLEM(S): ' + ', '.join(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
