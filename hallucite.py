#!/usr/bin/env python3
"""hallucite: find fabricated and mismatched citations. Deterministic, no LLM, no API key.

Motivation (2026-07-11 research digest): the HALLMARK study found a deterministic bibtex/DOI
verifier (F1 0.908) BEATS the best zero-shot LLM (0.840) at catching fabricated citations, and
giving the LLM tool access made it WORSE.
So this is a $0, no-LLM, network-only check that resolves every reference against the authoritative
registries (Crossref for DOIs/titles, arXiv for eprints) and classifies it.

Classes (most→least dangerous):
  FABRICATED  identifier resolves nowhere AND no record matches the title -> hard
  MISMATCH    DOI/arXiv resolves, but to a DIFFERENT title          -> hard
  BAD-DOI     the paper is real, the identifier resolves nowhere    -> soft
  SUSPECT     title-only ref with no close Crossref match           -> soft (may be a book/thesis/non-indexed venue)
              or a resolving id disagreeing with a GUESSED title   -> soft (a guess is not evidence)
  OK          resolved and title matches                            -> pass
  UNCHECKABLE no DOI / arXiv / usable title, or registry unreachable-> soft; unreachable also fails --gate

Exit codes. Without --gate the tool is advisory and always exits 0 (2 on a usage error);
read the summary line. With --gate it exits 1 on any hard finding, and also when a registry
could not be reached, because a check that did not run must not report a pass. --strict
additionally fails on SUSPECT and on references carrying no identifier.

What it does NOT catch: a citation whose title is close but wrong. Title comparison is
lexical, so "Attention Is Not All You Need" scores 0.93 against "Attention Is All You Need"
and passes, as does Recognition -> Segmentation. It catches invented and swapped
references, not subtly altered ones. Authors, venue and year are parsed but never
compared.

Usage:  hallucite <file.bib | file.tex | file.md> [...]   (--strict makes SUSPECT fail too)
Grounding lives in the REGISTRY, never the language. Advisory by default; wire into a commit hook with --gate.
"""

import difflib
import html
import json
import os
import re
import sys
import time
import unicodedata
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
# Prefix on every "the check did not run" message: the oracle could not answer, or the
# request could not be sent safely. verify() used to detect these by
# looking for the substrings "error"/"HTTP" in a human-readable sentence, which failed twice
# over: a non-JSON response said neither, so a fabricated DOI fell through to fuzzy title
# matching and came back OK; and the French "erreur" does not contain "error". A sentinel is
# checked, not guessed at.
NO_ORACLE = "oracle unavailable: "
# Distinct from NO_ORACLE, and deliberately so. NO_ORACLE means the registry FAILED to
# answer, which is transient and must fail the gate. This means the registry answered
# correctly and the record itself has no comparable title: a permanent data gap on their
# side, not a defect in the bibliography, so it is soft and does not fail the gate. Both
# stop verify() from falling through to a weaker check, for opposite reasons.
NO_TITLE = "resolved but unverifiable: "
# arXiv id: new-style 2101.01234[v2] OR old-style quant-ph/0101012, math.AG/0512013. The
# trailing boundary matters: without it the typo arXiv:1706.037621 was truncated into
# 1706.03762, a different real paper, and judged against it.
ARXIV_RE = r"(\d{4}\.\d{4,5}(?!\d)|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?!\d))"
# Where an arXiv id may be taken from: text that CITES it as one. The bibtex parser used to
# search the whole entry for anything id-shaped whenever the word "arxiv" appeared in it,
# so an IEEE Xplore URL gave `document/8765432`, a Zenodo URL gave `record/1234567`, and a
# real paper came back FABRICATED ("arXiv:document/8765432 has no record"). Audit
# 2026-09-28. The DOI form 10.48550/arXiv.* is DataCite's and names the same paper.
ARXIV_CITED = re.compile(r"(?:\barXiv\s*:\s*|arxiv\.org/(?:abs|pdf)/|10\.48550/arXiv\.)"
                         + ARXIV_RE, re.I)
# Crossref restricted the DOI suffix charset in 2008 but never invalidated what was already
# minted, so `<`, `>`, `#` and `+` are legal in pre-2008 DOIs. Wiley's SICI form is the
# common case and it is not rare: 99 of 100 Angewandte Chemie records from 2000-2002 carry
# one. Excluding these characters truncated the DOI at the `<`, and two verifiably real
# papers came back FABRICATED -- a false accusation, the worst output this tool has.
# Measured 2026-09-02 against live Crossref.
DOI_RE = r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9<>#+]+"
_CLOSERS = {")": "(", "]": "[", "}": "{", ">": "<"}


def clean_doi(doi):
    """Trailing punctuation is a prose habit, not part of the DOI. Leaving it attached
    makes doi.org 404 and the reference read as FABRICATED, which is a false accusation
    and the most damaging thing this tool can do."""
    d = (doi or "").strip()
    if d.startswith("<") and d.endswith(">"):
        d = d[1:-1]  # a markdown autolink, <https://doi.org/...>
    while d:
        if d[-1] in ".,;:'\"":
            d = d[:-1]
            continue
        # A legacy DOI's brackets are BALANCED (`...40:6<2004::AID-ANIE2004>3.0.CO;2-5`),
        # so an unbalanced closer came from the surrounding prose, not the identifier.
        # Stripping closers unconditionally, as this did, truncated real DOIs.
        if d[-1] in _CLOSERS and d.count(_CLOSERS[d[-1]]) != d.count(d[-1]):
            d = d[:-1]
            continue
        break
    return d.strip()


# LaTeX escapes inside a DOI. `_` must be escaped in LaTeX text, and Mendeley escapes it in
# .bib fields too, so 10.1162/tacl_a_00349 -- TACL, Neural Computation and Computational
# Linguistics DOIs all carry underscores -- arrived as `10.1162/tacl\_a\_00349`. The bibtex
# path sent the backslashes to the registry and the inline path cut the DOI at the first
# one; either way it 404ed, and a real paper was FABRICATED unless its title happened to
# be rescued. Audit 2026-09-28.
_TEX_ESCAPE = re.compile(r"\{?\\(?:([_%#&])|textunderscore\b\s*(?:\{\})?)\}?")


def find_dois(text):
    """Every DOI in `text`, cleaned and lowercased (DOIs are case-insensitive)."""
    text = _TEX_ESCAPE.sub(lambda m: m.group(1) or "_", text or "")
    return [clean_doi(m.group(0)).lower() for m in re.finditer(DOI_RE, text)]


def _get(url, accept="application/json"):
    """`accept` is a parameter because doi.org uses content negotiation: it needs
    application/vnd.citationstyles.csl+json to return metadata rather than a redirect
    to the publisher landing page."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace"), r.status
        except urllib.error.HTTPError as e:
            # 429 and 503 mean "come back later", not "no such record". Without a retry, a
            # bibliography large enough to trip the rate limiter comes back entirely
            # unverified and --gate fails the build for a reason that has nothing to do
            # with the references. That is how a gate gets switched off for good. Measured
            # 2026-09-02: 22 title queries in one file, all 429, all UNVERIFIED.
            if e.code not in (429, 503) or attempt == 2:
                raise
            wait = e.headers.get("Retry-After") if e.headers else None
            try:
                delay = float(wait)
            except (TypeError, ValueError):
                delay = 2.0 * (attempt + 1)
            time.sleep(min(max(delay, 1.0), 10.0))
    raise urllib.error.URLError("retries exhausted")  # pragma: no cover


# `{\'e}`, `\'{e}`, `\'e` and `{\c c}` all mean one letter. norm() mapped the backslash and
# braces to spaces, so `S{\'e}minaire de g{\'e}om{\'e}trie` tokenised as
# ["s","e","minaire","de","g","e","om","e","trie"] -- every accented word shattered into
# fragments. A real SGA volume scored 0.53 against its own registry record and was reported
# MISMATCH. French and German bibliographies are full of these. Found by running the sweep
# over a private corpus, 2026-09-02; three rounds of hand-built fixtures missed it.
_TEX_ACCENT = [
    re.compile(r"\{\\[a-zA-Z`'^\"~=.]+\s*\{?([A-Za-z])\}?\}"),  # {\'e}  {\c c}  {\c{c}}
    re.compile(r"\\[a-zA-Z]+\s*\{([A-Za-z])\}"),                  # \c{c}  \v{s}
    re.compile(r"\\[`'^\"~=.]\s*\{?([A-Za-z])\}?"),               # \'e  \'{e}  \"u
]


# Letters TeX spells as commands. {\o}, {\ss}, {\L} and the dotless \i in `\'{\i}` (the
# standard way to write í) matched none of the accent patterns, so `Bj{\o}rn` shattered into
# "bj o rn" and `F\'{\i}sica` into "f i sica". The (?![A-Za-z]) keeps `\it` and `\label` out.
_TEX_LETTERS = {"o": "ø", "O": "Ø", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "aa": "å",
                "AA": "Å", "ss": "ß", "l": "ł", "L": "Ł", "i": "i", "j": "j"}
_TEX_LETTER = re.compile(r"\{?\\(" + "|".join(sorted(_TEX_LETTERS, key=len, reverse=True))
                         + r")(?![A-Za-z])\s*\}?")
_GREEK = ("alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda "
          "mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega "
          "Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega").split()
_GREEK_CHAR = dict(zip(_GREEK, "αβγδεεζηθθικλμνξππρρσςτυφφχψωΓΔΘΛΞΠΣΥΦΨΩ"))
_TEX_GREEK = re.compile(r"\\(" + "|".join(sorted(_GREEK, key=len, reverse=True)) + r")(?![A-Za-z])")
# Font and text commands carry no letters of their own: `\emph{Title}` is "Title".
_TEX_FONT = re.compile(r"\\(?:emph|text(?:it|bf|sl|sc|rm|tt|sf|up|normal)?|math(?:rm|bf|it|cal|bb|"
                       r"sf|tt|frak|scr)?|mbox|ensuremath|boldsymbol|operatorname)\s*|"
                       r"(?<=\{)\\(?:em|it|bf|sl|sc|rm|tt|sf)\b\s*")
# Crossref titles carry JATS, HTML and MathML markup: `CO<sub>2</sub>`, `<i>E. coli</i>`,
# whole <mml:math> trees. A bibliography writes `CO$_2$`. Tags are deleted, not spaced, so
# both sides become "CO2"; the TeX copy that JATS keeps beside a formula is dropped so the
# formula is not counted twice. The benchmark could not see any of this: it copies the
# registry's own title, markup and all, into the bibliography. Audit 2026-09-28.
_MARKUP = re.compile(
    r"<((?:[a-z]+:)?(?:tex-math|annotation))\b[^>]*>.*?</\1>"
    r"|</?(?:[a-z]+:)?(?:i|b|em|strong|sub|sup|sc|scp|u|span|p|br|italic|bold|monospace|"
    r"underline|inline-formula|alternatives|math|mi|mn|mo|ms|mtext|mspace|msub|msup|msubsup|"
    r"mrow|mfrac|mover|munder|munderover|mstyle|mfenced|msqrt|mroot|mpadded|mphantom|"
    r"semantics|none|mprescripts|mmultiscripts|mtable|mtr|mtd)\b[^>]*>", re.I | re.S)


def _math(m):
    """Inline math, folded to the characters it prints: `$_2$` is "2", `$\alpha$` is "α"."""
    inner = _TEX_GREEK.sub(lambda g: _GREEK_CHAR[g.group(1)], m.group(1))
    inner = _TEX_FONT.sub("", inner)
    return re.sub(r"[{}_^\s]|\\[,;:!]", "", inner)


def _untex(s):
    s = re.sub(r"\$([^$]{0,200})\$", _math, s)
    s = _TEX_LETTER.sub(lambda m: _TEX_LETTERS[m.group(1)], s)
    for rx in _TEX_ACCENT:
        s = rx.sub(r"\1", s)
    s = _TEX_GREEK.sub(lambda g: _GREEK_CHAR[g.group(1)], s)
    s = _TEX_FONT.sub("", s)
    # Braces group; they never separate words. Mapped to spaces, as they were, the case
    # protection in `A Primer in {BERT}ology` split the word, and "bert ology" no longer
    # matched the registry's "BERTology".
    return s.replace("{", "").replace("}", "")


def norm(s):
    """Fold a title to comparable tokens.

    This used to keep only [a-z0-9], which deletes every character of a title written in
    Japanese, Chinese, Cyrillic, Greek, Arabic, Hebrew or Korean. Two IDENTICAL non-Latin
    titles therefore scored 0.00 and the reference was reported MISMATCH -- a hard failure,
    under --gate, on a correct citation. Measured 2026-09-02.

    Accents are folded rather than deleted so that a LaTeX-escaped `{\"U}ber` still matches
    the registry's `Über`; str.isalnum is Unicode-aware, so every other script survives
    intact and compares against itself.
    """
    s = _untex(html.unescape(_MARKUP.sub("", s or "")))
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c if c.isalnum() else " " for c in s).split()


def _ordered_coverage(ta, tb):
    """Fraction of the shorter token list found in the longer one IN ORDER."""
    short, lng = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not short:
        return 0.0
    sm = difflib.SequenceMatcher(None, short, lng, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(short)


# Containment can establish "plausibly the same work"; it can never establish identity, so
# its score is capped below the 0.90 bar that certifies one. Without the cap, a title that
# merely BEGINS with the cited one scores 1.00 -- which is how "Attention Is All You Need"
# was certified against "Attention Is All You Need: An Analysis Of The Valuation Of Art".
_CONTAINMENT_CEILING = 0.85
# The bar that says "this IS that work". Containment sits strictly below it, so ordered
# containment alone can never certify identity anywhere in the tool. It was 0.75 for a
# title-only reference, which put the bar INSIDE the containment band: on real data seven
# references flipped SUSPECT -> OK at exactly 0.85, among them an 1812 Hegel volume matched
# against some modern paper named after it. A reference carrying no identifier at all is
# precisely where a containment-only match should NOT be enough.
IDENTITY = 0.90


def title_match(a, b):
    """0..1 similarity of two titles: a close string, OR one contained in the other in order.

    The second term was unordered token overlap, which discards word order entirely, so
    "Learning to Rank for Information Retrieval" and "Information Retrieval for Learning to
    Rank" -- different papers -- scored 1.00, as did "Attention Is All You Need" against
    "Is Attention All You Need?". That is how a wrong Crossref record got certified as the
    right one. Ordered containment rejects both (0.50, 0.88).

    The term cannot simply be dropped: a real paper cited without the registry's long
    subtitle falls to 0.52 on the string ratio alone and would be reported MISMATCH. So
    containment stays, capped, to rescue that without certifying anything.
    """
    ta, tb = norm(a), norm(b)
    if not ta or not tb:
        return 0.0
    # autojunk=False: with the default, difflib treats every character that makes up more
    # than 1% of a sequence of 200 or more as junk. For a joined title that long that is
    # the space and most vowels, so a one-word edit to a 250-character clinical-trial title
    # scored 0.54 instead of 0.99, and such a title could never clear the 0.90 bar that
    # rescues a real paper from a dead DOI. Audit 2026-09-28.
    seq = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb), autojunk=False).ratio()
    return max(seq, _CONTAINMENT_CEILING * _ordered_coverage(ta, tb))


def _record_titles(rec):
    """Every title a registry record (Crossref or CSL-JSON) carries, first title first.

    Crossref splits `Title: Subtitle` across `title` and `subtitle`, and keeps the
    original-language title in its own field. Only title[0] was read, so a reference citing
    the full title scored 0.85 against the bare one -- below the 0.90 bar, so a real paper
    with a dead DOI was FABRICATED -- and one citing a paper by its original title read
    against the translation. Short titles are left out on purpose: "BERT" matching "BERT"
    would certify identity at the bar a fabricated reference must not clear. Audit
    2026-09-28.
    """
    def strs(v):
        if isinstance(v, str):
            return [v]
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    titles, subs = strs(rec.get("title")), strs(rec.get("subtitle"))
    out = titles + ([f"{titles[0]}: {subs[0]}"] if titles and subs else [])
    out += strs(rec.get("original-title"))
    return [t for t in out if t.strip()]


def _best_title(claimed, titles):
    """(score, title) of the registry title closest to the cited one."""
    return max(((title_match(claimed, t), t) for t in titles), default=(0.0, ""))


# ---- reference extraction --------------------------------------------------
def _entries(text):
    """Yield (etype, inner text) for each @entry, counting braces.

    The old regex required the closing brace to start a line. An indented `  }`, a `}}`
    riding on the last field's line, and a one-line entry were all INVISIBLE, and an
    invisible entry is not reported as anything -- it is simply absent from the count.
    Measured 2026-09-02: a two-entry file whose second entry carried a fabricated DOI and
    an indented closing brace passed --gate with exit 0. Silent partial loss is the exact
    failure this tool exists to prevent, so the scanner must not depend on layout.
    """
    # `@article(key, ...)` is legal BibTeX and was silently skipped, so a mixed file lost
    # those entries without a word. With `(` delimiters a `)` closes the entry only at brace
    # depth zero and outside a quoted value. Scanning resumes AFTER each entry, so an `@`
    # inside a field value cannot open a phantom entry.
    pos = 0
    for m in re.finditer(r"@(\w+)\s*([{(])", text):
        if m.start() < pos:
            continue
        i = m.end()
        closer = "}" if m.group(2) == "{" else ")"
        depth, j, quoted = 0, i, False
        while j < len(text):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0 and closer == "}":
                    break
                depth -= 1
            elif closer == ")" and depth == 0:
                if c == '"':
                    quoted = not quoted
                elif c == ")" and not quoted:
                    break
            j += 1
        if j >= len(text):
            continue  # unterminated entry: truncated file
        pos = j + 1
        yield m.group(1).lower(), text[i:j]


# Month abbreviations are predefined macros in every BibTeX style.
_MONTHS = {m: m for m in ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                          "oct", "nov", "dec")}


def _bib_value(body, i, macros, raw_words=False):
    """Parse the BibTeX value starting at body[i]: `{...}`, `"..."`, a number, or an
    @string macro, joined by `#`.

    Returns None when the value uses a macro that is not defined, which is different from
    empty: the old parser returned the bare word, so `title = t1` was compared against the
    registry as the title "t1" and the reference came back MISMATCH. `raw_words` keeps an
    undefined bare word verbatim, for fields like `doi` that people write unbraced.

    Inside `"..."` a `"` at brace depth > 0 does not end the value. The old parser stopped
    at the first `"`, so `title = "{\\"U}ber ..."` -- the form BibTeX itself requires for
    an umlaut in a quoted field -- parsed as `{\\`, scored 0.00, and read MISMATCH.
    """
    parts = []
    while True:
        while i < len(body) and body[i].isspace():
            i += 1
        if i >= len(body):
            break
        if body[i] in '{"':
            close = "}" if body[i] == "{" else '"'
            depth, j = 0, i + 1
            while j < len(body):
                c = body[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    if depth == 0 and close == "}":
                        break
                    depth -= 1
                elif c == '"' and close == '"' and depth == 0:
                    break
                j += 1
            parts.append(body[i + 1 : j])
            i = j + 1
        else:
            m = re.match(r"[^\s,#{}\"]+", body[i:])
            if not m:
                break
            word = m.group(0)
            i += len(word)
            if word.isdigit():
                parts.append(word)
            elif word.lower() in macros:
                parts.append(macros[word.lower()])
            elif raw_words:
                parts.append(word)
            else:
                return None
        while i < len(body) and body[i].isspace():
            i += 1
        if i < len(body) and body[i] == "#":
            i += 1
            continue
        break
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def parse_bib(text):
    """Yield dicts for each @entry with the fields we can verify."""
    refs = []
    macros = dict(_MONTHS)
    for etype, inner in _entries(text):
        if etype == "string":
            # @string{name = value}. Defined in file order, and a value may use an earlier one.
            sm = re.match(r"\s*([^\s=]+)\s*=\s*", inner)
            if sm:
                val = _bib_value(inner, sm.end(), macros)
                if val is not None:
                    macros[sm.group(1).lower()] = val
            continue
        key, _, body = inner.partition(",")
        key = key.strip()
        # The old bibtex entry regex stopped before the final newline while field()
        # required a trailing one, so the LAST field of every entry was invisible.
        # Worst case: when title came last it parsed as empty, check_doi took the
        # 'no claimed title' branch, and returned OK for any DOI that resolved. That
        # silently disabled MISMATCH detection, which is the entire point of the tool.
        # `control` is REVTeX's bookkeeping entry (@CONTROL{REVTEX42Control}); it carries
        # no reference. It only became visible once the scanner stopped needing a newline.
        if etype in ("comment", "preamble", "control") or not body.strip():
            continue
        body += "\n"

        def field(name, raw_words=False):
            """One field's value, counting braces, or "" if absent or unresolvable.

            The earlier `[{"](.+?)[}"]` was blind to nesting. BibTeX case
            protection is universal in physics (`title = {{Bell} inequalities
            ...}`), so it left an orphan brace in the value. The effect here was
            benign because norm() splits on non-alphanumerics, but the value
            shown to the user was wrong and a stricter consumer would break.
            """
            # The name must sit at a field boundary. Without the delimiter, `title`
            # matched inside `booktitle` and `journaltitle` -- whichever came first won,
            # so an @inproceedings that listed booktitle before title had the PROCEEDINGS
            # NAME checked against the DOI and a correct reference was reported MISMATCH.
            # biblatex, which spells the field `journaltitle`, is hit on every article.
            fm = re.search(r"(?:^|[,{\s])" + name + r"\s*=\s*", body, re.I)
            if not fm:
                return ""
            return _bib_value(body, fm.end(), macros, raw_words) or ""

        # The doi field is searched for a DOI rather than taken verbatim. Only a
        # `https://doi.org/` prefix used to be stripped, so `http://dx.doi.org/...`,
        # `doi:...` and `DOI: ...` -- all common in exports -- went to the registry as
        # written, 404ed, and a real paper was FABRICATED unless its title was rescued.
        # @misc entries routinely park the DOI in `note` or `howpublished` instead, so the
        # whole entry is the fallback; a reference carrying a resolvable DOI in plain sight
        # used to come back UNCHECKABLE.
        dois = find_dois(field("doi", raw_words=True)) or find_dois(body)
        doi = dois[0] if dois else ""
        # An eprint field is an arXiv id unless the entry says it belongs to another archive
        # (biblatex `eprinttype = {hdl}`, `archiveprefix = {HAL}`), and only if it IS one.
        arxiv = ""
        archive = (field("eprinttype", True) or field("archiveprefix", True)).lower()
        em = re.fullmatch(r"\s*(?:arXiv\s*:\s*)?" + ARXIV_RE + r"(?:v\d+)?\s*",
                          field("eprint", raw_words=True), re.I)
        if em and archive in ("", "arxiv"):
            arxiv = em.group(1)
        else:
            am = ARXIV_CITED.search(body)
            if am:
                arxiv = am.group(1)
        refs.append(
            {
                "key": key,
                "type": etype,
                "doi": doi,
                "arxiv": arxiv,
                "title": field("title"),
                "title_guess": False,
                "year": field("year", raw_words=True),
            }
        )
    return refs


def _braced(s, i):
    """Content of the group whose `{` sits just before s[i], or None if unterminated."""
    depth, j = 1, i
    while j < len(s) and depth:
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
        j += 1
    return None if depth else s[i : j - 1]


# Bibliographies use these where a title would sit; none of them is one.
_NOT_A_TITLE = re.compile(r"(?:et\s+al\.?|ibid\.?|op\.\s*cit\.?|eds?\.?|[\W\d_]+)", re.I)
_FONT = re.compile(r"\\(?:emph|textit|textsl|textbf|textsc|textrm|mbox|enquote)\s*\{|"
                   r"\{\\(?:em|it|sl|bf|sc|rm)\b\s*")


def _tidy_title(raw):
    """Collapse whitespace and drop font wrappers (their braces are harmless to norm())."""
    t = re.sub(r"\s+", " ", _FONT.sub("{", raw)).strip()
    t = t.rstrip(".,").strip()
    if t.startswith("{") and _braced(t, 1) == t[1:-1]:
        t = t[1:-1].strip()
    return "" if _NOT_A_TITLE.fullmatch(t) else t


def _bibitem_title(body):
    r"""The title of a \bibitem when the entry says unambiguously where it is, else None.

    Ranked by how unambiguous the source is. A title from any of these may produce
    MISMATCH; one from _bibitem_emph_guess may not.

    1. ``...'' or "..." -- IEEE-style bibliographies quote the title. The LaTeX quote form
       comes FIRST: one corpus uses \emph{} for "et al." and for journal names, so an
       emph-first heuristic captured "et al." as the title and reported four correct
       references as MISMATCH (match 0.14) on 2026-08-25. An explicit quote IS the title,
       however short, so the floor is 2; at 6 it silently dropped ``Chaos'' and ``Two''.
       A `"` preceded by a backslash is an umlaut, not a quotation mark: `Schr\"odinger
       and G\"odel` used to parse as the title `odinger and G\` and read MISMATCH.
    2. \bibinfo{title}{...} -- what ACM's and REVTeX's .bbl files write.
    3. The first \newblock segment -- what every standard BibTeX style (plain, plainnat,
       abbrv, unsrt, alpha, apalike) writes: authors, \newblock title, \newblock venue.
       Before this, a plainnat .bbl had its \emph{} VENUE read as the title, so every
       reference carrying a DOI came back MISMATCH against "Nature" or "Proceedings of
       the IEEE Conference on ...". Found by audit 2026-09-28.
    """
    m = re.search(r"``(.{2,300}?)''", body, re.S)
    if not m:
        m = re.search(r'(?<!\\)"(.{2,300}?)(?<!\\)"', body, re.S)
    if m:
        return _tidy_title(m.group(1))
    bi = re.search(r"\\bibinfo\s*\{title\}\s*\{", body)
    if bi:
        return _tidy_title(_braced(body, bi.end()) or "")
    if r"\newblock" in body:
        seg = re.split(r"\\newblock\b", body)[1].strip()
        # A segment that opens with the venue or a link means the entry had no title.
        if re.match(r"(?:In\b|\\url|\\href|\\doi|\\urlprefix)", seg):
            return ""
        return _tidy_title(seg)
    return None


def _bibitem_emph_guess(body):
    r"""A title GUESSED from the first \emph{}/\textit{}. Guessed, because \emph{} holds the
    title in some styles and the journal or "et al." in others, and nothing in the entry
    says which. verify() therefore never lets a guessed title produce MISMATCH.

    The 6-character floor stays because it is a guess, but the scan must count braces:
    `[^{}]` could not cross the inner braces of `\emph{On {BIC} states}`, so a title
    carrying LaTeX case protection or inline math was dropped entirely.
    """
    for em in re.finditer(r"\\(?:emph|textit|textsl|textbf)\s*\{", body):
        raw = _braced(body, em.end())
        if raw is None or not 6 <= len(raw) <= 300 or r"\bibinfo" in raw:
            continue
        return _tidy_title(raw)
    return ""


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
        title, guessed = _bibitem_title(body), False
        if title is None:
            title, guessed = _bibitem_emph_guess(body), True
        dois = find_dois(body)
        doi = dois[0] if dois else ""
        arx = ""
        ma = ARXIV_CITED.search(body)
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
                "title_guess": guessed and bool(title),
                "year": year,
            }
        )
    return refs


def parse_inline(text):
    """Fallback: pull bare DOIs and arXiv ids out of prose/tex (no title to match)."""
    refs = []
    for d in sorted(set(find_dois(text))):
        refs.append(
            {
                "key": d,
                "type": "inline-doi",
                "doi": d,
                "arxiv": "",
                "title": "",
                "year": "",
            }
        )
    # An arXiv DOI (10.48550/arXiv.*) is already checked as a DOI above; only the `arXiv:`
    # and arxiv.org URL forms add an arXiv reference. The URL form was not recognised at
    # all, so a markdown paper linking its preprints listed none of them.
    cited = {m.group(1) for m in ARXIV_CITED.finditer(text)
             if not m.group(0).lower().startswith("10.48550")}
    for a in sorted(cited):
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
# A `.` or `..` path segment is resolved by the server before the lookup, so the DOI
# `10.1234/../../works/10.1038/nature14539` would be answered with the record of a different,
# real DOI. No registered DOI needs one. Audit 2026-09-28; not tried against the live API.
_DOT_SEGMENT = re.compile(r"(?:^|/)\.{1,2}(?:/|$)")


def check_doi(doi, claimed_title):
    if _DOT_SEGMENT.search(doi):
        return "UNCHECKABLE", NO_ORACLE + (f"not sent: {doi} contains a '.' or '..' path "
                                           f"segment, which would be looked up as another DOI")
    try:
        body, status = _get(
            f"https://api.crossref.org/works/{urllib.parse.quote(doi)}?mailto={MAILTO}"
        )
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return "UNCHECKABLE", NO_ORACLE + f"Crossref HTTP {e.code}"
        return _check_doi_elsewhere(doi, claimed_title)
    except Exception as e:  # noqa: BLE001
        return "UNCHECKABLE", NO_ORACLE + f"Crossref {type(e).__name__}"
    try:
        _j = json.loads(body)
    except ValueError:
        # A 200 carrying an HTML maintenance or captcha page. This is an outage wearing a
        # success code, and it must poison the result rather than fall through.
        return "UNCHECKABLE", NO_ORACLE + "Crossref returned non-JSON (error page?)"
    titles = _record_titles(_j.get("message", {}))
    found = titles[0] if titles else ""
    if not claimed_title:
        return "OK", f"DOI resolves: {found[:70]}"
    if not titles:
        # Crossref records do occasionally carry an empty title (chapoton_livernet_2001,
        # a real IMRN 2001 paper with a correct DOI). Comparing against "" scores 0.00 and
        # read as MISMATCH: the registry's gap became an accusation against the author.
        return "UNCHECKABLE", NO_TITLE + f"{doi} resolves, but the registry record has no title"
    r, found = _best_title(claimed_title, titles)
    return (
        ("OK", f"DOI resolves, title match {r:.2f}")
        if r >= 0.6
        else (
            "MISMATCH",
            f"DOI resolves to a DIFFERENT title (match {r:.2f}): got '{found[:60]}'",
        )
    )


def _check_doi_elsewhere(doi, claimed_title):
    """A DOI Crossref does not hold.

    ABSENCE FROM ONE REGISTRY IS NOT ABSENCE. Crossref only knows DOIs registered with
    Crossref. DataCite DOIs 404 there while being perfectly valid: that covers most
    institutional repositories and every arXiv DOI (10.48550/*). Verified live 2026-08-13:
    10.48550/arXiv.2512.24601, a real arXiv DOI, was reported FABRICATED for a week.

    Whether the DOI EXISTS is asked of the DOI system itself, through its Handle API, which
    answers from the registry and redirects nowhere. It used to be inferred from content
    negotiation at doi.org, which answers by REDIRECTING -- to the agency's metadata service
    when there is one, to the publisher's landing page when there is not -- and urllib
    follows redirects. A publisher's 404 on a moved landing page therefore read as "does
    not resolve at doi.org (all registration agencies)", and a registered DOI came back
    FABRICATED. Reproduced with real urllib against a local server, audit 2026-09-28.
    """
    try:
        hbody, _ = _get(f"https://doi.org/api/handles/{urllib.parse.quote(doi)}")
        code = json.loads(hbody).get("responseCode")
    except urllib.error.HTTPError as e:
        code = 100 if e.code == 404 else None
        if code is None:
            return "UNCHECKABLE", NO_ORACLE + f"doi.org handle API HTTP {e.code}"
    except (ValueError, AttributeError):
        return "UNCHECKABLE", NO_ORACLE + "doi.org handle API returned something that is not JSON"
    except Exception as e:  # noqa: BLE001
        return "UNCHECKABLE", NO_ORACLE + f"doi.org {type(e).__name__}"
    if code == 100:  # the Handle System's "handle not found"
        return _unresolvable(f"DOI {doi} is not registered with any agency (doi.org)",
                             f"DOI {doi}", claimed_title)
    if code not in (1, 200):  # 1: found; 200: found, but it carries no values
        return "UNCHECKABLE", NO_ORACLE + f"doi.org handle API responseCode {code}"

    # The DOI is registered. What remains is whether its title matches, which needs the
    # agency's metadata. An agency without content negotiation lands on a publisher page,
    # and a publisher may refuse bots. Both are permanent gaps on their side, not outages:
    # they were NO_ORACLE, which failed --gate on every run with "re-run when the registry
    # answers", forever. The DOI system has just answered, so this is not an outage.
    try:
        b, _ = _get(f"https://doi.org/{urllib.parse.quote(doi)}",
                    accept="application/vnd.citationstyles.csl+json")
        titles = _record_titles(json.loads(b))
    except Exception:  # noqa: BLE001
        return "UNCHECKABLE", NO_TITLE + (f"{doi} is registered outside Crossref, but its "
                                          f"agency returned no readable metadata")
    found = titles[0] if titles else ""
    if not claimed_title:
        return "OK", f"DOI resolves via a non-Crossref agency: {found[:60]}"
    if not found.strip():
        # The empty-title guard below was added to the Crossref path only, so a DataCite
        # record with no title still scored 0.00 and read MISMATCH. Audit 2026-09-28.
        return "UNCHECKABLE", NO_TITLE + f"{doi} resolves, but the registry record has no title"
    r, found = _best_title(claimed_title, titles)
    return (
        ("OK", f"DOI resolves (non-Crossref agency), title match {r:.2f}")
        if r >= 0.6
        else (
            "MISMATCH",
            f"DOI resolves to a DIFFERENT title (match {r:.2f}): '{found[:60]}'",
        )
    )


_ARXIV_API = "https://export.arxiv.org/api/query?"
_arxiv_last = [0.0]


def _arxiv_get(query):
    """arXiv asks API clients for at most one request every three seconds. The tool paced
    every registry at 0.25 s, so an arXiv-heavy bibliography invited 429s and, at worst,
    a blocked address. The query also went out over plain http, where anyone on the path
    could rewrite the verdict. Audit 2026-09-28."""
    wait = _arxiv_last[0] + 3.0 - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    try:
        return _get(_ARXIV_API + query)
    finally:
        _arxiv_last[0] = time.monotonic()


def _arxiv_entries(query):
    """(entries, None) from the arXiv API, or (None, reason) if it did not answer."""
    try:
        body, _ = _arxiv_get(query)
    except urllib.error.HTTPError as e:
        return None, NO_ORACLE + f"arXiv HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, NO_ORACLE + f"arXiv {type(e).__name__}"
    # A 200 that is not an Atom feed is a maintenance or rate-limit page. It carries no
    # <entry>, and check_arxiv read that as "no record": a real paper came back FABRICATED
    # during an outage -- the failure already fixed for Crossref's non-JSON 200, never
    # ported here. Audit 2026-09-28.
    if "<feed" not in body:
        return None, NO_ORACLE + "arXiv answered with something that is not an Atom feed"
    return re.findall(r"<entry>(.*?)</entry>", body, re.S), None


def _entry_title(entry):
    tm = re.search(r"<title[^>]*>(.*?)</title>", entry, re.S)
    return html.unescape(re.sub(r"\s+", " ", tm.group(1)).strip()) if tm else ""


def check_arxiv(aid, claimed_title):
    # The subject class in an old-style id is not part of the identifier: math.AG/0512013
    # is math/0512013.
    qid = re.sub(r"^([a-z-]+)\.[A-Za-z]{2}/", r"\1/", aid)
    for _attempt in range(2):
        entries, why = _arxiv_entries(f"id_list={urllib.parse.quote(qid)}&max_results=1")
        if entries is None:
            return "UNCHECKABLE", why
        # arXiv reports a malformed id as an entry whose <id> is its own error page, titled
        # "Error". Read as a record, that is an OK for a fabricated inline id with no title.
        errors = [e for e in entries if "/api/errors" in e]
        records = [e for e in entries if "/api/errors" not in e and _entry_title(e)]
        if records or errors:
            break
        # An empty feed is asked again once before it becomes an accusation. API users report
        # empty answers for valid ids under load; this was not reproduced, only guarded.
    if not records:
        reason = "arXiv rejects the id as malformed" if errors else "arXiv has no record of it"
        return _unresolvable(f"arXiv:{aid}: {reason}", f"arXiv:{aid}", claimed_title)
    found = _entry_title(records[0])
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


def _title_lookup(title):
    """Best Crossref match for a title: (score, found_title), or (None, reason) if the
    registry could not answer. Split out so that a definitively dead DOI can consult it."""
    try:
        q = urllib.parse.quote(title)
        body, _ = _get(
            # rows=3 was too few to be a fair test: for "Attention Is All You Need" the
            # three exact Crossref records rank 8th, 9th and 10th, so the lookup saw only
            # near-misses and matched a DIFFERENT paper.
            f"https://api.crossref.org/works?query.bibliographic={q}&rows=10&mailto={MAILTO}"
        )
    except urllib.error.HTTPError as e:
        return None, NO_ORACLE + f"Crossref HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, NO_ORACLE + f"Crossref {type(e).__name__}"
    try:
        _j = json.loads(body)
    except ValueError:
        return None, NO_ORACLE + "Crossref returned non-JSON (error page?)"
    best, found = 0.0, ""
    for it in _j.get("message", {}).get("items", []):
        r, cand = _best_title(title, _record_titles(it))
        if r > best:
            best, found = r, cand
    return best, found


def _arxiv_title_lookup(title):
    """Best arXiv match for a title, same contract as _title_lookup."""
    words = norm(title)[:12]
    if not words:
        return 0.0, ""
    q = urllib.parse.quote('ti:"' + " ".join(words) + '"')
    entries, why = _arxiv_entries(f"search_query={q}&max_results=10")
    if entries is None:
        return None, why
    best, found = 0.0, ""
    for e in entries:
        cand = _entry_title(e)
        r = title_match(title, cand) if "/api/errors" not in e else 0.0
        if r > best:
            best, found = r, cand
    return best, found


def _title_rescue(title):
    """Is there a real work with this title? (score, found, where) or (None, reason, None).

    Crossref alone could only rescue a paper Crossref holds. NeurIPS and ICML proceedings,
    the usual home of a dead ACM 10.5555 DOI, are generally not Crossref-registered; most of
    those papers are on arXiv. Audit 2026-09-28.

    Any source that certifies the title is enough to rescue. Only when EVERY source answered
    and none certified is the title unknown; if one could not answer, that is half a check.
    """
    best, found = _title_lookup(title)
    if best is not None and best >= IDENTITY:
        return best, found, "Crossref"
    abest, afound = _arxiv_title_lookup(title)
    if abest is not None and abest >= IDENTITY:
        return abest, afound, "arXiv"
    if best is None:
        return None, found, None
    if abest is None:
        return None, afound, None
    return max(best, abest), "", None


def check_title(title):
    best, found = _title_lookup(title)
    if best is None:
        return "UNCHECKABLE", found
    return (
        ("OK", f"title found in Crossref (match {best:.2f})")
        if best >= IDENTITY
        else (
            "SUSPECT",
            f"no close Crossref match (best {best:.2f}) - verify by hand (book/thesis/non-indexed?)",
        )
    )


def _unresolvable(dead, ident, claimed_title):
    """An identifier that resolves nowhere. Is the REFERENCE invented, or only the identifier?

    ACM's `10.5555/*` proceedings range is the standard case: `10.5555/3295222.3295349` is
    "Attention Is All You Need", it 404s at doi.org, and a flat FABRICATED verdict therefore
    accused the most-cited paper in modern machine learning of not existing. Publishers also
    mistype, retire and never register DOIs for work that plainly exists, and a reader who
    is told their real reference is fake stops believing the tool on the one that is. The
    same holds for a mistyped arXiv id, which used to be FABRICATED with no title check.

    Consulting the title here is NOT the fallback verify() refuses. That refusal covers an
    oracle that failed to answer; this is an oracle that answered definitively. The bar is
    0.90 rather than the 0.60 used elsewhere because a fabricated reference almost always
    carries a plausible title, and a loose bar would launder exactly what this tool exists
    to catch.
    """
    if not claimed_title:
        return "FABRICATED", dead
    best, found, where = _title_rescue(claimed_title)
    if best is None:
        # Half a check is not a verdict: the identifier is definitively dead, but with no way
        # to ask about the title we cannot tell a wrong identifier from an invented paper.
        return "UNCHECKABLE", f"{found} -- {ident} resolves nowhere, title unverified"
    if where:
        return "BAD-DOI", (f"the paper is real ({where} match {best:.2f}: "
                           f"'{found[:50]}') but {ident} resolves nowhere: wrong, "
                           f"retired or never-registered identifier")
    return "FABRICATED", f"{dead}, and no Crossref or arXiv record matches the title (best {best:.2f})"


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
    why. Refusing to answer is the only honest output when the oracle is unreachable.

    The same asymmetry applies to the PARSER. A title the parser had to guess is evidence
    of nothing, so a disagreement with it is SUSPECT, never MISMATCH."""
    # A title with no comparable characters (`{\`, `$$`, `--`) is no title. Compared as
    # one it scored 0.00 against the registry and read MISMATCH.
    title = ref["title"] if norm(ref["title"]) else ""
    cls, why = _verify(dict(ref, title=title))
    if cls == "MISMATCH" and ref.get("title_guess"):
        return "SUSPECT", (why + " -- but the cited title was only GUESSED from \\emph{} and "
                           "may be the venue; check this reference by hand")
    return cls, why


def _verify(ref):
    degraded = None
    if ref["doi"]:
        cls, why = check_doi(ref["doi"], ref["title"])
        if cls == "FABRICATED" and ref["arxiv"]:
            # The DOI is dead and the title matched nothing, but the entry also carries an
            # arXiv id, and that was never asked. A real preprint cited with a mistyped DOI
            # came back FABRICATED with its own arXiv id sitting in the entry. Audit
            # 2026-09-28.
            acls, awhy = check_arxiv(ref["arxiv"], ref["title"])
            if acls == "OK":
                return "BAD-DOI", (f"the paper is real ({awhy}, arXiv:{ref['arxiv']}) but "
                                   f"DOI {ref['doi']} resolves nowhere: wrong, retired or "
                                   f"never-registered identifier")
            if acls == "UNCHECKABLE" and NO_ORACLE in awhy:
                return "UNCHECKABLE", (f"DOI {ref['doi']} resolves nowhere and the arXiv id "
                                       f"could not be checked: {awhy}")
            return cls, f"{why}; arXiv:{ref['arxiv']} does not rescue it: {awhy}"
        if cls != "UNCHECKABLE":
            return cls, why
        if why.startswith(NO_TITLE):
            # The DOI resolved: the reference is not fabricated, and no weaker check can
            # add to that. Falling through reported "no close Crossref match", which
            # describes the wrong thing entirely.
            return cls, why
        if why.startswith(NO_ORACLE):
            degraded = "DOI " + why
    if ref["arxiv"]:
        cls, why = check_arxiv(ref["arxiv"], ref["title"])
        if cls != "UNCHECKABLE":
            return cls, why
        if why.startswith(NO_ORACLE):
            degraded = "arXiv " + why
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
                "title": "Quantum Pretzel Bathymetry in Subarctic Systems",
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
            # An invented title, not a generic one: a dead identifier is now checked against
            # its title, and "Nonexistent" could be rescued by a real work of that name.
            {"doi": "", "arxiv": "9999.99999", "key": "t4",
             "title": "Quantum Wombat Onomastics in Triassic Systems"},
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
        # The most-cited paper in modern machine learning, carrying ACM's 10.5555 DL
        # identifier, which resolves nowhere. A flat FABRICATED here accused it of not
        # existing, which is the one verdict this tool must never get wrong.
        (
            {
                "doi": "10.5555/3295222.3295349",
                "arxiv": "",
                "title": "Attention Is All You Need",
                "key": "t6",
            },
            "BAD-DOI",
        ),
        # A DataCite DOI: Crossref 404s it, so this is the one live case that exercises the
        # Handle API and doi.org content negotiation. Neither could be reached when that path
        # was rewritten (audit 2026-09-28), so a failure here is the first place to look.
        (
            {
                "doi": "10.48550/arXiv.1706.03762",
                "arxiv": "",
                "title": "Attention Is All You Need",
                "key": "t7",
            },
            "OK",
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


KNOWN_FLAGS = {"--strict", "--gate", "--selftest"}


# BibTeX and biblatex entry types. A known list rather than `@\w+`, so that a markdown file
# mentioning `@someone(` is not mistaken for a bibliography.
_ENTRY_TYPES = ("article", "book", "booklet", "conference", "inbook", "incollection",
                "inproceedings", "manual", "mastersthesis", "misc", "phdthesis", "proceedings",
                "techreport", "unpublished", "online", "report", "thesis", "dataset",
                "software", "collection", "patent", "electronic", "www", "mvbook",
                "reference", "periodical", "standard")
_BIB_ENTRY = re.compile(r"@(?:" + "|".join(_ENTRY_TYPES) + r")\s*[{(]", re.I)
# Evidence that a file cites something. It was `\cite{`, `\bibitem`, `@article` and
# `@inproceedings`, so natbib's \citep and \citet -- the commonest commands there are --
# biblatex's \parencite, \autocite and \textcite, `\cite[p.~3]{}`, `@Article` and `@book`
# were all invisible. A natbib paper run without its .bib printed "no citation markers" and
# PASSED --gate, where the README promises a hard parser failure. Audit 2026-09-28.
_CITE_MARKER = re.compile(
    r"\\[A-Za-z]*cite[A-Za-z]*\*?\s*(?:\[[^\]]*\]\s*){0,2}\{"
    r"|\\bibitem\b|\\bibliography\s*\{|\\addbibresource\s*\{|\\printbibliography\b"
    r"|" + _BIB_ENTRY.pattern, re.I)


def parse_file(path, text):
    r"""Every reference in one file.

    A .bib is BibTeX. Anything else may mix forms, and each is read: BibTeX entries, then
    \bibitem entries, then any DOI or arXiv id not already accounted for. Choosing ONE parser
    per file used to drop the rest silently: a markdown page quoting a single BibTeX entry
    was parsed as BibTeX only, and every other reference on it vanished from the count.
    """
    if path.lower().endswith(".bib"):
        return parse_bib(text)
    refs = parse_bib(text) if _BIB_ENTRY.search(text) else []
    if r"\bibitem" in text:
        refs += parse_bibitem(text)
    seen = {r["doi"] for r in refs} | {r["arxiv"] for r in refs}
    refs += [r for r in parse_inline(text) if (r["doi"] or r["arxiv"]) not in seen]
    return refs


def main():
    # A misspelled flag used to be dropped silently, so `--gates` ran advisory and exited 0
    # while the caller believed they were gating. A gate you think you enabled and did not
    # is worse than no gate.
    bad = [a for a in sys.argv[1:] if a.startswith("-") and a not in KNOWN_FLAGS]
    if bad:
        print(f"unknown option(s): {' '.join(bad)}", file=sys.stderr)
        print(f"known options: {' '.join(sorted(KNOWN_FLAGS))}", file=sys.stderr)
        sys.exit(2)
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
    hard, soft, degraded, bare = 0, 0, 0, 0
    # Parse every input BEFORE judging any of it: a .tex citing into a sibling .bib is the
    # standard LaTeX layout, and judging the .tex alone reported a hard parser failure for
    # a perfectly normal paper.
    parsed = []
    for path in args:
        # An unreadable path used to raise, and the traceback exited 1 -- indistinguishable
        # under --gate from "this bibliography contains fabricated references".
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError as e:
            print(f"cannot read {path}: {e.strerror}", file=sys.stderr)
            sys.exit(2)
        parsed.append((path, text, parse_file(path, text)))
    any_refs = any(refs for _, _, refs in parsed)

    for path, text, refs in parsed:
        if not refs:
            # A file that clearly HAS references but yielded none is a parser
            # failure, not a clean bill of health. Reporting "0 fabricated"
            # there is the exact silent-degradation this tool exists to prevent.
            has_markers = bool(_CITE_MARKER.search(text))
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
            if cls == "OK" and not norm(ref["title"]):
                # Markdown and bare .tex give identifiers without titles, and so does a
                # bibtex entry with no title field. "OK" there means the identifier EXISTS,
                # not that it is the paper cited: a DOI pointing at a different paper, the
                # failure this tool calls the worse kind, passes. It said so nowhere.
                bare += 1
                why += "  [identifier only: no cited title to compare]"
            if cls in ("FABRICATED", "MISMATCH"):
                hard += 1
            elif cls in ("SUSPECT", "UNCHECKABLE", "BAD-DOI"):
                soft += 1
                if NO_ORACLE in why:
                    degraded += 1
            icon = {
                "OK": " ok ",
                "FABRICATED": "FABR",
                "MISMATCH": "MISM",
                "SUSPECT": "SUSP",
                "BAD-DOI": "BDOI",
                "UNCHECKABLE": "??? ",
            }[cls]
            print(f"  [{icon}] {ref['key'][:40]:40s} {why}")
            time.sleep(0.25)  # be polite to Crossref/arXiv
    print(
        f"\nsummary: {hard} hard (fabricated/mismatch), {soft} soft (suspect/uncheckable)"
        + (f", {degraded} UNVERIFIED (the check could not run)" if degraded else "")
    )
    if bare:
        print(f"note: {bare} reference(s) passed on the identifier alone. With no cited title to\n"
              "      compare, a DOI that points at a different paper would pass too.")
    if degraded:
        print(
            f"WARNING: {degraded} reference(s) could not be checked; the reason is on each line.\n"
            "         This run does not clear them. Re-run when the registry answers."
        )
    if gate:
        # A gate must fail closed. Rate-limited or offline, every reference comes back
        # UNCHECKABLE, and counting that as a soft pass meant --gate exited 0 having
        # verified nothing at all -- green because the oracle was down. Measured
        # 2026-09-02 with the network blackholed: two fabricated DOIs, exit 0.
        # A reference with no identifier to check is a different thing, and still soft.
        sys.exit(1 if hard or degraded or (strict and soft) else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()
