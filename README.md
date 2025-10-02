# MDTO XML → RDF converter

This repository contains a script to convert MDTO XML (as defined in `xml/MDTO-XML1.0.1.xsd`) into MDTO RDF instances that validate against the SHACL shapes in `rdf/mdto-rdf-1.0-shacl.ttl`.

## Layout
- `scripts/xml2rdf.py` – converter CLI.
- `rdf/mdto-rdf-1.0.ttl` – ontology.
- `rdf/mdto-rdf-1.0-shacl.ttl` – SHACL shapes.
- `xml/` – XSD and example XMLs.

## Installation
It is recommended to use a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage
Convert all example XML files into a single Turtle file and validate against SHACL:

```bash
python scripts/xml2rdf.py \
  --xml-dir xml/example \
  --out output/mdto.ttl \
  --base http://example.com/mdto/ \
  --validate \
  --shapes rdf/mdto-rdf-1.0-shacl.ttl
```

- `--xml-dir` can be a directory with one or more `.xml` files.
- `--base` sets the base URI for generated resources. Use your own domain when integrating.
- `--validate` runs SHACL validation using `pyshACL` and writes a report next to the output file.

## URI and reference handling
- The script generates deterministic URIs for all resources using a combination of slugified values and a short hash suffix.
- Cross-file references are resolved using identification data when possible:
  - Where SHACL requires a specific class (e.g., `mdto:isOnderdeelVan` → `mdto:Informatieobject`, `mdto:heeftRepresentatie` → `mdto:Bestand`), the script links to actual resources if resolvable.
  - If a reference cannot be resolved to the required class, the script skips emitting that triple to avoid SHACL violations.
  - For properties that allow `mdto:VerwijzingGegevens`, the script emits a dedicated `mdto:VerwijzingGegevens` node and attaches its `verwijzingNaam` and optional identification.

## Notes
- The converter infers correct XML Schema date datatypes (`xsd:date`, `xsd:gYearMonth`, `xsd:gYear`, `xsd:dateTime`) to satisfy SHACL `sh:or` constraints.
- The SHACL provided by Nationaal Archief was incorrect, so it was modified to be correct. See [corrected MDTO RDF](https://github.com/Regionaal-Archief-Zuid-Utrecht/MDTO-RDF).
- URI's are minted using `<identificatieKenmerk>` and `<identificatieBron>`, unless `<identificiatieKenmerk>` is a valid URI, in which case it is used as-is.

## Output
- The generated RDF is written to `output/mdto.ttl` (configurable via `--out`).
- If `--validate` is used, a SHACL report is written to `output/mdto.shacl-report.ttl` and the process exits with a non-zero code on validation failure.
