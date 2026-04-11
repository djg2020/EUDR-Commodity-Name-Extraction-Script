"""
Extract the most current EU Combined Nomenclature (CN) codes with product names
and commodity (chapter-level) names from the official EU CELLAR SPARQL endpoint.

Data source: EU Publications Office — CELLAR
  https://publications.europa.eu/webapi/rdf/sparql

Output: cn_codes_<year>.csv
"""

import csv
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
REQUEST_TIMEOUT = 180  # seconds per request

# ---------------------------------------------------------------------------
# EUDR commodity mapping — Regulation (EU) 2023/1115, Annex I
# Maps CN code prefixes (spaces removed) to the EUDR commodity name.
# Prefixes marked "ex" in the regulation (partial headings) are included
# because the heading-level match is the best available without sub-code
# product-level detail.
# ---------------------------------------------------------------------------
EUDR_PREFIXES: dict[str, list[str]] = {
    "Cattle": [
        "010221", "010229",
        "0201", "0202",
        "020610", "020622", "020629",
        "160250",
        "4101", "4104", "4107",
    ],
    "Cocoa": [
        "1801", "1802", "1803", "1804", "1805", "1806",
    ],
    "Coffee": [
        "0901",
    ],
    "Oil palm": [
        "120710",
        "1511",
        "151321", "151329",
        "230660",
        "290545",
        "291570", "291590",
        "3823", "3826",
    ],
    "Rubber": [
        "4001",
        "4005", "4006", "4007", "4008",
        "4010", "4011", "4012", "4013",
        "4015", "4016", "4017",
    ],
    "Soya": [
        "1201",
        "120810",
        "1507",
        "2304",
    ],
    "Wood": [
        "4401", "4402", "4403", "4404", "4405", "4406", "4407", "4408",
        "4409", "4410", "4411", "4412", "4413", "4414", "4415", "4416",
        "4417", "4418", "4419", "4420", "4421",
        # Pulp of wood
        "4701", "4702", "4703", "4704", "4705", "4706", "4707",
        # Paper and paperboard
        "4801", "4802", "4803", "4804", "4805", "4806", "4807", "4808",
        "4809", "4810", "4811", "4812", "4813", "4814", "4816", "4817",
        "4818", "4819", "4820", "4821", "4822", "4823",
        # Printed books, newspapers, pictures, etc.
        "4901", "4902", "4904", "4905", "4906", "4907", "4908",
        "4909", "4910", "4911",
        # Wooden furniture / seats / prefab buildings
        "9401", "9403", "9406",
    ],
}

# Consolidated version of Regulation (EU) 2023/1115 that EUDR_PREFIXES was
# last verified against.  Update this date after reviewing a newer version.
EUDR_MAPPING_CONSOLIDATED_DATE = "20251226"  # corresponds to 2025-12-26

# Build a flat list of (prefix, commodity) sorted longest-prefix-first so that
# more specific matches (e.g. 6-digit) take priority over shorter ones.
_EUDR_FLAT: list[tuple[str, str]] = sorted(
    ((pfx, commodity) for commodity, pfxs in EUDR_PREFIXES.items() for pfx in pfxs),
    key=lambda t: -len(t[0]),
)


def classify_eudr(notation: str) -> str:
    """Return the EUDR commodity name for a CN code, or '' if not covered."""
    code = notation.replace(" ", "")
    for prefix, commodity in _EUDR_FLAT:
        if code.startswith(prefix):
            return commodity
    return ""


def check_eudr_regulation_update() -> None:
    """Check EUR-Lex for a newer consolidated version of the EUDR regulation.
    Prints a warning if the Annex I mapping may be outdated."""
    url = (
        "https://eur-lex.europa.eu/legal-content/EN/ALL/"
        "?uri=CELEX:32023R1115"
    )
    try:
        resp = requests.get(
            url, timeout=30,
            headers={"User-Agent": "CN-Code-Extractor/1.0"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ⚠ Could not reach EUR-Lex to check for EUDR updates: {exc}")
        return

    # Find all consolidated version CELEX identifiers (02023R1115-YYYYMMDD)
    dates = re.findall(r"02023R1115-(\d{8})", resp.text)
    if not dates:
        print("  ⚠ Could not parse consolidated version dates from EUR-Lex.")
        return

    latest = max(dates)
    if latest > EUDR_MAPPING_CONSOLIDATED_DATE:
        fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"  # noqa: E731
        print(
            f"  ⚠ WARNING: A newer consolidated version of Regulation (EU) 2023/1115\n"
            f"    exists on EUR-Lex (dated {fmt(latest)}).\n"
            f"    The EUDR_PREFIXES mapping was last verified against version "
            f"{fmt(EUDR_MAPPING_CONSOLIDATED_DATE)}.\n"
            f"    Annex I (CN code → commodity mapping) may have been amended.\n"
            f"    Please review: https://eur-lex.europa.eu/legal-content/EN/AUTO/"
            f"?uri=CELEX:02023R1115-{latest}"
        )
    else:
        fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"  # noqa: E731
        print(
            f"  ✓ EUDR mapping is up to date "
            f"(consolidated version: {fmt(EUDR_MAPPING_CONSOLIDATED_DATE)})"
        )


# ---------------------------------------------------------------------------
# SPARQL helpers
# ---------------------------------------------------------------------------

def _sparql_select(query: str) -> list[dict]:
    """Run a SPARQL SELECT via POST and return rows as list of dicts."""
    resp = requests.post(
        SPARQL_ENDPOINT,
        data={"query": query},
        timeout=REQUEST_TIMEOUT,
        headers={"Accept": "application/sparql-results+json"},
    )
    resp.raise_for_status()
    data = resp.json()
    vars_ = data["head"]["vars"]
    rows = []
    for binding in data["results"]["bindings"]:
        rows.append({v: binding[v]["value"] if v in binding else "" for v in vars_})
    return rows


# ---------------------------------------------------------------------------
# Step 1 — Discover the most recent CN concept scheme
# ---------------------------------------------------------------------------

def find_latest_cn_scheme() -> tuple[str, str]:
    """Return (scheme_uri, year) for the most recent CN publication."""
    query = """
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

SELECT DISTINCT ?scheme WHERE {
    ?scheme a skos:ConceptScheme .
    FILTER(REGEX(STR(?scheme), "cn20[0-9]{2}/cn20[0-9]{2}$", "i"))
}
ORDER BY DESC(?scheme)
LIMIT 10
"""
    rows = _sparql_select(query)
    if not rows:
        sys.exit("ERROR: Could not discover any CN concept schemes via SPARQL.")

    best_uri = rows[0]["scheme"]
    year = best_uri.rstrip("/").split("cn")[-1]  # e.g. "2026"
    return best_uri, year


# ---------------------------------------------------------------------------
# Step 2 — Fetch concepts per chapter to avoid SPARQL timeout/offset issues
# ---------------------------------------------------------------------------

def _fetch_sections_and_chapters(scheme_uri: str) -> list[dict]:
    """Fetch the top-level concepts: sections (Roman numerals) and chapters
    (2-digit codes), plus their labels and parent links."""
    query = f"""
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

SELECT ?concept ?notation ?label ?broader WHERE {{
    ?concept skos:inScheme <{scheme_uri}> ;
             skos:notation ?notation .
    OPTIONAL {{
        ?concept skos:prefLabel ?label .
        FILTER(LANG(?label) = "en")
    }}
    OPTIONAL {{ ?concept skos:broader ?broader . }}
    FILTER(
        STRLEN(STR(?notation)) <= 5
    )
}}
"""
    return _sparql_select(query)


def _fetch_codes_for_chapter(scheme_uri: str, chapter: str) -> list[dict]:
    """Fetch all codes whose notation starts with the given 2-digit chapter."""
    query = f"""
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

SELECT ?concept ?notation ?label ?broader WHERE {{
    ?concept skos:inScheme <{scheme_uri}> ;
             skos:notation ?notation .
    OPTIONAL {{
        ?concept skos:prefLabel ?label .
        FILTER(LANG(?label) = "en")
    }}
    OPTIONAL {{ ?concept skos:broader ?broader . }}
    FILTER(STRSTARTS(STR(?notation), "{chapter}"))
    FILTER(STRLEN(STR(?notation)) > 2)
}}
"""
    return _sparql_select(query)


def fetch_all_cn_concepts(scheme_uri: str) -> list[dict]:
    """Fetch every CN concept by first fetching sections/chapters, then
    fetching detailed codes chapter-by-chapter to stay within query limits."""
    # Get sections + chapters
    print("  Fetching sections & chapters …")
    top_rows = _fetch_sections_and_chapters(scheme_uri)
    print(f"    → {len(top_rows)} top-level rows")

    # Identify 2-digit chapter codes
    chapters = sorted({
        r["notation"].replace(" ", "")
        for r in top_rows
        if r["notation"].replace(" ", "").isdigit()
        and len(r["notation"].replace(" ", "")) == 2
    })
    print(f"  Found {len(chapters)} chapters: {chapters[0]}..{chapters[-1]}")

    all_rows = list(top_rows)
    for i, ch in enumerate(chapters, 1):
        print(f"  Chapter {ch} ({i}/{len(chapters)}) …", end=" ", flush=True)
        rows = _fetch_codes_for_chapter(scheme_uri, ch)
        all_rows.extend(rows)
        print(f"{len(rows)} codes")
        time.sleep(0.5)  # be polite to the endpoint

    return all_rows


# ---------------------------------------------------------------------------
# Step 3 — Build hierarchy and resolve chapter (commodity) names
# ---------------------------------------------------------------------------

def build_lookup(rows: list[dict]) -> dict:
    """Build URI -> {notation, label, broader_uri} lookup.
    Merges data from duplicate rows (same concept from different queries)."""
    lookup: dict[str, dict] = {}
    for r in rows:
        uri = r["concept"]
        if uri not in lookup:
            lookup[uri] = {
                "notation": r.get("notation", ""),
                "label": r.get("label", ""),
                "broader_uri": r.get("broader", ""),
            }
        else:
            existing = lookup[uri]
            if r.get("label") and not existing.get("label"):
                existing["label"] = r["label"]
            if r.get("broader") and not existing.get("broader_uri"):
                existing["broader_uri"] = r["broader"]
    return lookup


def _clean_label(notation: str, label: str) -> str:
    """Remove the CN code prefix and leading dashes from the label.
    e.g. '0101 21 00 -- Pure-bred breeding animals' -> 'Pure-bred breeding animals'
    """
    if not label:
        return ""
    # Strip notation prefix (formatted with spaces) if present
    formatted = notation
    # Try formatted patterns: "0101 21 00", "0102 29", "0101", etc.
    for fmt in [
        f"{notation[:4]} {notation[4:6]} {notation[6:8]}",
        f"{notation[:4]} {notation[4:6]}",
        notation[:4],
        notation[:2],
        notation,
    ]:
        fmt = fmt.strip()
        if label.startswith(fmt):
            label = label[len(fmt):]
            break
    # Strip leading whitespace, dashes and CHAPTER/SECTION prefixes
    label = re.sub(r"^[\s\-–—]+", "", label)
    label = re.sub(r"^CHAPTER\s+\d+\s*[-–—]\s*", "", label)
    return label.strip()


def _notation_to_chapter_prefix(notation: str) -> str:
    """Extract the 2-digit chapter prefix from a CN notation."""
    clean = notation.replace(" ", "")
    if len(clean) >= 2 and clean[:2].isdigit():
        return clean[:2]
    return ""


def resolve_chapter(uri: str, lookup: dict, _seen: set | None = None) -> str:
    """Walk up the broader chain to find the 2-digit chapter concept and
    return its label (the commodity name).  Returns '' if not found."""
    if _seen is None:
        _seen = set()
    if uri in _seen or uri not in lookup:
        return ""
    _seen.add(uri)
    entry = lookup[uri]
    notation = entry["notation"].replace(" ", "")
    if notation.isdigit() and len(notation) == 2:
        return entry["label"]
    return resolve_chapter(entry["broader_uri"], lookup, _seen)


def resolve_section(uri: str, lookup: dict, _seen: set | None = None) -> str:
    """Walk up the broader chain to the section level (non-digit notations
    like Roman numerals) and return its label."""
    if _seen is None:
        _seen = set()
    if uri in _seen or uri not in lookup:
        return ""
    _seen.add(uri)
    entry = lookup[uri]
    notation = entry["notation"].replace(" ", "")
    if notation and not any(ch.isdigit() for ch in notation):
        return entry["label"]
    return resolve_section(entry["broader_uri"], lookup, _seen)


# ---------------------------------------------------------------------------
# Step 4 — Assemble and write CSV
# ---------------------------------------------------------------------------

def write_csv(lookup: dict, year: str) -> Path:
    out_path = Path(__file__).with_name(f"cn_codes_{year}.csv")

    # Pre-build chapter_prefix -> chapter_label and section_label maps as fallback
    chapter_map: dict[str, str] = {}   # "09" -> "Coffee, tea, maté and spices"
    section_map: dict[str, str] = {}   # "09" -> "SECTION II - VEGETABLE PRODUCTS"
    for uri, entry in lookup.items():
        n = entry["notation"].replace(" ", "")
        if n.isdigit() and len(n) == 2:
            chapter_map[n] = _clean_label(n, entry["label"])
            # Also resolve this chapter's section
            sec = resolve_section(uri, lookup)
            if sec:
                section_map[n] = re.sub(r"^SECTION\s+[IVXLCDM]+\s*[-–—]\s*", "", sec).strip()

    records: list[tuple] = []
    for uri, entry in lookup.items():
        notation = entry["notation"].replace(" ", "")
        raw_label = entry["label"]
        label = _clean_label(notation, raw_label)

        # Skip non-numeric entries (sections like "I", "IV", etc.)
        if not notation or not any(ch.isdigit() for ch in notation):
            continue

        # Resolve commodity (chapter) name via broader chain first, then fallback
        chapter = resolve_chapter(uri, lookup)
        if chapter:
            chapter = _clean_label("", chapter)
            chapter = re.sub(r"^CHAPTER\s+\d+\s*[-–—]\s*", "", chapter).strip()
        if not chapter:
            prefix = _notation_to_chapter_prefix(notation)
            chapter = chapter_map.get(prefix, "")

        # Resolve section name via broader chain, then fallback
        section = resolve_section(uri, lookup)
        if section:
            section = re.sub(r"^SECTION\s+[IVXLCDM]+\s*[-–—]\s*", "", section).strip()
        if not section:
            prefix = _notation_to_chapter_prefix(notation)
            section = section_map.get(prefix, "")

        eudr = classify_eudr(notation)
        records.append((notation, label, chapter, section, eudr))

    records.sort(key=lambda r: r[0])

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["cn_code", "product_name", "commodity_name", "section_name", "eudr_commodity"])
        writer.writerows(records)

    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("EU Combined Nomenclature — CN Code Extractor")
    print(f"Date: {date.today()}")
    print(f"Source: {SPARQL_ENDPOINT}")
    print("=" * 60)

    print("\n[1/5] Checking EUDR regulation for updates …")
    check_eudr_regulation_update()

    print("\n[2/5] Discovering most recent CN concept scheme …")
    scheme_uri, year = find_latest_cn_scheme()
    print(f"  → Found CN {year}  ({scheme_uri})")

    print(f"\n[3/5] Fetching all CN {year} concepts chapter-by-chapter …")
    raw_rows = fetch_all_cn_concepts(scheme_uri)
    print(f"  → Received {len(raw_rows)} total rows")

    print("\n[4/5] Building hierarchy & resolving commodity/section names …")
    lookup = build_lookup(raw_rows)
    print(f"  → {len(lookup)} unique concepts")

    print("\n[5/5] Writing CSV …")
    out = write_csv(lookup, year)
    print(f"  → Saved to {out}  ({sum(1 for _ in open(out)) - 1} data rows)")

    print("\nDone ✓")


if __name__ == "__main__":
    main()
