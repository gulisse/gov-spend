#!/usr/bin/env python3
"""
classify_suppliers.py
Adds two fields to distinct_suppliers_clean.csv:
  type           — Gov Agency | Department | Redacted | Business |
                   Personal Address | Persons Name | Nan
  business_type  — for Business: keyword-derived sector (free-text baseline)
                   for Gov Agency / Department: the agency/department identity
                   blank for Redacted / Personal Address / Persons Name / Nan
Deterministic first-match-wins rule cascade. Person-name detection is gated
by a first+surname gazetteer to keep precision up.
"""
import csv, re, sys
from functools import lru_cache
from names_dataset import NameDataset

SRC = "/mnt/user-data/uploads/distinct_suppliers_clean.csv"
OUT = "/home/claude/distinct_suppliers_classified.csv"

ND = NameDataset()

# ── token sets ───────────────────────────────────────────
CORP_SUFFIX = re.compile(r"\b(ltd|limited|plc|llp|inc|incorporated|cic|co|company|holdings|group)\b", re.I)
LAW_WORD    = re.compile(r"\b(chambers|solicitor|solicitors|barrister|barristers|legal|law|advocates|attorneys)\b", re.I)
STREET      = re.compile(r"\b(road|rd|street|st|lane|ln|avenue|ave|close|drive|dr|way|court|ct|grove|place|pl|walk|row|hill|gardens|gdns|crescent|cres|terrace|terr|square|sq|park|mews|parade|broadway|embankment|wharf|quay|rise|vale|green|common|gate|yard|estate|villas?|cottages?|buildings?|chase|approach|hill)\b", re.I)
HOUSE_NUM   = re.compile(r"^\d+[A-Za-z]?(\s*[-/]\s*\d+[A-Za-z]?)?\s+\S")
TITLE       = re.compile(r"^(mr|mrs|ms|miss|mx|dr|prof|professor|sir|dame|rev|reverend|fr|father|lord|lady|capt|col|major)\b\.?\s+\S", re.I)

ORG_KW = re.compile(r"\b(school|academy|college|university|nursery|preschool|childcare|care|nursing|residential|home|surgery|clinic|medical|dental|hospital|pharmacy|nhs|construction|build|builders|contractor|roofing|plumbing|electrical|heating|glazing|scaffolding|joinery|property|estate|lettings|housing|facilities|recruitment|staffing|resourcing|consult|consultancy|accountant|audit|software|technology|digital|systems|telecom|communications|catering|food|restaurant|cafe|bakery|cleaning|janitorial|security|transport|taxi|coach|logistics|haulage|courier|motor|garage|vehicle|fleet|energy|utilities|waste|recycling|refuse|landscape|grounds|gardening|tree|print|printing|signage|stationery|media|marketing|advertising|design|publishing|training|coaching|tuition|tutor|charity|foundation|trust|community|association|church|mosque|temple|parish|diocese|insurance|bank|finance|financial|leasing|architect|surveyor|engineering|hotel|lodge|accommodation|furniture|equipment|supplies|supply|products|wholesale|interpreting|translation|therapy|therapeutic|counselling|psychology|fostering|adoption|art|arts|music|theatre|dance|gallery|museum|sport|fitness|leisure|gym|services|service|solutions|enterprises?|partnership|partners|associates|centre|center|project|network|union|club|society|institute|council|authority|agency|board|commission)\b", re.I)

# pure redaction markers (whole-meaning redactions, not real entities)
REDACT_PURE = re.compile(r"(personal data|personal information|sensitive data|sensitive supplier|redacted\s*-\s*personal|^redacted$|xxxx?redacted|name\s+redacted|supplier\s+redacted|data\s+redacted|^redacted\s+sensitive)", re.I)

NAN = {"nan", "none", "null", "n/a", "na", ""}

# ── government detectors ─────────────────────────────────
DEPARTMENT = re.compile(r"(\bdepartment (for|of)\b|\bministry of\b|\bdept (for|of)\b|mhclg|dluhc|cabinet office|home office|hm treasury|foreign[, ].*commonwealth|department for education|defra|department for transport|department for work)", re.I)
GOV_AGENCY = re.compile(r"(\bcouncil\b|\bborough\b|county council|city council|hmrc|hm revenue|inland revenue|revenue & customs|revenue and customs|greater london authority|\bgla\b|transport for london|\btfl\b|transport trading|\bdvla\b|\bdvsa\b|\bdwp\b|jobcentre|\bnhs\b|clinical commissioning|\bccg\b|teachers? pension|pension agency|pensions agency|environment agency|valuation office|land registry|companies house|ofsted|\bpolice\b|fire authority|fire and rescue|combined authority|crown court|magistrates|courts? (and|&) tribunals|hmcts|student loans|driver and vehicle|highways england|national highways|\bhsbc government\b|local government)", re.I)

# ── business sector keyword map (ordered, specific first) ─
SECTOR = [
    (re.compile(r"\b(chambers|solicitor|solicitors|barrister|barristers|legal services|\blaw\b|advocates|attorneys|llp)\b", re.I), "Legal services"),
    (re.compile(r"\b(nursing home|care home|residential care|care centre|care center|rest home|domiciliary)\b", re.I), "Care home / residential care"),
    (re.compile(r"\b(childrens home|children's home|fostering|foster care|adoption|looked after)\b", re.I), "Children's residential & fostering"),
    (re.compile(r"\b(care|caring|supported living|support services|homecare|home care|carers)\b", re.I), "Care & support services"),
    (re.compile(r"\b(nursery|preschool|pre-school|childcare|child care|day care|early years|childmind|playgroup|kids club|after school)\b", re.I), "Nursery / childcare"),
    (re.compile(r"\b(university|higher education)\b", re.I), "Higher education"),
    (re.compile(r"\b(school|academy|primary|secondary|sixth form|college|education|grammar)\b", re.I), "School / education"),
    (re.compile(r"\b(surgery|\bgp\b|medical centre|medical practice|health centre|practice)\b", re.I), "GP / medical practice"),
    (re.compile(r"\b(dental|dentist|orthodont)\b", re.I), "Dental practice"),
    (re.compile(r"\b(pharmacy|chemist|pharmaceutical)\b", re.I), "Pharmacy"),
    (re.compile(r"\b(hospital|nhs trust|healthcare|health care|clinic|clinical)\b", re.I), "Healthcare provider"),
    (re.compile(r"\b(therapy|therapeutic|counselling|counseling|psychology|psychotherapy|psychological)\b", re.I), "Therapy & counselling"),
    (re.compile(r"\b(roofing|plumbing|electrical|electrician|heating|glazing|glass|scaffolding|decorating|plastering|joinery|carpentry|fencing|flooring|tiling|brickwork|guttering)\b", re.I), "Building trades"),
    (re.compile(r"\b(construction|builders|building|contractor|contractors|groundwork|civil engineering|demolition|refurbishment)\b", re.I), "Construction & contracting"),
    (re.compile(r"\b(facilities management|facilities|property management|property|estate|estates|lettings|landlord|surveyor|surveying|valuation)\b", re.I), "Property & facilities"),
    (re.compile(r"\b(housing association|housing|homes|tenant|registered provider)\b", re.I), "Housing provider"),
    (re.compile(r"\b(recruitment|staffing|resourcing|employment agency|personnel|\bscm\b|temp|temporary|locum|payroll)\b", re.I), "Recruitment & staffing"),
    (re.compile(r"\b(accountant|accountancy|audit|auditor|bookkeep|tax)\b", re.I), "Accountancy & audit"),
    (re.compile(r"\b(consult|consultancy|consulting|advisory|advisor)\b", re.I), "Consultancy"),
    (re.compile(r"\b(software|technology|technologies|digital|systems|\bit\b|cyber|data|computing|computers?)\b", re.I), "IT & technology"),
    (re.compile(r"\b(telecom|telecoms|communications|mobile|broadband|networks?)\b", re.I), "Telecoms"),
    (re.compile(r"\b(catering|food|restaurant|cafe|bakery|kitchen|meals)\b", re.I), "Catering & food"),
    (re.compile(r"\b(cleaning|janitorial|hygiene|laundry)\b", re.I), "Cleaning services"),
    (re.compile(r"\b(security|guarding|surveillance|cctv)\b", re.I), "Security services"),
    (re.compile(r"\b(taxi|private hire|cab|cabs|coach|coaches|minibus|chauffeur)\b", re.I), "Passenger transport"),
    (re.compile(r"\b(transport|logistics|haulage|courier|freight|removals|distribution)\b", re.I), "Transport & logistics"),
    (re.compile(r"\b(motor|garage|vehicle|vehicles|fleet|\bcar\b|cars|automotive|tyres?)\b", re.I), "Motor & vehicle services"),
    (re.compile(r"\b(energy|electric|electricity|gas|utilities|utility|water|power|solar)\b", re.I), "Utilities & energy"),
    (re.compile(r"\b(waste|recycling|refuse|skip|skips|disposal)\b", re.I), "Waste & recycling"),
    (re.compile(r"\b(landscape|landscaping|grounds|gardening|garden|tree|trees|horticult|arboricult|grass)\b", re.I), "Grounds & landscaping"),
    (re.compile(r"\b(print|printing|signage|signs|stationery)\b", re.I), "Printing & signage"),
    (re.compile(r"\b(media|marketing|advertising|\bpr\b|publishing|publications?|creative|film|video|photography|photographic)\b", re.I), "Media & marketing"),
    (re.compile(r"\b(design|designers?|graphic)\b", re.I), "Design"),
    (re.compile(r"\b(training|coaching|tuition|tutor|tutoring|tutors)\b", re.I), "Training & tuition"),
    (re.compile(r"\b(interpreting|interpreter|translation|translator|language|languages)\b", re.I), "Interpreting & translation"),
    (re.compile(r"\b(charity|foundation|charitable|voluntary|mencap|mind|samaritans|trust|community|association|society|outreach|aid)\b", re.I), "Charity / voluntary"),
    (re.compile(r"\b(church|mosque|temple|synagogue|parish|diocese|gurdwara|ministries)\b", re.I), "Religious organisation"),
    (re.compile(r"\b(insurance|insurers?|underwrit)\b", re.I), "Insurance"),
    (re.compile(r"\b(bank|finance|financial|credit|leasing|capital|investments?)\b", re.I), "Financial services"),
    (re.compile(r"\b(architect|architects|architecture)\b", re.I), "Architecture"),
    (re.compile(r"\b(engineering|engineers?|mechanical)\b", re.I), "Engineering"),
    (re.compile(r"\b(hotel|lodge|guest house|accommodation|\bb&b\b|hostel|inn)\b", re.I), "Accommodation / hotel"),
    (re.compile(r"\b(art|arts|music|musical|theatre|theater|dance|gallery|museum|cultural|orchestra|band|choir)\b", re.I), "Arts & culture"),
    (re.compile(r"\b(sport|sports|fitness|leisure|\bgym\b|swimming|football|athletics)\b", re.I), "Sport & leisure"),
    (re.compile(r"\b(furniture|equipment|supplies|supply|products|wholesale|merchants?|trade|trading)\b", re.I), "Goods & supplies"),
    (re.compile(r"\b(mediation|advocacy|advice|citizens|welfare)\b", re.I), "Advice & advocacy"),
]

# Broad set of name-relevant locales (reflects a diverse London population)
RELEVANT = {
    "United Kingdom", "United States", "Ireland", "Canada", "Australia",
    "Nigeria", "Ghana", "Kenya", "South Africa", "Somalia",
    "India", "Pakistan", "Bangladesh", "Sri Lanka",
    "Jamaica", "Trinidad and Tobago",
    "Poland", "Romania", "Portugal", "Spain", "France", "Italy",
    "Germany", "Greece", "Lithuania", "Bulgaria",
    "China", "Hong Kong", "Philippines", "Malaysia", "Singapore",
    "Turkey", "Iran, Islamic Republic of", "Iraq", "Afghanistan",
    "Brazil", "Colombia", "Mexico",
}
RANK_MAX = 45000  # ignore ultra-rare tail matches

def _relevant_rank(field) -> bool:
    """field is the first_name/last_name dict from NameDataset, or None."""
    if not field:
        return False
    rank = field.get("rank") or {}
    return any(
        (rank.get(c) is not None and rank.get(c) <= RANK_MAX) for c in RELEVANT
    )

@lru_cache(maxsize=None)
def good_first(tok: str) -> bool:
    try:
        return _relevant_rank(ND.search(tok)["first_name"])
    except Exception:
        return False

@lru_cache(maxsize=None)
def good_last(tok: str) -> bool:
    try:
        return _relevant_rank(ND.search(tok)["last_name"])
    except Exception:
        return False

ALPHA_TOK = re.compile(r"^[A-Za-z][A-Za-z'\-\.]*$")

# common nouns that double as gazetteer surnames/first names but signal a
# place / venue / business rather than a human
NONPERSON_WORD = re.compile(r"\b(house|farm|park|manor|inn|lodge|cottage|cottages|villa|villas|grange|hall|court|lodge|cuisine|chemist|profit|store|kiosk|stores|market|garden|gardens|bridge|hill|green|wood|woods|field|fields|works|mill|barn|stable|stables|cuisine|kitchen|deli|grill|bar|pub|tavern|cafe|bistro|palace|castle|tower|gate|cross|point|view|place|lane|road|street|way|close|drive|terrace|square|nights?|pleasure|outdoors|connex|cherub|data)\b", re.I)

def _is_abbrev(tok: str) -> bool:
    """All-caps short token (TR, GB, SP, AUT, UK) — signals a brand/acronym."""
    return tok.isupper() and len(tok) <= 4

def looks_like_person(name: str) -> bool:
    if TITLE.search(name):
        return True
    # handle "A & B" two-name pattern → test each side
    parts = re.split(r"\s*&\s*|\s+and\s+", name)
    if len(parts) == 2 and all(_single_person(p) for p in parts):
        return True
    return _single_person(name)

def _single_person(name: str) -> bool:
    if CORP_SUFFIX.search(name) or ORG_KW.search(name) or LAW_WORD.search(name):
        return False
    if NONPERSON_WORD.search(name):
        return False
    if any(ch.isdigit() for ch in name):
        return False
    toks = name.split()
    if not (2 <= len(toks) <= 4):
        return False
    if not all(ALPHA_TOK.match(t) for t in toks):
        return False
    if any(_is_abbrev(t) for t in toks):           # reject GB / TR / AUT / SP …
        return False
    # recognised given name in first OR second position, recognised surname last
    first_ok = good_first(toks[0]) or (len(toks) >= 2 and good_first(toks[1]))
    last_ok = good_last(toks[-1])
    return first_ok and last_ok

def sector_of(name: str) -> str:
    for rx, label in SECTOR:
        if rx.search(name):
            return label
    return "Other / unclassified"

def classify(name: str):
    raw = name
    n = name.strip()
    low = n.lower()

    if low in NAN:
        return "Nan", ""

    if REDACT_PURE.search(low) or "redact" in low:
        # if a real entity survives (suffix/sector), treat as Business
        if CORP_SUFFIX.search(n) or ORG_KW.search(n) or LAW_WORD.search(n):
            return "Business", sector_of(n)
        return "Redacted", ""

    # Government — Department (ministerial) before Agency
    if DEPARTMENT.search(low):
        return "Department", n
    if GOV_AGENCY.search(low):
        return "Gov Agency", n

    # Personal Address — house number + street, no law word, no corp suffix
    if HOUSE_NUM.match(n) and STREET.search(n) and not CORP_SUFFIX.search(n):
        if LAW_WORD.search(n):
            return "Business", "Legal services"   # e.g. "1 Kings Bench Walk Chambers"
        return "Personal Address", ""

    # Persons Name
    if looks_like_person(n):
        return "Persons Name", ""

    # Default — Business
    return "Business", sector_of(n)


def main():
    rows_out = []
    from collections import Counter
    tcount = Counter(); btcount = Counter()
    with open(SRC, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        fields = r.fieldnames + ["type", "business_type"]
        for row in r:
            name = row["supplier_clean"]
            t, bt = classify(name if name is not None else "")
            row["type"] = t
            row["business_type"] = bt
            rows_out.append(row)
            tcount[t] += 1
            if t == "Business":
                btcount[bt] += 1

    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)

    print(f"Rows written: {len(rows_out):,}  →  {OUT}\n")
    print("=== type distribution ===")
    for k, v in tcount.most_common():
        print(f"  {k:18s} {v:>7,}  ({v/len(rows_out)*100:4.1f}%)")
    print(f"\n=== business_type distribution ({sum(btcount.values()):,} Business rows) ===")
    for k, v in btcount.most_common():
        print(f"  {k:34s} {v:>7,}")

if __name__ == "__main__":
    main()
