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
     "(\"OK\", f\"DOI resolves (non-Crossref agency), title match {r:.2f}\")\n            if r >= 0.6",
     "(\"OK\", f\"DOI resolves (non-Crossref agency), title match {r:.2f}\")\n            if True"),
    ("BAD-DOI rescue bar lowered to 0.60",
     "    if best >= IDENTITY:\n        return \"BAD-DOI\"", "    if best >= 0.60:\n        return \"BAD-DOI\""),
    ("title-only bar lowered to 0.60",
     "        if best >= IDENTITY\n", "        if best >= 0.60\n"),
    ("check_arxiv: every id is OK",
     "    if not entries:\n        return \"FABRICATED\"", "    if True:\n        return \"OK\""),
    ("check_arxiv: MISMATCH never reported",
     "(\"OK\", f\"arXiv resolves, title match {r:.2f}\")\n        if r >= 0.6",
     "(\"OK\", f\"arXiv resolves, title match {r:.2f}\")\n        if True"),
    ("429/503 not retried",
     "if e.code not in (429, 503) or attempt == 2:", "if True:"),
    ("\\bibitem arXiv ids not extracted",
     "        ma = re.search(r\"arXiv:\\s*(\" + ARXIV_RE + \")\", body, re.I)", "        ma = None"),
    ("inline arXiv ids not extracted",
     "for a in sorted(set(re.findall(r\"arXiv:\\s*\" + ARXIV_RE, text, re.I))):", "for a in []:"),
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
