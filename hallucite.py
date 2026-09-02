#!/usr/bin/env python3
"""hallucite: find fabricated and mismatched citations. Deterministic, no LLM, no API key.

Motivation (2026-07-11 research digest): the HALLMARK study found a deterministic bibtex/DOI
verifier (F1 0.908) BEATS the best zero-shot LLM (0.840) at catching fabricated citations, and
giving the LLM tool access made it WORSE. Internal history: a draft paper shipped fabricated refs.
So this is a $0, no-LLM, network-only check that resolves every reference against the authoritative
registries (Crossref for DOIs/titles, arXiv for eprints) and classifies it.

Classes (most→least dangerous):
  FABRICATED  a DOI that 404s, or an arXiv id with no record        -> hard fail (exit 1)
  MISMATCH    DOI/arXiv resolves, but to a DIFFERENT title          -> hard fail (exit 1)
  SUSPECT     title-only ref with no close Crossref match           -> warn (may be a book/thesis/non-indexed venue)
  OK          resolved and title matches                            -> pass
  UNCHECKABLE no DOI / arXiv / usable title                         -> warn (can't verify, not necessarily wrong)

Usage:  hallucite <file.bib | file.tex | file.md> [...]   (--strict makes SUSPECT fail too)
Grounding lives in the REGISTRY, never the language. Advisory by default; wire into a commit hook with --gate.
"""

import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Crossref runs a "polite pool" giving faster, more reliable service to callers who
# identify themselves. Set CROSSREF_MAILTO to your own address to join it. Without it
# the tool still works, on the anonymous pool.
MAILTO = os.environ.get("CROSSREF_MAILTO", "").strip()
UA = "hallucite/1.0 (+https://github.com/julien-tremblay/hallucite)" + (
    f" mailto:{MAILTO}" if MAILTO else "")
TIMEOUT = 12
# arXiv id: new-style 2101.01234[v2] OR old-style quant-ph/0101012, math.AG/0512013
ARXIV_RE = r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})"


def clean_doi(doi):
    """Trailing punctuation is a prose habit, not part of the DOI. Leaving it attached
    makes doi.org 404 and the reference read as FABRICATED, which is a false accusation
    and the most damaging thing this tool can do."""
    return (doi or "").strip().strip("<>").rstrip(".,;:)]}'\"").strip()


def _get(url, accept="application/json"):
    """`accept` is a parameter because doi.org uses content negotiation: it needs
    application/vnd.citationstyles.csl+json to return metadata rather than a redirect
    to the publisher landing page."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace"), r.status


def norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()


def title_match(a, b):
    ta, tb = norm(a), norm(b)
    if not ta or not tb:
        return 0.0
    seq = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    setj = len(set(ta) & set(tb)) / len(set(ta) | set(tb))
    return max(seq, setj)  # either a close string OR strong token overlap counts


# ---- reference extraction --------------------------------------------------
def parse_bib(text):
    """Yield dicts for each @entry with the fields we can verify."""
    refs = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,]+),(.*?)\n\}", text, re.S):
        # The old bibtex entry regex stopped before the final newline while field()
        # required a trailing one, so the LAST field of every entry was invisible.
        # Worst case: when title came last it parsed as empty, check_doi took the
        # 'no claimed title' branch, and returned OK for any DOI that resolved. That
        # silently disabled MISMATCH detection, which is the entire point of the tool.
        # un `\n` terminal: le DERNIER champ de chaque entree etait donc
        # invisible. Consequence la plus grave: si `title` est en dernier, il est
        # vide, check_doi prend la branche `not claimed_title` et retourne OK sur
        # tout DOI qui resout. La detection de MISMATCH, raison d'etre de l'outil,
        # etait desactivee EN SILENCE sur une entree parfaitement bien formee.
        etype, key, body = m.group(1).lower(), m.group(2).strip(), m.group(3) + "\n"
        if etype in ("comment", "string", "preamble"):
            continue

        def field(name):
            """Valeur d'un champ, en comptant les accolades.

            L'ancienne version utilisait `[{"](.+?)[}"]`, aveugle a
            l'imbrication. Sur la protection de casse BibTeX, universelle en
            physique (`title = {{Bell} inequalities ...}`), elle laissait une
            accolade orpheline dans la valeur. Ici l'effet etait benin parce que
            norm() decoupe sur les non-alphanumeriques, mais la valeur affichee
            a l'utilisateur etait fausse et un futur consommateur plus strict
            aurait casse.
            """
            fm = re.search(name + r"\s*=\s*", body, re.I)
            if not fm:
                return ""
            i = fm.end()
            if i < len(body) and body[i] in '{"':
                opener = body[i]
                close = "}" if opener == "{" else '"'
                depth, j = 1, i + 1
                while j < len(body) and depth:
                    if opener == "{" and body[j] == "{":
                        depth += 1
                    elif body[j] == close:
                        depth -= 1
                    j += 1
                return re.sub(r"\s+", " ", body[i + 1 : j - 1]).strip()
            return body[i:].split(",")[0].strip()

        doi = clean_doi(field("doi"))
        eprint = field("eprint")
        # arXiv id: new-style (2101.01234) OR old-style (quant-ph/0101012, math.AG/0512013)
        arxiv = ""
        am = re.search(ARXIV_RE, eprint)
        if not am and "arxiv" in body.lower():
            am = re.search(ARXIV_RE, body)
        if am:
            arxiv = am.group(1)
        refs.append(
            {
                "key": key,
                "type": etype,
                "doi": doi.lower().replace("https://doi.org/", ""),
                "arxiv": arxiv,
                "title": field("title"),
                "year": field("year"),
            }
        )
    return refs


def parse_bibitem(text):
    r"""Parse a LaTeX `thebibliography` block.

    Added 2026-08-25. Without this, `parse_inline` saw only bare DOIs and arXiv
    ids, so a paper whose references live in `\bibitem` entries came back with a
    reference count far below its real one and a clean summary. One private paper
    has 11 `\bibitem`s; this tool reported 2 refs and "0 fabricated".
    """
    refs = []
    blocks = re.split(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}", text)
    # split yields [pre, key1, body1, key2, body2, ...]
    for i in range(1, len(blocks) - 1, 2):
        key, body = blocks[i].strip(), blocks[i + 1]
        body = body.split(r"\bibitem")[0]
        if r"\end{thebibliography}" in body:
            body = body.split(r"\end{thebibliography}")[0]
        # Title extraction. The LaTeX quote form ``Title,'' comes FIRST: this
        # corpus uses \emph{} for "et al." and for journal names, so an
        # emph-first heuristic captured "et al." as the title and reported four
        # correct references as MISMATCH (match 0.14) on 2026-08-25. A parser
        # that manufactures false positives fails a correct paper under --gate,
        # which is the same defect as one that passes a wrong one.
        title = ""
        m = re.search(r"``(.{6,300}?)''", body, re.S)
        if not m:
            m = re.search(r'"(.{6,300}?)"', body, re.S)
        if not m:
            m = re.search(r"\\(?:emph|textit|textsl)\s*\{([^{}]{6,300})\}", body)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".,")
            if re.fullmatch(r"(?:et\s+al\.?|ibid\.?|op\.\s*cit\.?)", title, re.I):
                title = ""
        doi = ""
        md = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", body)
        if md:
            doi = clean_doi(md.group(0).lower())
        arx = ""
        ma = re.search(r"arXiv:\s*(" + ARXIV_RE + ")", body, re.I)
        if ma:
            arx = ma.group(1)
        year = ""
        my = re.search(r"\b(19|20)\d{2}\b", body)
        if my:
            year = my.group(0)
        refs.append(
            {
                "key": key,
                "type": "bibitem",
                "doi": doi,
                "arxiv": arx,
                "title": title,
                "year": year,
            }
        )
    return refs


def parse_inline(text):
    """Fallback: pull bare DOIs and arXiv ids out of prose/tex (no title to match)."""
    refs = []
    for d in sorted(set(re.findall(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", text))):
        refs.append(
            {
                "key": d,
                "type": "inline-doi",
                "doi": d.lower().rstrip(".,;)"),
                "arxiv": "",
                "title": "",
                "year": "",
            }
        )
    for a in sorted(set(re.findall(r"arXiv:\s*" + ARXIV_RE, text, re.I))):
        refs.append(
            {
                "key": "arXiv:" + a,
                "type": "inline-arxiv",
                "doi": "",
                "arxiv": a,
                "title": "",
                "year": "",
            }
        )
    return refs


# ---- verification ----------------------------------------------------------
def check_doi(doi, claimed_title):
    try:
        body, status = _get(
            f"https://api.crossref.org/works/{urllib.parse.quote(doi)}?mailto={MAILTO}"
        )
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return "UNCHECKABLE", f"Crossref HTTP {e.code}"
        # ABSENCE D'UN REGISTRE N'EST PAS ABSENCE. Crossref ne connait que les DOI
        # Crossref only knows DOIs registered with Crossref. DataCite DOIs 404 there
        # while being perfectly valid: that covers most institutional repositories and
        # every arXiv DOI (10.48550/*). Verified live 2026-08-13: 10.48550/arXiv.2512.24601,
        # a real arXiv DOI, was reported FABRICATED for a week, and rightly
        # so. Content negotiation at doi.org covers ALL registration agencies.
        try:
            _b, _st = _get(
                f"https://doi.org/{urllib.parse.quote(doi)}",
                accept="application/vnd.citationstyles.csl+json",
            )
        except urllib.error.HTTPError as e2:
            if e2.code == 404:
                return (
                    "FABRICATED",
                    f"DOI {doi} does not resolve at doi.org (all registration agencies)",
                )
            return "UNCHECKABLE", f"doi.org HTTP {e2.code}"
        except Exception as e2:  # noqa: BLE001
            return "UNCHECKABLE", f"doi.org erreur: {type(e2).__name__}"
        try:
            _csl = json.loads(_b)
        except ValueError:
            return "OK", f"DOI {doi} resout hors Crossref (titre non lisible)"
        found = _csl.get("title") or ""
        if isinstance(found, list):
            found = found[0] if found else ""
        if not claimed_title:
            return "OK", f"DOI resout via une agence non-Crossref: {found[:60]}"
        r = title_match(claimed_title, found)
        return (
            ("OK", f"DOI resout hors Crossref, titre concorde {r:.2f}")
            if r >= 0.6
            else (
                "MISMATCH",
                f"DOI resout a un titre DIFFERENT (match {r:.2f}): '{found[:60]}'",
            )
        )
    except Exception as e:  # noqa: BLE001
        return "UNCHECKABLE", f"Crossref error: {type(e).__name__}"
    try:
        _j = json.loads(body)
    except ValueError:
        return "UNCHECKABLE", "reponse non-JSON (page d'erreur ou corps tronque)"
    msg = _j.get("message", {})
    found = (msg.get("title") or [""])[0]
    if not claimed_title:
        return "OK", f"DOI resolves: {found[:70]}"
    r = title_match(claimed_title, found)
    return (
        ("OK", f"DOI resolves, title match {r:.2f}")
        if r >= 0.6
        else (
            "MISMATCH",
            f"DOI resolves to a DIFFERENT title (match {r:.2f}): got '{found[:60]}'",
        )
    )


def check_arxiv(aid, claimed_title):
    try:
        body, _ = _get(f"http://export.arxiv.org/api/query?id_list={aid}&max_results=1")
    except Exception as e:  # noqa: BLE001
        return "UNCHECKABLE", f"arXiv error: {type(e).__name__}"
    entries = re.findall(r"<entry>(.*?)</entry>", body, re.S)
    if not entries:
        return "FABRICATED", f"arXiv:{aid} has no record"
    tm = re.search(r"<title>(.*?)</title>", entries[0], re.S)
    found = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else ""
    if not claimed_title:
        return "OK", f"arXiv resolves: {found[:70]}"
    r = title_match(claimed_title, found)
    return (
        ("OK", f"arXiv resolves, title match {r:.2f}")
        if r >= 0.6
        else (
            "MISMATCH",
            f"arXiv:{aid} is a DIFFERENT title (match {r:.2f}): got '{found[:60]}'",
        )
    )


def check_title(title):
    try:
        q = urllib.parse.quote(title)
        body, _ = _get(
            f"https://api.crossref.org/works?query.bibliographic={q}&rows=3&mailto={MAILTO}"
        )
    except Exception as e:  # noqa: BLE001
        return "UNCHECKABLE", f"Crossref error: {type(e).__name__}"
    try:
        _j = json.loads(body)
    except ValueError:
        return "UNCHECKABLE", "reponse non-JSON (page d'erreur ou corps tronque)"
    items = _j.get("message", {}).get("items", [])
    best = max(
        (title_match(title, (it.get("title") or [""])[0]) for it in items), default=0.0
    )
    return (
        ("OK", f"title found in Crossref (match {best:.2f})")
        if best >= 0.75
        else (
            "SUSPECT",
            f"no close Crossref match (best {best:.2f}) — verify by hand (book/thesis/non-indexed?)",
        )
    )


def verify(ref):
    """Classify one reference, and NEVER let a registry outage produce an OK.

    The old control flow treated every UNCHECKABLE alike and kept falling through to the
    next, weaker method. But UNCHECKABLE covers two very different things: "this identifier
    was not supplied" and "the registry could not be reached". Conflating them meant a
    FABRICATED arXiv id whose lookup timed out fell through to fuzzy title matching, and a
    generic title scored a 0.85 partial match against something unrelated in Crossref, so the
    reference came back **OK**.

    Measured 2026-08-12, on this tool's own selftest: arXiv answered 429/timeout, and case t4
    (arXiv 9999.99999, title "Nonexistent", expected FABRICATED) was reported OK. A citation
    verifier that passes fabricated references during a network hiccup is worse than no
    verifier, because it is trusted.

    So an ERRORED lookup now poisons the result: the reference stays UNCHECKABLE and says
    why. Refusing to answer is the only honest output when the oracle is unreachable."""
    degraded = None
    if ref["doi"]:
        cls, why = check_doi(ref["doi"], ref["title"])
        if cls != "UNCHECKABLE":
            return cls, why
        if "error" in why.lower() or "HTTP" in why:
            degraded = f"DOI lookup unavailable ({why})"
    if ref["arxiv"]:
        cls, why = check_arxiv(ref["arxiv"], ref["title"])
        if cls != "UNCHECKABLE":
            return cls, why
        if "error" in why.lower() or "HTTP" in why:
            degraded = f"arXiv lookup unavailable ({why})"
    if degraded:
        # Do NOT fall through to title matching. A weaker method cannot clear an identifier
        # that was never actually checked.
        return "UNCHECKABLE", degraded + " -- refusing to fall back to title matching"
    if ref["title"]:
        return check_title(ref["title"])
    return "UNCHECKABLE", "no DOI, arXiv id, or title to verify"


def selftest_parsers():
    """Offline parser fixtures. No network.

    Both cases are real failures this tool shipped, in opposite directions:
      * bibitem_title -- titles here live in ``...'' while \\emph{} holds
        "et al.". An emph-first heuristic captured "et al." as the title and
        reported four CORRECT references as MISMATCH at match 0.14.
      * empty_but_cited -- a .tex whose references the parser cannot read used
        to print "0 hard, 0 soft" and exit 0 under --gate.
    """
    failures = []

    body = r"""\bibitem{Ma2024}
S.~Ma \emph{et al.},
``The Era of 1-bit LLMs: All Large Language Models are in 1.58 Bits,''
arXiv:2402.17764, 2024.
\end{thebibliography}"""
    refs = parse_bibitem(body)
    if len(refs) != 1:
        failures.append(f"bibitem_title: parsed {len(refs)} refs, expected 1")
    elif "Era of 1-bit LLMs" not in refs[0]["title"]:
        failures.append(
            f"bibitem_title: title came out {refs[0]['title']!r}, "
            f"expected the quoted title, not the \\emph{{}} content"
        )
    elif refs[0]["arxiv"] != "2402.17764":
        failures.append(f"bibitem_title: arxiv came out {refs[0]['arxiv']!r}")

    # the fixture must be able to FAIL: an emph-first parser must not pass it
    import re as _re

    m = _re.search(r"\\emph\s*\{([^{}]+)\}", body)
    if not m or "et al" not in m.group(1):
        failures.append(
            "bibitem_title fixture is degenerate: it no longer "
            "contains the \\emph{et al.} that caused the bug"
        )

    if parse_bibitem("no bibitems here"):
        failures.append("empty_but_cited: parse_bibitem invented references")

    print("parser selftest:", "PASS" if not failures else "FAIL")
    for f in failures:
        print("  -", f)
    return not failures


def selftest():
    cases = [
        (
            {
                "doi": "10.1103/physrevlett.88.057902",
                "arxiv": "",
                "title": "Continuous Variable Quantum Cryptography Using Coherent States",
                "key": "t1",
            },
            "OK",
        ),
        (
            {
                "doi": "10.9999/this.does.not.exist.999999",
                "arxiv": "",
                "title": "Fake paper",
                "key": "t2",
            },
            "FABRICATED",
        ),
        (
            {
                "doi": "",
                "arxiv": "2105.03586",
                "title": "Overcoming the repeaterless bound in continuous-variable quantum communication",
                "key": "t3",
            },
            "OK",
        ),
        (
            {"doi": "", "arxiv": "9999.99999", "title": "Nonexistent", "key": "t4"},
            "FABRICATED",
        ),
        (
            {
                "doi": "10.1103/physrevlett.88.057902",
                "arxiv": "",
                "title": "A totally unrelated title about penguins on the moon",
                "key": "t5",
            },
            "MISMATCH",
        ),
    ]
    # A case whose registry could not be REACHED is SKIPPED, not failed. The distinction is
    # the whole point: "the tool gave the wrong answer" and "the oracle was rate-limited" are
    # different events, and calling both FAILURES trains the reader to ignore the selftest.
    # Measured 2026-08-12: arXiv answered 429 and timeouts, so two live cases were unrunnable
    # while the tool itself behaved correctly (it refused to guess). Exit code still reflects
    # real failures only, so a genuine regression cannot hide behind an outage.
    ok, skipped = True, 0
    for ref, want in cases:
        ref.setdefault("year", "")
        cls, why = verify(ref)
        unreachable = cls == "UNCHECKABLE" and "unavailable" in why
        if cls == want:
            mark = "PASS"
        elif unreachable:
            mark = "SKIP"
            skipped += 1
        else:
            mark = "FAIL"
            ok = False
        print(f"  [{mark}] {ref['key']}: got {cls} (want {want}) — {why}")
        time.sleep(0.3)
    verdict = "ALL PASS" if ok else "FAILURES"
    if ok and skipped:
        verdict += f" ({skipped} SKIPPED: registry unreachable, not a defect)"
    print("selftest:", verdict)
    sys.exit(0 if ok else 1)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    strict = "--strict" in sys.argv
    gate = "--gate" in sys.argv
    if "--selftest" in sys.argv:
        if not selftest_parsers():
            sys.exit(1)
        selftest()
    if not args:
        print(__doc__.strip().split("\n\n")[0])
        print(
            "\nusage: hallucite <file.bib|.tex|.md> [...] [--strict] [--gate] [--selftest]"
        )
        sys.exit(2)
    hard, soft = 0, 0
    # Parse every input BEFORE judging any of it: a .tex citing into a sibling .bib is the
    # standard LaTeX layout, and judging the .tex alone reported a hard parser failure for
    # a perfectly normal paper.
    parsed = []
    for path in args:
        text = open(path, encoding="utf-8", errors="replace").read()
        if path.endswith(".bib") or "@article" in text or "@inproceedings" in text:
            refs = parse_bib(text)
        elif r"\bibitem" in text:
            refs = parse_bibitem(text) or parse_inline(text)
        else:
            refs = parse_inline(text)
        parsed.append((path, text, refs))
    any_refs = any(refs for _, _, refs in parsed)

    for path, text, refs in parsed:
        if not refs:
            # A file that clearly HAS references but yielded none is a parser
            # failure, not a clean bill of health. Reporting "0 fabricated"
            # there is the exact silent-degradation this tool exists to prevent.
            has_markers = bool(
                re.search(r"\\cite\{|\\bibitem|@article|@inproceedings", text)
            )
            if has_markers and not any_refs:
                print(f"\n== {path} ==")
                print(
                    "  [PARS] file contains citation markers but 0 references "
                    "were parsed -- this is a PARSER FAILURE, not a clean result"
                )
                hard += 1
            elif has_markers:
                print(f"{path}: citation markers only; references supplied by another input")
            else:
                print(f"{path}: no references found (and no citation markers)")
            continue
        print(f"\n== {path} ({len(refs)} refs) ==")
        for ref in refs:
            cls, why = verify(ref)
            if cls in ("FABRICATED", "MISMATCH"):
                hard += 1
            elif cls in ("SUSPECT", "UNCHECKABLE"):
                soft += 1
            icon = {
                "OK": " ok ",
                "FABRICATED": "FABR",
                "MISMATCH": "MISM",
                "SUSPECT": "SUSP",
                "UNCHECKABLE": "??? ",
            }[cls]
            print(f"  [{icon}] {ref['key'][:40]:40s} {why}")
            time.sleep(0.25)  # be polite to Crossref/arXiv
    print(
        f"\nsummary: {hard} hard (fabricated/mismatch), {soft} soft (suspect/uncheckable)"
    )
    if gate:
        sys.exit(1 if hard or (strict and soft) else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()
