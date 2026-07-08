# EU CN Code & EUDR Commodity Extraction Script

A Python script that extracts the most current **EU Combined Nomenclature (CN) codes** with product names, commodity names, section names, and **EUDR commodity classifications** from official EU data sources.

## Data Sources

| Data | Source | Type |
|------|--------|------|
| CN codes, product names, hierarchy | [EU Publications Office — CELLAR SPARQL endpoint](https://publications.europa.eu/webapi/rdf/sparql) | Live query (dynamic) |
| EUDR commodity mapping | [Regulation (EU) 2023/1115, Annex I](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32023R1115) | Reviewable JSON mapping derived from the legal text |

The script auto-discovers the latest CN year (e.g. CN 2026) and fetches all codes chapter-by-chapter via SPARQL.

## Output

A CSV file (`cn_codes_<year>.csv`) with the following columns, plus an optional companion metadata file (`cn_codes_<year>.meta.json`) when metadata output is enabled:

| Column | Description |
|--------|-------------|
| `cn_code` | Combined Nomenclature code (digits only, e.g. `01012100`) |
| `product_name` | Product description from the CN classification |
| `commodity_name` | Chapter-level commodity group (e.g. "LIVE ANIMALS") |
| `section_name` | Section-level grouping (e.g. "LIVE ANIMALS; ANIMAL PRODUCTS") |
| `eudr_commodity` | EUDR commodity name if applicable: Cattle, Cocoa, Coffee, Oil palm, Rubber, Soya, or Wood — empty if not EUDR-relevant |

**Sample rows:**

```
cn_code,product_name,commodity_name,section_name,eudr_commodity
0101,"Live horses, asses, mules and hinnies",LIVE ANIMALS,LIVE ANIMALS; ANIMAL PRODUCTS,
010221,Pure-bred breeding animals,LIVE ANIMALS,LIVE ANIMALS; ANIMAL PRODUCTS,Cattle
0901,"Coffee, whether or not roasted or decaffeinated...",COFFEE; TEA; MATÉ AND SPICES,VEGETABLE PRODUCTS,Coffee
```

The current output contains **12,680 CN codes** covering all 97 chapters.

## Requirements

- Python 3.10+
- [`requests`](https://pypi.org/project/requests/)

```bash
pip install requests
```

## Usage

```bash
python cn_code.py
```

Optional flags:

```bash
python cn_code.py --year 2026
python cn_code.py --scheme-uri http://data.europa.eu/xsp/cn2026/cn2026
python cn_code.py --no-metadata
```

The script runs a 5-step pipeline:

1. **Check EUDR regulation for updates** — queries EUR-Lex for newer consolidated versions of Regulation (EU) 2023/1115 and warns if the Annex I mapping may be outdated
2. **Discover CN scheme** — either auto-detects the latest CN year or uses the explicit year/scheme URI you provide
3. **Fetch CN codes** — retrieves all concepts chapter-by-chapter (97 chapters, ~60s)
4. **Build hierarchy** — resolves chapter and section names via `skos:broader` chains
5. **Write output** — outputs `cn_codes_<year>.csv` in the script directory and, by default, a companion `cn_codes_<year>.meta.json` file

## EUDR Commodity Classification

The script classifies each CN code against the seven commodities defined in the [EU Deforestation Regulation (EUDR)](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32023R1115) Annex I:

- **Cattle** — live cattle, beef, offal, hides, leather
- **Cocoa** — beans, paste, butter, powder, chocolate
- **Coffee** — coffee beans, roasted, husks
- **Oil palm** — palm oil, palm kernel oil, derivatives
- **Rubber** — natural rubber, tyres, rubber articles
- **Soya** — soya beans, flour, oil, oilcake
- **Wood** — timber, paper, pulp, furniture, printed products

### Keeping the EUDR Mapping Current

CN codes are fetched live from the EU SPARQL endpoint and are always up to date. The EUDR commodity mapping, however, is stored in a reviewable JSON file so it can be updated independently of the script logic when the regulation or Annex I mapping changes.

The script **automatically checks EUR-Lex** on each run for newer consolidated versions of the regulation. If an amendment is detected, it prints a warning:

```
⚠ WARNING: A newer consolidated version of Regulation (EU) 2023/1115
  exists on EUR-Lex (dated 2026-06-30).
  The EUDR_PREFIXES mapping was last verified against version 2025-12-26.
  Annex I (CN code → commodity mapping) may have been amended.
  Please review: https://eur-lex.europa.eu/legal-content/EN/AUTO/?uri=CELEX:02023R1115-20260630
```

To update the mapping after reviewing a new Annex I:

1. Edit the mapping file `eudr_prefixes.json`
2. Update the `consolidated_date` entry to the new version date
3. Re-run the script to regenerate the CSV and metadata

## License

This project extracts publicly available data from EU institutional sources. The EU legal texts and classification data are available under the [EU legal notice](https://eur-lex.europa.eu/content/legal-notice/legal-notice.html).
