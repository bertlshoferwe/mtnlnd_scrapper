"""Reads a Mountainland quote PDF (bid board: moving a bid to Quoted).

The quote system prints two layouts:
  - "Quote": a QUOTE NUMBER box at the top right (S108301093) and totals ending
    in "Amount Due 11333.54".
  - "Bid": "Bid" over the number at the top right (S108307002.) and totals
    ending in "Due $54,801.14", with tabs between the words.
pypdf's layout mode keeps each label on the same line as its number, so plain
text matching is enough — no AI needed.

read(pdf_bytes) -> {"amount": "11333.54" | None, "quote": "S108301093" | None,
                    "where": "Amount Due, page 1" | None}
"""

import io
import re

from pypdf import PdfReader

_MONEY = r"\$?\s*(-?[\d,]+\.\d{2})"
_AMOUNT = re.compile(r"\b((?:Amount\s+)?Due)\s*:?\s*" + _MONEY, re.I)
_HEAD = re.compile(r"QUOTE\s+(NUMBER|NO\.?|#)|^\s*(Bid|Quote)\s*$", re.I)
_QUOTE_NO = re.compile(r"\b([A-Z]{1,2}\d{6,12})\b")


def _pages(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    out = []
    for page in reader.pages:
        try:
            t = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            t = page.extract_text() or ""
        out.append(t.replace("\t", " "))
    return out


def _quote_number(lines):
    """The number under the QUOTE NUMBER (or "Bid") heading: the first
    quote-shaped code on the few lines after it."""
    for i, line in enumerate(lines):
        if _HEAD.search(line):
            for nxt in lines[i:i + 4]:
                m = _QUOTE_NO.search(_HEAD.sub("", nxt))
                if m:
                    return m.group(1)
    return None


def read(pdf_bytes, filename=""):
    pages = _pages(pdf_bytes)
    amount = where = quote = None
    # multi-page quotes print the totals on the last page; take the last Amount Due found
    for n, text in enumerate(pages, 1):
        for m in _AMOUNT.finditer(text):
            amount = m.group(2).replace(",", "")
            where = f"{' '.join(m.group(1).split()).title()}, page {n}"
        if quote is None:
            quote = _quote_number(text.splitlines())
    if quote is None:
        # the file name the quote system gives it: S108301093-0001.pdf, <id>-S108307002_001_<n>.pdf
        m = re.search(r"(?:^|[-_ ])([A-Z]{1,2}\d{6,12})(?:[-_ ]\d+)*\.pdf$", filename or "", re.I)
        if m:
            quote = m.group(1).upper()
    return {"amount": amount, "quote": quote, "where": where}


if __name__ == "__main__":
    import json
    import sys
    path = sys.argv[1]
    with open(path, "rb") as f:
        print(json.dumps(read(f.read(), path.rsplit("/", 1)[-1])))
