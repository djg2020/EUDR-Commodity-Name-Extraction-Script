"""
Extract the most current EU Combined Nomenclature (CN) codes with product names
and commodity (chapter-level) names from the official EU CELLAR SPARQL endpoint.

Data source: EU Publications Office — CELLAR
  https://publications.europa.eu/webapi/rdf/sparql

Output: cn_codes_<year>.csv
"""

import argparse
import csv
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
REQUEST_TIMEOUT = 180  # seconds per request
MAX_RETRIES = 3
BACKOFF_SECONDS = 2

# ---------------------------------------------------------------------------
# EUDR commodity mapping — Regulation (EU) 2023/1115, Annex I
# Maps CN code prefixes (spaces removed) to the EUDR commodity name.
# The mapping is stored in a separate JSON file so it can be reviewed and
# updated independently of the script logic. This makes regulatory changes
# easier to track and diff over time.
# ---------------------------------------------------------------------------
EUDR_MAPPING_PATH = Path(__file__).with_name("eudr_prefixes.json")
with EUDR_MAPPING_PATH.open(encoding="utf-8") as fh:
    _EUDR_MAPPING_DATA = json.load(fh)

EUDR_PREFIXES: dict[str, list[str]] = _EUDR_MAPPING_DATA.get("commodities", {})
EUDR_MAPPING_CONSOLIDATED_DATE = _EUDR_MAPPING_DATA.get("consolidated_date", "")

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

def _request_with_retries(method: str, url: str, **kwargs) -> requests.Response:
    """Issue an HTTP request with a short retry loop for transient 5xx errors."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = getattr(requests, method)(url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                raise
            print(f"  ⚠ Request failed (attempt {attempt}/{MAX_RETRIES}): {exc}")
            time.sleep(BACKOFF_SECONDS * attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError("Request failed without an exception")


def _sparql_select(query: str) -> list[dict]:
    """Run a SPARQL SELECT via POST and return rows as list of dicts."""
    resp = _request_with_retries(
        "post",
        SPARQL_ENDPOINT,
        data={"query": query},
        timeout=REQUEST_TIMEOUT,
        headers={"Accept": "application/sparql-results+json"},
    )
    data = resp.json()
    vars_ = data["head"]["vars"]
    rows = []
    for binding in data["results"]["bindings"]:
        rows.append({v: binding[v]["value"] if v in binding else "" for v in vars_})
    return rows


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for selecting a CN scheme explicitly.

    Using --year or --scheme-uri makes the export reproducible by pinning the
    CN source version instead of always discovering the latest scheme.
    """
    parser = argparse.ArgumentParser(description="Extract CN codes from the EU CELLAR SPARQL endpoint")
    parser.add_argument("--year", help="Pin the CN publication year to retrieve (for example: 2026)")
    parser.add_argument("--scheme-uri", help="Pin an exact CN concept scheme URI instead of discovering the latest one")
    parser.add_argument("--no-metadata", action="store_true", help="Skip writing the companion metadata JSON file")
    return parser.parse_args(argv)


def infer_year_from_scheme_uri(scheme_uri: str) -> str:
    """Infer a year from a CN scheme URI if possible."""
    match = re.search(r"cn(\d{4})", scheme_uri, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def build_export_metadata(year: str, scheme_uri: str, row_count: int, eudr_mapping_date: str | None = None) -> dict:
    """Build a small metadata block describing the export.

    The metadata file records the selected CN scheme, source endpoint, export
    date and the EUDR mapping version so that later changes are easier to audit.
    """
    return {
        "exported_at": date.today().isoformat(),
        "source": SPARQL_ENDPOINT,
        "year": year,
        "scheme_uri": scheme_uri,
        "row_count": row_count,
        "eudr_mapping_date": eudr_mapping_date or EUDR_MAPPING_CONSOLIDATED_DATE,
    }


# ---------------------------------------------------------------------------
# Step 1 — Discover the most recent CN concept scheme
# ---------------------------------------------------------------------------

def find_latest_cn_scheme(preferred_year: str | None = None) -> tuple[str, str]:
    """Return (scheme_uri, year) for the most recent CN publication.

    When preferred_year is supplied, the function looks for the matching scheme
    explicitly rather than always picking the latest publication returned by the
    endpoint.
    """
    if preferred_year:
        query = f"""
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

SELECT DISTINCT ?scheme WHERE {{
    ?scheme a skos:ConceptScheme .
    FILTER(REGEX(STR(?scheme), "cn{preferred_year}/cn{preferred_year}$", "i"))
}}
ORDER BY DESC(?scheme)
LIMIT 10
"""
        rows = _sparql_select(query)
        if rows:
            best_uri = rows[0]["scheme"]
            return best_uri, preferred_year
        sys.exit(f"ERROR: Could not find any CN concept schemes for year {preferred_year} via SPARQL.")

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

def _sort_rows(rows: list[dict]) -> list[dict]:
    """Return rows in a deterministic order keyed by notation and concept URI.

    This keeps the build step stable even when the upstream SPARQL endpoint does
    not return rows in the same order across runs.
    """
    return sorted(
        rows,
        key=lambda r: (
            r.get("notation", "").replace(" ", ""),
            r.get("concept", ""),
        ),
    )


def build_lookup(rows: list[dict]) -> dict:
    """Build URI -> {notation, label, broader_uri} lookup.
    Merges data from duplicate rows (same concept from different queries)."""
    lookup: dict[str, dict] = {}
    for r in _sort_rows(rows):
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

def write_csv(lookup: dict, year: str, scheme_uri: str, include_metadata: bool = True) -> tuple[Path, Path | None]:
    """Write the CSV export and, optionally, a companion metadata JSON file."""
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

    records.sort(key=lambda r: (r[0], r[1]))

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["cn_code", "product_name", "commodity_name", "section_name", "eudr_commodity"])
        writer.writerows(records)

    metadata_path = None
    if include_metadata:
        metadata = build_export_metadata(year=year, scheme_uri=scheme_uri, row_count=len(records))
        metadata_path = out_path.with_suffix(".meta.json")
        with open(metadata_path, "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    return out_path, metadata_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None):
    args = parse_args(argv)

    print("=" * 60)
    print("EU Combined Nomenclature — CN Code Extractor")
    print(f"Date: {date.today()}")
    print(f"Source: {SPARQL_ENDPOINT}")
    print("=" * 60)

    print("\n[1/5] Checking EUDR regulation for updates …")
    check_eudr_regulation_update()

    print("\n[2/5] Discovering CN concept scheme …")
    if args.scheme_uri:
        scheme_uri = args.scheme_uri
        year = args.year or infer_year_from_scheme_uri(scheme_uri)
        if not year:
            sys.exit("ERROR: Could not infer a year from the provided --scheme-uri.")
    else:
        scheme_uri, year = find_latest_cn_scheme(args.year)
    print(f"  → Selected CN {year}  ({scheme_uri})")

    print(f"\n[3/5] Fetching all CN {year} concepts chapter-by-chapter …")
    raw_rows = fetch_all_cn_concepts(scheme_uri)
    print(f"  → Received {len(raw_rows)} total rows")

    print("\n[4/5] Building hierarchy & resolving commodity/section names …")
    lookup = build_lookup(raw_rows)
    print(f"  → {len(lookup)} unique concepts")

    print("\n[5/5] Writing CSV …")
    out, metadata_path = write_csv(lookup, year, scheme_uri, include_metadata=not args.no_metadata)
    if metadata_path:
        print(f"  → Saved CSV to {out} and metadata to {metadata_path}  ({sum(1 for _ in open(out)) - 1} data rows)")
    else:
        print(f"  → Saved to {out}  ({sum(1 for _ in open(out)) - 1} data rows)")

    print("\nDone ✓")


if __name__ == "__main__":
    main()
