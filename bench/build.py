#!/usr/bin/env python3
"""Build three bibliographies from the manifest.

raw        registry title verbatim. Any hard finding here is a pure tool defect.
perturbed  the same works, deformed the way real bibliographies actually differ from the
           registry: LaTeX-escaped accents, dropped subtitles, case changes, a trailing
           period on the DOI, an indented closing brace. Still all real works, so still
           zero hard findings expected. This is the measurement that matters, because the
           raw round-trip is nearly a tautology (exact strings compared to themselves).
control    deliberately unresolvable DOIs, so the run can FAIL. Without it a verifier that
           returned OK unconditionally would score a perfect false-positive rate.
             control-real  broken DOI + the real title  -> must be BAD-DOI
             control-fake  broken DOI + a nonsense title -> must be FABRICATED
"""
import json, random, sys, unicodedata

ACC = {"é": r"{\'e}", "è": r"{\`e}", "ê": r"{\^e}", "à": r"{\`a}",
       "ç": r"{\c c}", "ü": r'{\"u}', "ö": r'{\"o}', "ä": r'{\"a}',
       "É": r"{\'E}", "Ú": r"{\'U}", "ñ": r"{\~n}", "â": r"{\^a}"}

def texify(t):
    return "".join(ACC.get(c, c) for c in t)

def emit(f, key, title, doi, year, indent_close=False, dot=False):
    f.write("@article{%s,\n  title = {%s},\n  doi = {%s%s},\n  year = {%s}\n%s\n" %
            (key, title.replace("{", "").replace("}", "") if "\\" not in title else title,
             doi, "." if dot else "", year or "", "  }" if indent_close else "}"))

man = json.load(open(sys.argv[1]))
rng = random.Random(20260902)          # seeded: the deformations are reproducible
d = sys.argv[2]

with open(f"{d}/raw.bib", "w") as f:
    for i, r in enumerate(man):
        emit(f, f"k{i}", r["title"], r["doi"], r["year"])

with open(f"{d}/perturbed.bib", "w") as f:
    for i, r in enumerate(man):
        t = r["title"]
        if ":" in t and rng.random() < 0.45:
            head = t.split(":")[0].strip()
            # Only if what remains is still a title. "Review: The Gender Impact of X" would
            # otherwise be cited as "Review", which no real bibliography contains, and the
            # MISMATCH that follows would be correct rather than a false positive. A harness
            # that manufactures its own failures measures nothing.
            if len(head) >= 25 and len(head.split()) >= 4:
                t = head
        if rng.random() < 0.5:
            t = texify(t)                        # LaTeX-escaped accents
        if rng.random() < 0.3:
            t = t.upper() if rng.random() < 0.5 else t.lower()
        emit(f, f"k{i}", t, r["doi"], r["year"],
             indent_close=rng.random() < 0.3, dot=rng.random() < 0.2)

with open(f"{d}/control.bib", "w") as f:
    for i, r in enumerate(man[:40]):
        emit(f, f"real{i}", r["title"], r["doi"] + "zz9q", r["year"])
    for i in range(40):
        emit(f, f"fake{i}",
             f"Quantum {rng.choice(['Zither','Wombat','Pretzel','Marmalade'])} "
             f"{rng.choice(['Refactoring','Onomastics','Bathymetry'])} in "
             f"{rng.choice(['Triassic','Cislunar','Subarctic'])} Systems",
             f"10.{rng.randint(1000,9999)}/nonexistent.{rng.randint(10**6,10**7)}", 2021)
print("built raw.bib, perturbed.bib, control.bib")
