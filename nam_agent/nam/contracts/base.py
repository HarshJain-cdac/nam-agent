"""What each NAM contract takes and returns, whatever backend answers it.

Every result carries the same four fields first:

    found      False means the backend was asked and holds nothing: "no data"
    source     which system answered and from where ({"system", "endpoint"})
    authority  how far the result binds (AUTHORITIES)
    warnings   what the caller must know before relying on it

A backend that could not be asked raises Unavailable instead of returning
found: False. The tool layer turns that into kg_unavailable (502, retryable),
so "the KG is down" never reads as "the KG has no precedent".

The schemas are built here in code, from shared parts, and checked by
nam_agent/nam/schema.py like the API's own schemas.
"""

from __future__ import annotations

from dataclasses import dataclass

from nam_agent.nam import schema

READ, WRITE, IRREVERSIBLE = "read", "write", "irreversible"
EFFECTS = (READ, WRITE, IRREVERSIBLE)

# precedent         a past tender in the KG: evidence an officer weighs, never a decision
# system_of_record  SAP or Aarohan itself (future connectors)
AUTHORITIES = ("precedent", "system_of_record")
SYSTEMS = ("kg", "sap", "aarohan")


class Unavailable(Exception):
    """The backend could not be asked (down, timed out, garbled reply).
    Transient: a read is retried."""


class Rejected(Exception):
    """The backend refused the arguments (a 4xx other than 404). Not retried:
    the same call would be refused again."""


_TEXT = {"type": "string", "minLength": 2, "maxLength": 200}
_OPT = {"type": ["string", "null"], "maxLength": 300}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 10}
_CASE_KEY = {"type": "string", "minLength": 3, "maxLength": 60,
             "pattern": r"^[A-Za-z0-9][A-Za-z0-9_./-]*$"}
_STRINGS = {"type": "array", "maxItems": 50, "items": {"type": "string", "maxLength": 300}}

CITE = {"type": "object", "additionalProperties": False, "required": ["case_key"],
        "properties": {"case_key": {"type": "string", "maxLength": 60}, "tender_no": _OPT,
                       "job_name": _OPT, "department": _OPT}}
_STATS = {"type": ["object", "null"], "additionalProperties": False,
          "required": ["n", "min", "median", "max"],
          "properties": {"n": {"type": "integer", "minimum": 1},
                         # exact decimal strings, as the KG computes them
                         "min": {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"},
                         "median": {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"},
                         "max": {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"}}}
_YEARS = {"type": "array", "maxItems": 60, "items": {"type": "integer", "minimum": 1950,
                                                     "maximum": 2100}}
_SOURCES = {"type": "array", "maxItems": 50, "items": {"type": "string", "maxLength": 120}}
# an exact decimal as the tender recorded it, or null when it recorded none
_DEC = {"type": ["string", "null"], "pattern": r"^-?\d+(\.\d+)?$"}

# what tender_record copies, and how much of it the KG is asked for
RECORD_FIELDS = ("tender_no", "job_name", "department", "work_category", "estimated_value",
                 "emd_amount", "bid_type", "bid_opening_date", "bid_submission_end_date",
                 "pre_bid_meeting_date", "pr_no")
# the fixed NIT rows whose text is standing NRL policy (the KG's /api/kg/boilerplate)
BOILERPLATE_SECTIONS = ("mse", "ppl_c", "land_boundary", "holiday_listing", "ptr_relaxation",
                        "general_instructions")
RECORD_ITEMS = 2000
RECORD_CLAUSES = 200


def _input(props: dict, required: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False, "required": required,
            "properties": props}


def _output(props: dict, required: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["found", "source", "authority", "warnings", *required],
        "properties": {
            "found": {"type": "boolean"},
            "source": {"type": "object", "additionalProperties": False,
                       "required": ["system", "endpoint"],
                       "properties": {"system": {"enum": list(SYSTEMS)},
                                      "endpoint": {"type": "string", "maxLength": 200}}},
            "authority": {"enum": list(AUTHORITIES)},
            "warnings": {"type": "array", "maxItems": 10,
                         "items": {"type": "string", "maxLength": 300}},
            **props,
        },
        "$defs": {"cite": CITE},
    }


def _list(item_props: dict, required: list[str], cap: int = 100) -> dict:
    return {"type": "array", "maxItems": cap,
            "items": {"type": "object", "additionalProperties": False, "required": required,
                      "properties": item_props}}


@dataclass(frozen=True)
class Contract:
    name: str
    description: str                 # what a model reads, when a model may call it
    effect: str                      # READ | WRITE | IRREVERSIBLE
    input_schema: dict
    output_schema: dict


CONTRACTS: dict[str, Contract] = {c.name: c for c in (
    Contract(
        "tenders",
        "Past NRL tenders for what is being procured. mode 'lexical' lists every tender whose "
        "job name or line items contain the words; mode 'similar' returns the closest few by "
        "meaning, with a similarity and a note when the match is weak.",
        READ,
        _input({"query": _TEXT, "mode": {"enum": ["lexical", "similar"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"]),
        _output({
            "total": {"type": "integer", "minimum": 0},
            "best_similarity": {"type": ["number", "null"], "minimum": -1, "maximum": 1},
            "weak": _OPT,
            "tenders": _list({"cite": {"$ref": "#/$defs/cite"},
                              "matching_items": _STRINGS,
                              "matching_item_count": {"type": "integer", "minimum": 0}},
                             ["cite", "matching_items", "matching_item_count"]),
        }, ["total", "best_similarity", "weak", "tenders"])),
    Contract(
        "resolve_reference",
        "Which past tender a reference names (a PUR case id or a tender number). An ambiguous "
        "reference returns every candidate; never pick one for the officer.",
        READ,
        _input({"ref": {"type": "string", "minLength": 3, "maxLength": 120}}, ["ref"]),
        _output({
            "tier": {"enum": ["case_key", "tender_no", "core", None]},
            "ambiguous": {"type": "boolean"},
            "matches": _list({"cite": {"$ref": "#/$defs/cite"},
                              "match": {"enum": ["case_key", "tender_no", "core"]}},
                             ["cite", "match"], 10),
        }, ["tier", "ambiguous", "matches"])),
    Contract(
        "price_history",
        "Historical NRL rates for an item: one row per item, unit and price level (estimate, "
        "quote, award, invoice are never merged), with the tenders and years observed.",
        READ,
        _input({"requirement": _TEXT, "limit": _LIMIT}, ["requirement"]),
        _output({
            "weak": _OPT,
            "rows": _list({"item": {"type": "string", "maxLength": 300}, "uom": _OPT,
                           "price_level": {"type": "string", "maxLength": 40},
                           "unit_price": _STATS, "estimated_rate": _STATS, "totals": _STATS,
                           "observations": {"type": "integer", "minimum": 0},
                           "matched": {"type": "boolean"}, "years": _YEARS,
                           "spans_years": {"type": "boolean"}, "sources": _SOURCES},
                          ["item", "uom", "price_level", "unit_price", "estimated_rate",
                           "totals", "observations", "matched", "years", "spans_years",
                           "sources"], 200),
        }, ["weak", "rows"])),
    Contract(
        "quantity_history",
        "The quantities NRL ordered of an item before, per unit, with the tenders and years.",
        READ,
        _input({"requirement": _TEXT, "limit": _LIMIT}, ["requirement"]),
        _output({
            "rows": _list({"item": {"type": "string", "maxLength": 300}, "uom": _OPT,
                           "typical_qty": _STATS, "matched": {"type": "boolean"},
                           "years": _YEARS, "sources": _SOURCES},
                          ["item", "uom", "typical_qty", "matched", "years", "sources"], 200),
        }, ["rows"])),
    Contract(
        "clauses",
        "Verbatim PQC, SCC or technical-specification clauses from past NRL tenders, each with "
        "the tender it came from. Quote them; never compose an NRL clause.",
        READ,
        _input({"requirement": _TEXT, "kind": {"enum": ["pqc", "scc", "tech_spec"]},
                "limit": _LIMIT}, ["requirement", "kind"]),
        _output({
            "clauses": _list({"kind": {"enum": ["pqc", "scc", "tech_spec"]},
                              "text": {"type": "string", "maxLength": 20000},
                              "cite": {"$ref": "#/$defs/cite"}}, ["kind", "text", "cite"], 60),
        }, ["clauses"])),
    Contract(
        "boilerplate",
        "The standing clause text NRL issued for one fixed NIT row (MSE preference, PP-LC, "
        "land-boundary bidders, holiday listing, PTR relaxation, general instructions), "
        "verbatim, from the tenders that carry it. A candidate wording with its tender, never "
        "a rule in force: the clause library of record is not connected.",
        READ,
        _input({"section": {"enum": list(BOILERPLATE_SECTIONS)}, "limit": _LIMIT},
               ["section"]),
        _output({
            "section": {"enum": list(BOILERPLATE_SECTIONS)},
            "clauses": _list({"text": {"type": "string", "maxLength": 20000},
                              "cite": {"$ref": "#/$defs/cite"}}, ["text", "cite"], 60),
        }, ["section", "clauses"])),
    Contract(
        "documents",
        "The files a past tender holds (type, name, size), and which one is its NIT.",
        READ,
        _input({"case_key": _CASE_KEY,
                "doc_type": {"type": "string", "maxLength": 60}}, ["case_key"]),
        _output({
            "best_nit": _OPT,
            "documents": _list({"document_type": _OPT, "filename": _OPT,
                                "extension": {"type": "string", "maxLength": 12},
                                "byte_size": {"type": ["integer", "null"], "minimum": 0},
                                "readable": {"type": "boolean"}},
                               ["document_type", "filename", "extension", "byte_size",
                                "readable"], 60),
        }, ["best_nit", "documents"])),
    Contract(
        "enrichment",
        "The recorded fields of one past tender (department, category, estimated value, EMD, "
        "bid type, PR number, awardees, what it holds), to prefill a need from precedent.",
        READ,
        _input({"case_key": _CASE_KEY}, ["case_key"]),
        _output({
            "cite": {"type": ["object", "null"], "additionalProperties": False,
                     "required": ["case_key"], "properties": CITE["properties"]},
            "fields": {"type": "object", "additionalProperties": False,
                       "properties": {k: _OPT for k in (
                           "work_category", "estimated_value", "emd_amount", "bid_type",
                           "bid_opening_date", "bid_submission_end_date",
                           "pre_bid_meeting_date", "pr_no")}},
            "awarded_to": _STRINGS,
            "counts": {"type": "object", "additionalProperties": {"type": "integer",
                                                                  "minimum": 0}},
        }, ["cite", "fields", "awarded_to", "counts"])),
    Contract(
        "tender_record",
        "The exact record of one past tender the officer named: its fields, every BOQ line "
        "with the rate it recorded, and its PQC, SCC and specification clauses verbatim. For "
        "create by copy (F2); the rates are historical, never the rate to propose (R7).",
        READ,
        _input({"case_key": _CASE_KEY}, ["case_key"]),
        _output({
            "cite": {"type": ["object", "null"], "additionalProperties": False,
                     "required": ["case_key"], "properties": CITE["properties"]},
            "fields": {"type": "object", "additionalProperties": False,
                       "properties": {k: _OPT for k in RECORD_FIELDS}},
            "boq": _list({"item": {"type": "string", "maxLength": 300},
                          "material_code": _OPT, "uom": _OPT, "price_level": _OPT,
                          **{k: _DEC for k in ("qty", "unit_price", "estimated_rate",
                                               "total_price")}},
                         ["item", "material_code", "uom", "price_level", "qty", "unit_price",
                          "estimated_rate", "total_price"], RECORD_ITEMS),
            "clauses": {"type": "object", "additionalProperties": False,
                        "required": ["pqc", "scc", "tech_spec"],
                        "properties": {k: _list({"text": {"type": "string",
                                                          "maxLength": 20000},
                                                 "summary": {"type": "string",
                                                             "maxLength": 300}},
                                                ["text", "summary"], RECORD_CLAUSES)
                                       for k in ("pqc", "scc", "tech_spec")}},
            "sor_references": _STRINGS,
            "awarded_to": _STRINGS,
            # False when a list may have been cut at the limit asked for: the
            # copy is then not the whole tender, and says so
            "complete": {"type": "boolean"},
        }, ["cite", "fields", "boq", "clauses", "sor_references", "awarded_to", "complete"])),
    Contract(
        "suppliers",
        "Who NRL awarded past tenders for this item to, with the tenders. Precedent only: a "
        "past awardee is not an approved or current vendor.",
        READ,
        _input({"requirement": _TEXT,
                "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["requirement"]),
        _output({
            "suppliers": _list({"name": {"type": "string", "maxLength": 300},
                                "awards": {"type": "integer", "minimum": 1},
                                "tenders": _SOURCES}, ["name", "awards", "tenders"]),
            "tenders_checked": {"type": "integer", "minimum": 0},
        }, ["suppliers", "tenders_checked"])),
)}

for _c in CONTRACTS.values():
    schema.check_schema(_c.input_schema, f"{_c.name}.input")
    schema.check_schema(_c.output_schema, f"{_c.name}.output")
