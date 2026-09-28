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

Added 2026-09-28, because an audit found false accusations in every class the three files
above cannot produce. Their titles are the registry's own, so registry markup was only ever
compared with itself, a subtitle Crossref stores apart was never cited, and the dead-DOI
rescue was only tried on verbatim titles. raw, perturbed and control keep their exact recipe
and random sequence, so the published measurement can still be rebuilt from its manifest.

deformed   the title as a bibliography writes it: registry markup turned into TeX
           (CO<sub>2</sub> -> CO$_2$, <i>x</i> -> \emph{x}), Crossref's separate subtitle cited
           as part of the title, and the letters TeX spells as commands ({\o}, {\ss}, \'{\i}).
           Real works, correct DOIs: zero hard findings expected.
rescue     the deformed title with a broken DOI. Must be BAD-DOI; FABRICATED is a false
           accusation. This is the path where the 0.90 bar, not the 0.60 one, decides.
raw.bbl    the raw works as the .bbl BibTeX writes for natbib users (plainnat): title in the
           first \newblock, venue in \emph{}. Zero hard findings expected.
"""
import re
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
# ---- arms added 2026-09-28 (their own seed: the three files above must not change) ------
rng2 = random.Random(20260928)
LETTERS = {"ø": r"{\o}", "Ø": r"{\O}", "ß": r"{\ss}", "æ": r"{\ae}", "å": r"{\aa}",
           "ł": r"{\l}", "Ł": r"{\L}", "í": r"\'{\i}", "ó": r"{\'o}", "á": r"{\'a}",
           "ú": r"{\'u}", "ã": r"{\~a}", "õ": r"{\~o}", **ACC}


def tex_markup(t):
    """Registry markup the way a bibliography writes it."""
    t = re.sub(r"<(?:[a-z]+:)?sub>(.*?)</(?:[a-z]+:)?sub>", r"$_{\1}$", t, flags=re.S)
    t = re.sub(r"<(?:[a-z]+:)?sup>(.*?)</(?:[a-z]+:)?sup>", r"$^{\1}$", t, flags=re.S)
    t = re.sub(r"<(?:[a-z]+:)?(?:i|italic)>(.*?)</(?:[a-z]+:)?(?:i|italic)>", r"\\emph{\1}", t, flags=re.S)
    return re.sub(r"<[^>]+>", "", t).replace("&amp;", r"\&").replace("&lt;", "<").replace("&gt;", ">")


def deform(r):
    t = tex_markup(r["title"])
    if r.get("subtitle") and rng2.random() < 0.7:
        t = f"{t}: {tex_markup(r['subtitle'])}"
    return "".join(LETTERS.get(c, c) for c in t)


with open(f"{d}/deformed.bib", "w") as f, open(f"{d}/rescue.bib", "w") as g:
    for i, r in enumerate(man):
        t = deform(r)
        f.write("@article{k%d,\n  title = {%s},\n  doi = {%s},\n  year = {%s}\n}\n" % (i, t, r["doi"], r["year"] or ""))
        g.write("@article{dead%d,\n  title = {%s},\n  doi = {%s},\n  year = {%s}\n}\n"
                % (i, t, r["doi"] + "zz9q", r["year"] or ""))

with open(f"{d}/raw.bbl", "w") as f:
    f.write("\\begin{thebibliography}{%d}\n" % len(man))
    for i, r in enumerate(man):
        venue = tex_markup(r.get("venue") or "") or "Unpublished"
        f.write("\n\\bibitem[Anonymous(%s)]{k%d}\nA.~Author.\n\\newblock %s.\n\\newblock \\emph{%s}, %s.\n"
                "\\newblock \\doi{%s}.\n" % (r["year"] or "", i, tex_markup(r["title"]), venue,
                                           r["year"] or "", r["doi"]))
    f.write("\\end{thebibliography}\n")
print("built raw.bib, perturbed.bib, control.bib, deformed.bib, rescue.bib, raw.bbl")
