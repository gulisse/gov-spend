#!/usr/bin/env python3
"""
clean_suppliers.py — Clean and normalise supplier names in the spend dataset.

Usage:


  # Filter to 2024 onwards  
  python clean_suppliers_V4.py --min-year 2024






  # Full dataset, default output
  python clean_suppliers_V3.py


  # Custom output file
  python clean_suppliers.py --min-year 2024 --output spend_2024_clean.csv

Lookup files (expected alongside this script):
  normalized_spend_raw.csv   — source data (never modified)
  supplier_overrides.csv     — manual exact-match overrides (supplier → supplier_clean)
  candidate_acronyms.csv     — curated acronym list (token, keep_caps Y/blank)
"""
import os
import re
import sys
import time
import argparse
import pandas as pd
import ftfy 

# ── CLI arguments ─────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(
    description="Clean and normalise supplier names in the spend dataset."
)
parser.add_argument(
    "--min-year",
    type=int,
    default=None,
    metavar="YEAR",
    help="Only include rows where year >= YEAR (e.g. --min-year 2024)."
)
parser.add_argument(
    "--output",
    type=str,
    default=None,
    metavar="FILENAME",
    help="Output filename (default: normalized_spend.csv). "
         "Relative paths are resolved from the script directory."
)
args = parser.parse_args()

# ── File paths (always relative to this script's directory) ───────────────────
SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
INPUT_RAW       = os.path.join(SCRIPT_DIR, "normalized_spend_raw.csv")
OVERRIDES_FILE  = os.path.join(SCRIPT_DIR, "supplier_overrides.csv")
ACRONYMS_FILE   = os.path.join(SCRIPT_DIR, "candidate_acronyms.csv")
AUDIT_FILE      = os.path.join(SCRIPT_DIR, "supplier_changes_audit.csv")

if args.output:
    OUTPUT_FILE = (
        args.output if os.path.isabs(args.output)
        else os.path.join(SCRIPT_DIR, args.output)
    )
else:
    OUTPUT_FILE = os.path.join(SCRIPT_DIR, "normalized_spend.csv")


HEADERS = [
    "borough", "payment_type", "date", "year", "amount", "vat",
    "supplier_original", "department", "expense_type", "service_area",
    "supplier_category", "txn_id", "source_file", "blank_taxonomy",
    "batch_start_date",
]

# ── Step 0: validate input exists ─────────────────────────────────────────────
if not os.path.exists(INPUT_RAW):
    print(
        f"ERROR: 'normalized_spend_raw.csv' not found in:\n  {SCRIPT_DIR}\n"
        f"Please ensure your source data is named 'normalized_spend_raw.csv'."
    )
    sys.exit(1)

# ── Load overrides (applied first, case-insensitive) ─────────────────────────
OVERRIDES_MAP = {}   # lowercase supplier_original → supplier_clean value
if os.path.exists(OVERRIDES_FILE):
    overrides_df = pd.read_csv(OVERRIDES_FILE, dtype=str, encoding="latin-1").fillna("")
    # Accept either column naming convention
    if {"supplier_raw", "supplier_clean"}.issubset(overrides_df.columns):
        raw_col, clean_col = "supplier_raw", "supplier_clean"
    elif {"supplier", "supplier_clean"}.issubset(overrides_df.columns):
        raw_col, clean_col = "supplier", "supplier_clean"
    else:
        raw_col, clean_col = None, None
        print(f"  WARNING: '{os.path.basename(OVERRIDES_FILE)}' needs columns "
              f"'supplier'/'supplier_raw' and 'supplier_clean' — skipping overrides.")

    if raw_col:
        OVERRIDES_MAP = {
            k.strip().lower(): v.strip()
            for k, v in zip(overrides_df[raw_col], overrides_df[clean_col])
            if k.strip() and v.strip()
        }
        print(f"Loaded {len(OVERRIDES_MAP):,} overrides from '{os.path.basename(OVERRIDES_FILE)}'.")
else:
    print(f"No overrides file found at '{os.path.basename(OVERRIDES_FILE)}' — skipping.")

# ── Load acronyms lookup ─────────────────────────────────────────────────────
KEEP_CAPS_SET = set()
if os.path.exists(ACRONYMS_FILE):
    acronyms_df = pd.read_csv(ACRONYMS_FILE, dtype=str).fillna("")
    if "token" in acronyms_df.columns and "keep_caps" in acronyms_df.columns:
        KEEP_CAPS_SET = set(
            acronyms_df.loc[
                acronyms_df["keep_caps"].str.strip().str.upper() == "Y", "token"
            ].str.strip().str.upper()
        )
        print(f"Loaded {len(KEEP_CAPS_SET):,} acronyms to keep capitalised from '{os.path.basename(ACRONYMS_FILE)}'.")
    else:
        print(f"  WARNING: '{os.path.basename(ACRONYMS_FILE)}' must have 'token' and 'keep_caps' columns — skipping.")
else:
    print(f"No acronyms file found at '{os.path.basename(ACRONYMS_FILE)}' — using default init-cap logic.")

# ── Load data ─────────────────────────────────────────────────────────────────
print(f"Reading 'normalized_spend_raw.csv' from {SCRIPT_DIR} ...")
df = pd.read_csv(INPUT_RAW, header=0)
df.columns = HEADERS  # enforce expected headers; original 'supplier' col → 'supplier_original'

# ── Optional year filter ──────────────────────────────────────────────────────
if args.min_year is not None:
    before = len(df)
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df = df[df["year"] >= args.min_year].copy()
    after = len(df)
    print(f"Year filter >= {args.min_year}: {before:,} → {after:,} rows ({before - after:,} excluded).")

# Initialise working column from original supplier
df["supplier"] = df["supplier_original"].astype(str)

# TEMPORARY: dump actual bytes for problem suppliers  AtkinsRéalis
for val in df["supplier"].unique():
    raw = val.encode("utf-8")
    if b"\xc3\x83" in raw or b"\xc3\xa2" in raw or b"\xe2\x80" in raw:
        print(f"  MOJIBAKE: {val!r}")
        print(f"    BYTES:  {raw}")


# ── Cleaning helpers ──────────────────────────────────────────────────────────

# Exact-match map for known card-feed garbled values
EXACT_SUPPLIER_MAP = {
    "AMZNMKTPLACE":       "Amazon Marketplace",
    "AMZN MKTP":          "Amazon Marketplace",
    "AMZNBUSINESS":       "Amazon Business",
    "AMZN Business":      "Amazon Business",
    "AMZ":                "Amazon.co.uk",
    "MSFT":               "Microsoft",
    "WH SMITH":           "WH Smith",
    "WHSMITH":            "WH Smith",
    "JOHNLEWIS":          "John Lewis",
    "JOHN LEWIS":         "John Lewis",
    "WAITROSE":           "Waitrose",
    "WAGAMAMA":           "Wagamama",
    "WAGAMAMAS":          "Wagamama",
    "WASABI":             "Wasabi",
    "TRAVELODGE":         "Travelodge",
    "UK2":                "UK2",
    "UDEMY":              "Udemy",
    "VEOLIA":             "Veolia",
    "ALDI":               "Aldi Supermarket Ltd",
    "ASDA":               "Asda Supermarket Ltd"
}

# Case-insensitive pattern map — anchored patterns for card-feed variants only.
# Checked only if the exact map above didn't match.
PATTERN_SUPPLIER_MAP = [
    (r"^AMZNMKTPLACE",           "Amazon Marketplace"),     
    (r"^AMZNBUSINESS",           "Amazon Business"), 
    (r"^AMZ ",           "Amazon.co.uk"),     
    (r"^AMZN DIGITA",           "Amazon Appstore"),   
    (r"^Amzn Mktp",           "Amazon Marketplace"),   
    (r"^AMZNB2BPRIME",           "Amazon Business Prime"),   
    (r"^AMZNBUSINES",           "Amazon Business"), 
    (r"^AMZNMKTPLAC",           "Amazon Marketplace"),     
    (r"^AMZNBUSINESS",           "Amazon Business"), 
    (r"^WWW AMAZON",           "Amazon.co.uk"), 
    (r"^Amazon Web Services",           "Amazon Web Services"), 
    (r"^Amazon EU SARL",           "Amazon EU SARL"), 
    (r"^Amazon Payments UK Limited",           "Amazon Payments UK Limited"),  
    (r"^AMZN DIGITAl",           "Amazon Appstore"),
    (r"^Amazon\*",           "Amazon.co.uk"),   
    (r"^Amazon Uk\*",      "Amazon.co.uk"),       # Amazon.co.uk, 
    (r"^Amazon\.co.uk",      "Amazon.co.uk"),       # Amazon.co.uk, 
    (r"^Amazon\.com",      "Amazon.com"),       # Amazon.com
    (r"^Amazon Prim",      "Amazon Prime"),   
    (r"^Amazon Music ",      "Amazon Music"),  
    (r"^Amazon",           "Amazon.co.uk"),    # Amazon.com catch all BEWARE if any suppliers called amazon!!


    (r"^EASYJET\b",      "Easyjet"),
    (r"^EBAY\b",         "Ebay"),
    (r"^MSFT\b",         "Microsoft"),
    (r"^WH\s*SMITH",     "WH Smith"),
    (r"^WASABI",         "Wasabi"),
    (r"^WAGAMAMA",       "Wagamama"),
    (r"JOHNLEWIS",      "John Lewis"),
    (r"WAITROSE",       "Waitrose"),
    (r"^TRAVELODGE",     "Travelodge"),
    (r"^UDEMY",          "Udemy"),
    (r"^Airbnb",         "Airbnb"),
    (r"^Aldi",           "Aldi Supermarket Ltd"),
    (r"^Anglian Water",  "Anglian Water"),
    (r"^Apple Store",    "Apple Store"),
    (r"^B&M",            "B&M"),
    (r"^Banyan Uk",      "Banyan Uk"),
    (r"^Barchester Healthcare",         "Barchester Health care Ltd"),
    (r"^Big Yellow Self Storage",       "Big Yellow Self Storage Company Ltd"),
    (r"^Boots ",             "Boots The Chemist"),
    (r"^BP ",                "BP"),
    (r"^Bright Horizons ",   "Bright Horizons"),
    (r"^British Gas",        "British Gas"),
    (r"^Busy Bees ",         "Busy Bees Nurseries Ltd"),
    (r"^Buzzsprout",         "Buzzsprout"),
    (r"^Caffe Nero",         "Caffe Nero"),
    (r"^Card Factory",       "Card Factory"),

    (r"^Childminder",        "Childminder"),
    (r"^Costa Coffee",       "Costa Coffee"),
    (r"^Costco",             "Costco"),
    (r"^Costcutter",         "Costcutter"),
    (r"^Country Court Care Homes",       "Country Court Care Homes Ltd"),

    (r"^Currys ",         "Currys"),
    (r"^Dropbox",         "Dropbox"),
    (r"^Edf Energy",      "Edf Energy Ltd"),
    (r"^Etsy",            "Etsy.com"),
    (r"^Facebk",          "Facebook Ads"),

    (r"^Five Guys",       "Five Guys"),
    (r"^Flyingtiger",     "Flyingtiger"),
    (r"^Freenow",         "Freenow"),
    (r"^Gails ",          "Gails Bakery"),
    (r"^Google Cloud",    "Google Cloud"),

    (r"^Google Gsuite",   "Google Gsuite"),
    (r"^Google Workspace","Google Workspace"),
    (r"^Grammarly Co",    "Grammarly Co"),
    (r"^Greggs",          "Greggs"),
    (r"^Heroku",          "Heroku"),


    (r"^Holiday Inn",       "Holiday Inn"),
    (r"^Home Bargains",     "Home Bargains"),
    (r"^Honest Burger",         "Honest Burger"),
    (r"^Itsu",          "Itsu"),

    (r"^Anglian Water",       "Anglian Water"),
    (r"^Home Bargains",     "Home Bargains"),
    (r"^Honest Burger",         "Honest Burger"),
    (r"^Itsu",          "Itsu"),
    (r"^BUNS FROM HOME",          "Buns From Home"),

    (r"BURGER KING",             "Burger King"),
    (r"Bubble Pop",             "Bubble Pop"),
    (r"UKVI",             "UK Visas and Immigration"),
    (r"CITIZENS ADVICE",             "Citizens Advice"),
    (r"CITY OF LONDON ACADEMY",             "City of London Academy"),
    (r"^COFFEE STATION",             "Coffee station"),
    (r"^COSTA ",             "COSTA "),
    (r"Creams Café",             "Creams Café"),
    (r"Chidminder ",             "Childminder "),
    (r"Dominos ",             "Dominos Pizza"),
    (r"Dunelm",             "Dunelm"),
    (r"Easyhotel ",             "Easyhotel"),
    (r"Easyjet",             "Easyjet"),
    (r"Escape Hunt ",             "Escape Hunt"),
    (r"Expedia ",             "Expedia"),
    (r"^GWR ",             "Great Western Trains"),
    (r"HALFORDS ",             "Halfords"),
    (r"HOME START",             "Home Start"),
    (r"HOMESTART ",             "Home Start"),
    (r"HUNGRY CATERPILLARS ",             "Hungry Caterpillars"),
    (r"J D SPORTS ",             "JD Sports "),
    (r"JD SPORTS ",             "JD Sports"),
    (r"JD WETHERSPOON" ,             "JD Wetherspoon" ),
    (r"^KFC - ",             "KFC"),
    (r"^KFC ",             "KFC"),
    (r"LASTMINUTECOM ",             "Lastminute.com "),
    (r"^LIDL GB ",             "Lidl"),
    (r"^LIDL U",             "Lidl"),
    (r"Linkedin ",             "Linkedin "),
    (r"M&S SIMPLY ",             "M & S Simply Food"),
    (r"M&S FOOD",             "M & S Simply Food"),
    (r"MAIL BOXES ETC",             "Mail Boxes Etc"),
    (r"MAILBOXES",             "Mail Boxes Etc"),
    (r"MAJESTIC WIN",             "Majestic Wine"),
    (r"^SAINSBURY'S",             "Sainsburys"),
    (r"^SAINSBURYS" ,             "Sainsburys" ),
    (r"MARKS&SPENCER" ,             "M & S Simply Food" ),
    (r"MARKS & SPENCER",             "M & S Simply Food"),
    (r"MARKSANDSPENCER" ,             "M & S Simply Food" ),
    (r"MARKS AND SPENCER",             "M & S Simply Food"),
    (r"METROPOLIS EVENTS ",             "Metropolis Events"),
    (r"^Mfg ",             "Motor Fuel Group Petrol Station"),
    (r"NAME-CHEAP.COM ",             "Name-Cheap.com"),
    (r"NANDOS",             "Nandos"),
    (r"Netflix",             "Netflix"),
    (r"NISA LOCAL ",             "NISA Local"),
    (r"NOVOTEL ",             "Novotel"),
    (r"NPOWER",             "NPower"),
    (r"^O2",             "O2"),
    (r"OCULUS",             "Oculus"),
    (r"ODEON",             "Odean Cinemas"),
    (r"OLE AND STEEN",             "Ole & Steen"),
    (r"OPODO",             "Opodo"),
    (r"PAYBYPHONE",             "PayByPhone"),
    (r"PIZZA EXPRESS",             "Pizza Express"),
    (r"PIZZAEXPRESS",             "Pizza Express"),
    (r"PIZZA HUT",             "Pizza Hut"),
    (r"Post Office",             "Post Office"),
    (r"POUNDSTRETCHER",             "Pound Stretcher"),
    (r"PREMIER INN",             "Premier Inn"),
    (r"PRET A MANGER",             "Pret a manger"),
    (r"PREZZE" ,             "Prezze" ),
    (r"PRIMARK",             "Primark"),
    (r"Prime Video",             "Amazon Prime"),
    (r"RENTOKIL",             "Rentokil"),
    (r"ROBERT DYAS",             "Robert Dias"),
    (r"ROYAL MAIL",             "Royal Mail"),
    (r"Ryanair",             "Ryanair"),
    (r"S40 FOIA",             "Freedom of Information S40"),
    (r"Screwfix",             "Screwfix"),
    (r"Spotify",             "Spotify"),
    (r"TESCO",             "Tesco"),
    (r"THE SALVATION ARMY",             "The Salvation Army"),
    (r"Thomson Reuters",             "Thomson Reuters"),
    (r"TRAVELODG",             "Travelodge"),
    (r"TRAVIS PERKINS",             "Travis Perkins"),
    (r"TSGN",             "Thameslink, Southern and Great Northern railway"),
    (r"UNIQLO",             "Uniqlo"),
    (r"URBAN OUTFITTERS",             "Urban Outfitters"),
    (r"VEOLIA",             "Veolia ES Ltd"),
    (r"VODAFONE",             "Vodaphone Ltd"),
    (r"WELCOME BREAK",             "Welcome Break"),
    (r"^WICKES ",             "Wickes"),
    (r"^Wordpress",             "Wordpress"),
    (r"YMCA ",             "YMCA "),
    (r"ZIPCAR",             "Zipcar Ltd"),
    (r"ZIZZI",             "Zizzi"),
    (r"lastminute",             "lastminute.com"),
    (r"revolut",             "revolut"),
    (r"COOP\b",             "CoOp Supermarket"),

    (r"^Asda Express",        "Asda Supermarket Ltd"),
    (r"^Asda George",         "Asda George Online"),
    (r"^Asda Groceries",      "Asda Supermarket Ltd"),
    (r"^Asda Petrol",         "Asda Petrol"),
    (r"^Asda Superstor",      "Asda Supermarket Ltd"),
    (r"^Asda\b",                "Asda Supermarket Ltd"),
]
# Trading-as / care-of patterns — strip the marker and everything after.
TRADING_AS_RE = re.compile(
    r'\s*('
    r'T/A\b'
    r'|Trading\s+As\b'
    r'|C/O\b'
    r'|(?<=Ltd\s)TA\b'
    r'|(?<=LLP\s)TA\b'
    r').*$',
    re.IGNORECASE
)

"""
some garbled characters can't encode back to CP-1252 once they've been read as UTF-8. 
But ftfy doesn't rely on round-tripping. 
It uses heuristic pattern matching to recognise mojibake sequences directly in the 
Unicode string and fix them, regardless of how many encoding layers went wrong.
"""
def fix_mojibake(text: str) -> str:
    result = ftfy.fix_text(text)
    result = ftfy.fix_text(result)
    # Build mojibake patterns from their byte representations
    patterns = [
        (b"\xc3\xa2\xc2\x80\xc2\x93", b"\xe2\x80\x93"),  # en dash
        (b"\xc3\xa2\xc2\x80\xc2\x94", b"\xe2\x80\x94"),  # em dash
        (b"\xc3\xa2\xc2\x80\xc2\x99", b"\xe2\x80\x99"),  # right single quote
        (b"\xc3\xa2\xc2\x80\xc2\x98", b"\xe2\x80\x98"),  # left single quote
        (b"\xc3\xa2\xc2\x80\xc2\x9c", b"\xe2\x80\x9c"),  # left double quote
        (b"\xc3\xa2\xc2\x80\xc2\x9d", b"\xe2\x80\x9d"),  # right double quote
        (b"\xc3\x83\xc2\xa9", b"\xc3\xa9"),               # é
        (b"\xc3\x83\xc2\x89", b"\xc3\x89"),               # É
    ]
    encoded = result.encode("utf-8")
    for bad, good in patterns:
        encoded = encoded.replace(bad, good)
    return encoded.decode("utf-8")


def apply_cleaning(value: str) -> str:
    """
    Cleaning pipeline — order matters:
      1. Encoding artefacts (â€) and question marks mid-word
      1b. Normalise & with no surrounding spaces → ' & '
      2. Remove commas
      3. www normalisation
      4. www.Amazon → Amazon
      5. Exact-match and pattern-match supplier map
      6. Remove underscores
      7. Remove double quotes
      8. Remove brackets and text inside (including unclosed brackets)
      9. @ handling
     10. Remove ~
     11. Normalise Limited/Ltd variants (including Li, LT anchored)
     12. Strip text after Ltd / LLP
     13. Strip T/A, Trading As, C/O
     14. Collapse whitespace
    """
    cleaned = value

    cleaned = fix_mojibake(cleaned)

    # ── 1. Remove encoding artefacts and mid-word question marks ──────────
    # cleaned = cleaned.replace("â€", "")
    # cleaned = cleaned.replace("â€™", "")
    # cleaned = cleaned.replace("\u00e2\u20ac", "")  # alternate encoding
    # Remove ? only when embedded mid-word (letter?letter)
    cleaned = re.sub(r'(?<=\w)\?(?=\w)', '', cleaned)

    # ── 1b. Normalise & with no surrounding spaces → ' & ' ───────────────
    cleaned = re.sub(r'(?<=\w)&(?=\w)', ' & ', cleaned)

    # ── 2. Remove commas ──────────────────────────────────────────────────
    cleaned = cleaned.replace(",", "")

    # ── 3. www normalisation ──────────────────────────────────────────────
    # All "www" variants → lowercase "www"
    # www followed immediately by a letter → insert dot: wwwAcme → www.Acme
    # www followed by comma → replace comma with dot: www,acme → www.acme
    cleaned = re.sub(r'(?i)\bwww,', 'www.', cleaned)           # www, → www.
    cleaned = re.sub(r'(?i)\bwww(?=[a-z])', 'www.', cleaned)   # wwwX → www.X
    cleaned = re.sub(r'(?i)\bwww\b', 'www', cleaned)           # normalise case

    # ── 4. www.Amazon... → Amazon ─────────────────────────────────────────
    cleaned = re.sub(r'(?i)\bwww\.amazon\b.*', 'Amazon', cleaned)

    # ── 5. Exact-match then pattern-match supplier map ────────────────────
    upper_stripped = cleaned.strip().upper()
    matched = False
    for raw_key, replacement in EXACT_SUPPLIER_MAP.items():
        if upper_stripped == raw_key.upper():
            cleaned = replacement
            matched = True
            break
    if not matched:
        for pattern, replacement in PATTERN_SUPPLIER_MAP:
            if re.search(pattern, cleaned, flags=re.IGNORECASE):
                cleaned = replacement
                break

    # ── 6. Remove underscores ─────────────────────────────────────────────
    cleaned = cleaned.replace("_", "")

    # ── 7. Remove double quotes ───────────────────────────────────────────
    cleaned = cleaned.replace('"', '')

    # ── 8. Remove brackets and text inside ────────────────────────────────
    cleaned = re.sub(r'\(.*?\)', '', cleaned)   # remove (…)
    cleaned = re.sub(r'\[.*?\]', '', cleaned)   # remove […]
    # Unclosed open bracket: remove ( and everything after if no closing )
    cleaned = re.sub(r'\([^)]*$', '', cleaned)
    # Unclosed open square bracket
    cleaned = re.sub(r'\[[^\]]*$', '', cleaned)

    # ── 9. @ handling ─────────────────────────────────────────────────────
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]                   # strip leading @ only
    else:
        cleaned = re.sub(r'@.*', '', cleaned)   # remove @ and everything after

    # ── 10. Remove ~ ─────────────────────────────────────────────────────
    cleaned = cleaned.replace("~", "")

    # ── 10b. Remove dots from L.T.D. and P.L.C. only ────────────────────
    cleaned = re.sub(r'(?i)\bL\.?\s*T\.?\s*D\.?', 'Ltd', cleaned)
    cleaned = re.sub(r'(?i)\bP\.?\s*L\.?\s*C\.?', 'Plc', cleaned)

    # ── 11. Normalise all Limited variants → Ltd ──────────────────────────
    cleaned = re.sub(r'(?i)\bLimited\b', 'Ltd', cleaned)
    cleaned = re.sub(r'(?i)\bLimi\b', 'Ltd', cleaned)
    cleaned = re.sub(r'(?i)\bLim\b', 'Ltd', cleaned)
    cleaned = re.sub(r'(?i)\bLIMTED\b',  'Ltd', cleaned)   # common typo
    cleaned = re.sub(r'(?i)\bLimit\b',   'Ltd', cleaned)   # truncated variant
    cleaned = re.sub(r'(?i)\bLtda\b',    'Ltd', cleaned)
    cleaned = re.sub(r'(?i)\bLTD\b',     'Ltd', cleaned) 
    cleaned = re.sub(r'(?i)\bLLTD\b',     'Ltd', cleaned)

    # LT → Ltd only at end of string after a word character (anchored)
    cleaned = re.sub(r'(?i)(?<=\w)\s+LT$', ' Ltd', cleaned)
    # Li → Ltd only at end of string after a word character (anchored)
    cleaned = re.sub(r'(?i)(?<=\w)\s+Li$', ' Ltd', cleaned)
        # Normalise PLC variants → Plc
    cleaned = re.sub(r'(?i)\bPLC\b', 'Plc', cleaned)

    # ── 12. Strip any text after Ltd or LLP ───────────────────────────────
    # Also strips a trailing full stop: "Acme Ltd." → "Acme Ltd"
    cleaned = re.sub(r'(?i)\b(Ltd)\b\.?.*', r'Ltd', cleaned)
    cleaned = re.sub(r'(?i)\b(LLP)\b\.?.*', r'LLP', cleaned)

    # ── 13. Strip T/A, Trading As, C/O ───────────────────────────────────
    cleaned = TRADING_AS_RE.sub('', cleaned)

    # ── 14. Collapse whitespace ───────────────────────────────────────────
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    return cleaned


def normalise_for_comparison(value: str) -> str:
    """
    Reduce a supplier value to a normalised key used ONLY for matching
    in deduplication rules 10-13.  Never written back to the supplier field.

    Steps:
      - uppercase
      - strip apostrophes
      - strip trailing S from each token  (Alzheimers == Alzheimer's)
      - replace & with AND
      - remove hyphens between word characters  (E-penny == Epenny)
      - collapse whitespace
    """
    v = value.upper()
    v = v.replace("'", "")
    v = re.sub(r'\b(\w+?)S\b', r'\1', v)   # strip trailing S (matching only)
    v = re.sub(r'\s*&\s*', ' AND ', v)     # & → AND
    v = re.sub(r'(?<=\w)-(?=\w)', '', v)   # remove hyphen between word chars
    # Strip trailing Ltd / LLP / Inc (and variants) so "Acme Ltd" == "Acme"
    v = re.sub(r'\s+(?:LTD|LLP|PLC)\.?\s*$', '', v)
    v = re.sub(r'\s+', ' ', v).strip()
    return v


def init_cap(value: str, keep_caps_set: set) -> str:
    """
    Title-case each word UNLESS it appears in the curated keep_caps_set
    (loaded from candidate_acronyms.csv).

    Fallback when no acronym file is provided: keep ALL-CAPS words with
    no vowels (BBC, NHS, HMRC, DWP, LLP).
    """
    words = value.split()
    result = []
    for word in words:
        core = re.sub(r'[^A-Za-z]', '', word)
        if not core:
            result.append(word)
            continue

        # Check curated acronym set first
        if core.upper() in keep_caps_set:
            result.append(core.upper())
            continue

        # Fallback heuristic: ALL-CAPS with no vowels → likely abbreviation
        if not keep_caps_set:  # only use fallback if no acronym file loaded
            VOWELS = set("AEIOUaeiou")
            is_all_caps = core == core.upper() and len(core) > 1
            has_vowel = any(c in VOWELS for c in core)
            if is_all_caps and not has_vowel:
                result.append(word)
                continue

        result.append(word.capitalize())
    return " ".join(result)


# ── Apply overrides FIRST (case-insensitive, skip all cleaning if matched) ───
print("Applying overrides as first pass ...")
override_series = df["supplier_original"].astype(str).str.strip().str.lower().map(OVERRIDES_MAP)
override_mask = override_series.notna()
df.loc[override_mask, "supplier"] = override_series[override_mask]
override_count = override_mask.sum()
print(f"  {override_count:,} rows matched overrides — these skip all further cleaning.")

# ── Apply per-row cleaning (rules 1-14) — deduplicated for speed ─────────────
# Only clean suppliers that were NOT already resolved by overrides.
print("Cleaning distinct supplier values ...")

# Get the set of original values that were NOT overridden
non_overridden = df.loc[~override_mask, "supplier"].unique()
print(f"  {len(non_overridden):,} distinct supplier values to clean (after excluding overrides).")

clean_map = {raw: apply_cleaning(raw) for raw in non_overridden}
# Apply only to non-overridden rows
df.loc[~override_mask, "supplier"] = df.loc[~override_mask, "supplier"].map(clean_map)
print("  Cleaning done.")

# ── Rules 10-13: sort desc then do comparison passes ─────────────────────────
df.sort_values("supplier", ascending=False, inplace=True)
df.reset_index(drop=True, inplace=True)

all_supplier_vals = df["supplier"].unique().tolist()

# ── Build normalised-key → canonical value lookup (single O(n) pass) ─────────
total_distinct = len(all_supplier_vals)
last_report_ts = time.time()
REPORT_EVERY   = 30  # seconds

print(f"Starting comparison pass on {total_distinct:,} distinct supplier values ...")

key_to_canonical = {}

for i, val in enumerate(all_supplier_vals):
    now = time.time()
    if now - last_report_ts >= REPORT_EVERY:
        print(f"  build pass: {i:,} / {total_distinct:,} done, {total_distinct - i:,} remaining ...")
        last_report_ts = now

    if not val:
        continue

    key = normalise_for_comparison(val)
    if key not in key_to_canonical:
        key_to_canonical[key] = val
    elif '&' in val and '&' not in key_to_canonical[key]:
        # Prefer the & variant as canonical — it's the more precise form
        key_to_canonical[key] = val

        # Rule 10 — also register the key WITHOUT trailing Ltd / LLP so that
        # a bare name later resolves to the Ltd/LLP variant seen first.
        for suffix in ("Ltd", "LLP"):
            norm_suffix = normalise_for_comparison(suffix)
            if key.endswith(" " + norm_suffix):
                base_key = key[: -(len(norm_suffix) + 1)].strip()
                if base_key and base_key not in key_to_canonical:
                    key_to_canonical[base_key] = val

        # Rule 11 — trailing-S stripping is baked into normalise_for_comparison

print(f"  lookup table built: {len(key_to_canonical):,} unique normalised keys.")

# Pass 2 — resolve each distinct cleaned value against the lookup dict
print("Resolving canonical values ...")
resolved_map = {}
for val in all_supplier_vals:
    if not val:
        continue
    key = normalise_for_comparison(val)
    resolved_map[val] = key_to_canonical.get(key, val)

remapped = sum(1 for k, v in resolved_map.items() if k != v)
print(f"  {remapped:,} distinct values remapped to a canonical form.")

# Pass 3 — init-cap using curated acronym set (skip overridden values)
print("Applying init-cap ...")
canonical_capped = {}
for k, v in resolved_map.items():
    # Check if this value came from an override — if so, keep it exactly as-is
    if v in OVERRIDES_MAP.values():
        canonical_capped[k] = v
    else:
        canonical_capped[k] = init_cap(v, KEEP_CAPS_SET)

# Map back to full dataframe
df["supplier"] = df["supplier"].map(canonical_capped)
print("  Done.")

# ── Derive month number from date field (DD/MM/YYYY) ─────────────────────────
print("Deriving month number from date field ...")
df["month_number"] = pd.to_datetime(df["date"], format="%d/%m/%Y", errors="coerce").dt.month
failed = df["month_number"].isna().sum()
if failed:
    print(f"  WARNING: {failed:,} rows had unparseable dates — month_number set to blank.")
print("  Done.")

# ── Copy final cleaned value to supplier_clean ───────────────────────────────
df["supplier_clean"] = df["supplier"]

# ── Audit trail: write distinct changes ───────────────────────────────────────
print("Building audit trail ...")
changed = df[df["supplier_original"].astype(str) != df["supplier"]][
    ["supplier_original", "supplier"]
].drop_duplicates()
changed.to_csv(AUDIT_FILE, index=False)
print(f"  {len(changed):,} distinct supplier name changes written to '{os.path.basename(AUDIT_FILE)}'.")

# ── Write output ──────────────────────────────────────────────────────────────
print(f"Writing output to '{OUTPUT_FILE}' ...")
df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
print(f"Done. Written '{os.path.basename(OUTPUT_FILE)}' ({len(df):,} rows).")

