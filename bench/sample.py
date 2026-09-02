#!/usr/bin/env python3
"""Pull a stratified random sample of REAL works from Crossref.

Every record here is real by construction: Crossref returned it, so the DOI is registered
and the title is the registry's own. The correct output of a citation verifier on this set
is therefore zero FABRICATED and zero MISMATCH, and every hit is a false accusation with a
diagnosis attached. Sampling is via Crossref's `sample=` parameter, which is not seedable,
so the drawn DOIs are recorded in the manifest to make the run reproducible.
"""
import json, sys, time, urllib.request, urllib.parse

UA = "hallucite-fpr-measurement/1.0"
STRATA = [
    ("1995-01-01", "1999-12-31", "journal-article", 40),   # legacy DOI era (<>#+ suffixes)
    ("2000-01-01", "2009-12-31", "journal-article", 40),
    ("2010-01-01", "2019-12-31", "journal-article", 40),
    ("2020-01-01", "2026-01-01", "journal-article", 40),
    ("1990-01-01", "2026-01-01", "proceedings-article", 40),
    ("1990-01-01", "2026-01-01", "book-chapter", 30),
    ("1990-01-01", "2026-01-01", "book", 30),
]

def get(url):
    for a in range(4):
        try:
            r = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(r, timeout=30) as f:
                return json.loads(f.read())
        except Exception as e:
            if a == 3: raise
            time.sleep(3 * (a + 1))

out = []
for lo, hi, typ, n in STRATA:
    url = ("https://api.crossref.org/works?"
           f"filter=from-pub-date:{lo},until-pub-date:{hi},type:{typ},has-full-text:false"
           f"&sample={n}")
    try:
        j = get(url)
    except Exception as e:
        print(f"  stratum {typ} {lo[:4]}-{hi[:4]}: FAILED {type(e).__name__}", file=sys.stderr); continue
    got = 0
    for it in j["message"]["items"]:
        t = (it.get("title") or [""])[0].strip()
        if not t or len(t) < 4:
            continue                      # no title in the record: nothing to compare
        out.append({"doi": it["DOI"], "title": t, "type": typ,
                    "year": (it.get("issued", {}).get("date-parts") or [[None]])[0][0]})
        got += 1
    print(f"  {typ:20s} {lo[:4]}-{hi[:4]}: {got}", file=sys.stderr)
    time.sleep(1)
json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
print(f"total {len(out)}", file=sys.stderr)
