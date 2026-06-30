#!/usr/bin/env python3
"""
clean_supplier_prefixes.py — strip card/payment-statement descriptor prefixes and
trailing junk from raw supplier strings, BEFORE normalise()/matching.

These prefixes come from card-acquirer / aggregator statement descriptors and are NOT
part of the company name, so they wreck matching (e.g. 'Stk*shutterstock',
'Wp-kamset Digital', 'SP Winstons Wish'). Run this in your supplier-clean pre-step.

The patterns are CONSERVATIVE and anchored at the start (or a clearly-trailing token).
Validate against your data and comment out any that over-fire. Order matters: prefix
strippers first, then trailing, then whitespace tidy.

    from clean_supplier_prefixes import strip_prefixes
    strip_prefixes("Stk*shutterstock")      -> "shutterstock"
    strip_prefixes("Wp-kamset Digital")     -> "kamset Digital"
    strip_prefixes("SP Winstons Wish")      -> "Winstons Wish"
    strip_prefixes("Eplatform.co Ebooks")   -> "Eplatform"      (with STRIP_DOTCO on)
"""

import re

# ── leading payment-descriptor prefixes (case-insensitive, start-anchored) ──
# Each pattern consumes the descriptor AND its separator (* - space) so the real
# name is left clean. Add/remove acquirers as you see them in your data.
PREFIX_PATTERNS = [
    r"^stk\*\s*",            # Stripe              Stk*shutterstock -> shutterstock
    r"^wp[-*]\s*",           # WorldPay            Wp-kamset        -> kamset
    r"^sp[\s*]+",            # SagePay / SumUp     SP Winstons Wish -> Winstons Wish
    r"^sq\s*\*\s*",          # Square              SQ *Cafe         -> Cafe
    r"^iz\s*\*\s*",          # iZettle             IZ *Shop         -> Shop
    r"^ztl\*\s*",            # Zettle
    r"^paypal\s*\*\s*",      # PayPal              PAYPAL *MERCHANT -> MERCHANT
    r"^pp\*\s*",             # PayPal short
    r"^sumup\s*\*?\s*",      # SumUp
    r"^gocardless\s*\*?\s*", # GoCardless
]
_PREFIX_RE = re.compile("|".join(PREFIX_PATTERNS), flags=re.IGNORECASE)

# ── trailing junk ──
# 'X.co <something>' — domain-style token plus trailing descriptor. OFF by default
# because '.co' can be legitimate; enable if your data shows the eplatform.co pattern.
STRIP_DOTCO = False
_DOTCO_RE = re.compile(r"\.co\b.*$", flags=re.IGNORECASE)

# collapse repeated whitespace and trim stray leading separators left behind
_LEAD_SEP_RE = re.compile(r"^[\s*\-:/.]+")
_WS_RE = re.compile(r"\s{2,}")


def strip_prefixes(name: str) -> str:
    if not isinstance(name, str):
        return name
    s = name
    # apply prefix strip repeatedly in case of stacked descriptors (rare)
    for _ in range(3):
        new = _PREFIX_RE.sub("", s, count=1)
        if new == s:
            break
        s = new
    if STRIP_DOTCO:
        s = _DOTCO_RE.sub("", s)
    s = _LEAD_SEP_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s or name        # never return empty; fall back to original


if __name__ == "__main__":
    tests = ["Stk*shutterstock", "Wp-kamset Digital", "SP Winstons Wish",
             "SQ *Corner Cafe", "PAYPAL *MERCHANTX", "Eplatform.co Ebooks",
             "Normal Company Ltd"]
    for t in tests:
        print(f"  {t!r:32} -> {strip_prefixes(t)!r}")
