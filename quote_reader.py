"""Reads a Mountainland quote PDF (bid board: moving a bid to Quoted).

The quote system prints the same layout every time: a QUOTE NUMBER box at the
top right (e.g. S108301093) and a totals box at the bottom ending in
"Amount Due 11333.54". pypdf's layout mode keeps each label on the same line as
its number, so plain text matching is enough — no AI needed.

read(pdf_bytes) -> {"amount": "11333.54" | None, "quote": "S108301093" | None,
                    "where": "Amount Due, page 1" | None}
"""

import io
import re

from pypdf import PdfReader

_MONEY = r"\$?\s*(-?[\d,]+\.\d{2})"
_AMOUNT = re.compile(r"Amount\s+Due\s*:?\s*" + _MONEY, re.I)
_QUOTE_NO = re.compile(r"\b([A-Z]{1,2}\d{6,12})\b")


def _pages(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    out = []
    for page in reader.pages:
        try:
            out.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:
            out.append(page.extract_text() or "")
    return out


def _quote_number(lines):
    """The number under the QUOTE NUMBER heading: the first quote-shaped code on
    the few lines after it."""
    for i, line in enumerate(lines):
        if re.search(r"QUOTE\s+(NUMBER|NO\.?|#)", line, re.I):
            for nxt in lines[i:i + 4]:
                m = _QUOTE_NO.search(re.sub(r"QUOTE\s+(NUMBER|NO\.?|#)", "", nxt, flags=re.I))
                if m:
                    return m.group(1)
    return None


def read(pdf_bytes, filename=""):
    pages = _pages(pdf_bytes)
    amount = where = quote = None
    # multi-page quotes print the totals on the last page; take the last Amount Due found
    for n, text in enumerate(pages, 1):
        for m in _AMOUNT.finditer(text):
            amount, where = m.group(1).replace(",", ""), f"Amount Due, page {n}"
        if quote is None:
            quote = _quote_number(text.splitlines())
    if quote is None:
        # the file name the quote system gives it: S108301093-0001.pdf
        m = re.match(r"([A-Z]{1,2}\d{6,12})(?:[-_ ]\d+)?\.pdf$", filename or "", re.I)
        if m:
            quote = m.group(1).upper()
    return {"amount": amount, "quote": quote, "where": where}


if __name__ == "__main__":
    import json
    import sys
    path = sys.argv[1]
    with open(path, "rb") as f:
        print(json.dumps(read(f.read(), path.rsplit("/", 1)[-1])))
