"""Read and write the spreadsheets TikTok Seller Center imports and exports.

Column names in TikTok's files vary a little by region and over time, so
columns are found by a list of possible names ("aliases") rather than by
exact position. Works with .xlsx and .csv.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Iterable, Optional

from openpyxl import Workbook, load_workbook


class SheetError(Exception):
    pass


def cell_str(v) -> str:
    """Cell value as text. Long numeric ids must not become '5.7e+17'."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def find_col(headers: list, aliases: Iterable[str]) -> Optional[int]:
    """Index of the first header matching any alias (exact first, then 'contains')."""
    h = [norm(x) for x in headers]
    aliases = [norm(a) for a in aliases]
    for a in aliases:
        if a in h:
            return h.index(a)
    for a in aliases:
        for i, x in enumerate(h):
            if x and a and (x.startswith(a) or a in x):
                return i
    return None


def _is_xlsx(filename: str, data: bytes) -> bool:
    return filename.lower().endswith((".xlsx", ".xlsm")) or data[:2] == b"PK"


def read_rows(data: bytes, filename: str) -> list[list]:
    """All rows of the first sheet as lists of strings."""
    if _is_xlsx(filename, data):
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:
            raise SheetError(f"Couldn't open the Excel file: {e}")
        ws = wb.worksheets[0]
        rows = [[cell_str(c) for c in r] for r in ws.iter_rows(values_only=True)]
        wb.close()
        return rows
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]


def header_index(rows: list[list], key_aliases: Iterable[str], scan: int = 15) -> int:
    for i, r in enumerate(rows[:scan]):
        if find_col(r, key_aliases) is not None:
            return i
    raise SheetError("Couldn't find the header row (looked for: " + ", ".join(key_aliases) + ").")


def read_table(data: bytes, filename: str, key_aliases: Iterable[str]) -> tuple[list, list[list]]:
    """(headers, data rows) — rows above the header are skipped."""
    rows = read_rows(data, filename)
    if not rows:
        raise SheetError("The file is empty.")
    h = header_index(rows, key_aliases)
    return rows[h], [r for r in rows[h + 1:] if any(c for c in r)]


def looks_like_id(v) -> bool:
    return bool(re.fullmatch(r"\s*\d{5,}\s*", str(v or "")))


def fill_template(data: bytes, filename: str, key_aliases: list[str], columns: dict[str, list[str]],
                  records: dict[str, dict], drop_unmatched: bool) -> bytes:
    """Fill a TikTok template you downloaded from Seller Center.

    key_aliases: names of the column used to match rows (e.g. Order ID or Seller SKU)
    columns:     field -> header aliases to write into
    records:     key value -> {field: value}
    drop_unmatched: remove data rows whose key isn't in `records` (e.g. orders
                 you don't fulfil through TTDS). Instruction rows are kept.
    Keys not already in the template are appended at the bottom.
    """
    if _is_xlsx(filename, data):
        wb = load_workbook(io.BytesIO(data))
        ws = wb.worksheets[0]
        rows = [[c.value for c in r] for r in ws.iter_rows()]
        h = header_index([[cell_str(v) for v in r] for r in rows], key_aliases)
        headers = [cell_str(v) for v in rows[h]]
        key_col = find_col(headers, key_aliases)
        cols = {f: find_col(headers, a) for f, a in columns.items()}
        missing = [f for f, c in cols.items() if c is None]
        if missing:
            raise SheetError(f"The template has no column for: {', '.join(missing)}. "
                             f"Its columns are: {', '.join(x for x in headers if x)}")
        done, delete = set(), []
        for r in range(h + 2, ws.max_row + 1):  # openpyxl rows are 1-based
            key = cell_str(ws.cell(r, key_col + 1).value)
            if key in records:
                for f, c in cols.items():
                    if f in records[key]:
                        ws.cell(r, c + 1).value = records[key][f]
                done.add(key)
            elif drop_unmatched and looks_like_id(key):
                delete.append(r)
        for r in reversed(delete):
            ws.delete_rows(r)
        row = ws.max_row + 1
        for key, rec in records.items():
            if key in done:
                continue
            ws.cell(row, key_col + 1).value = key
            for f, c in cols.items():
                if f in rec:
                    ws.cell(row, c + 1).value = rec[f]
            row += 1
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    # CSV template
    rows = read_rows(data, filename)
    h = header_index(rows, key_aliases)
    headers = rows[h]
    key_col = find_col(headers, key_aliases)
    cols = {f: find_col(headers, a) for f, a in columns.items()}
    missing = [f for f, c in cols.items() if c is None]
    if missing:
        raise SheetError(f"The template has no column for: {', '.join(missing)}.")
    out_rows, done = rows[:h + 1], set()
    for r in rows[h + 1:]:
        r = r + [""] * (len(headers) - len(r))
        key = r[key_col].strip()
        if key in records:
            for f, c in cols.items():
                if f in records[key]:
                    r[c] = str(records[key][f])
            done.add(key)
        elif drop_unmatched and looks_like_id(key):
            continue
        out_rows.append(r)
    for key, rec in records.items():
        if key not in done:
            r = [""] * len(headers)
            r[key_col] = key
            for f, c in cols.items():
                if f in rec:
                    r[c] = str(rec[f])
            out_rows.append(r)
    buf = io.StringIO()
    csv.writer(buf).writerows(out_rows)
    return buf.getvalue().encode("utf-8-sig")


def simple_xlsx(headers: list[str], rows: list[list], widths: Optional[list[int]] = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    for i, w in enumerate(widths or [22] * len(headers)):
        ws.column_dimensions[chr(65 + i)].width = w
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def simple_csv(headers: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")
