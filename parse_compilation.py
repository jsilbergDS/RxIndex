#!/usr/bin/env python3
"""Parse the FDA CDER NME/New-Biologic compilation xlsx WITHOUT openpyxl (zip+XML),
then compare 2021-2025 rows against our built cards + canonical CSV."""
import zipfile, re, sys, csv, glob, os
import xml.etree.ElementTree as ET

F = "/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/mcp_major_amendments/2026 Compilation_of_CDER_NME_and_New_Biologic_Approvals_1985-2025.xlsx"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

def col_letters(ref):
    return re.match(r"[A-Z]+", ref).group(0)
def col_idx(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1

with zipfile.ZipFile(F) as z:
    # shared strings
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{NS}si"):
            txt = "".join(t.text or "" for t in si.iter(f"{NS}t"))
            shared.append(txt)
    # find sheet names -> file
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheets = [(s.get("name"), s.get(f"{{http://schemas.openxmlformats.org/officeDocument/2006/relationships}}id"))
              for s in wb.iter(f"{NS}sheet")]
    print("SHEETS:", [s[0] for s in sheets], flush=True)
    # just parse every worksheet file
    all_rows = {}
    for name in z.namelist():
        if not re.match(r"xl/worksheets/sheet\d+\.xml$", name):
            continue
        root = ET.fromstring(z.read(name))
        rows = []
        for row in root.iter(f"{NS}row"):
            cells = {}
            for c in row.findall(f"{NS}c"):
                ref = c.get("r"); t = c.get("t")
                v = c.find(f"{NS}v"); iss = c.find(f"{NS}is")
                if v is not None:
                    val = v.text
                    if t == "s":
                        val = shared[int(val)]
                elif iss is not None:
                    val = "".join(x.text or "" for x in iss.iter(f"{NS}t"))
                else:
                    val = None
                cells[col_idx(col_letters(ref))] = val
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(i) for i in range(width)])
        all_rows[name] = rows
    # use the biggest sheet
    sheet = max(all_rows.values(), key=len)

print("TOTAL ROWS:", len(sheet), flush=True)
# find header row (row containing 'Year' or 'Approval' or 'Drug')
hdr_i = None
for i, r in enumerate(sheet[:15]):
    joined = " ".join(str(c).lower() for c in r if c)
    if any(k in joined for k in ("year", "approval", "trade", "active", "applic")):
        hdr_i = i; break
print(f"HEADER row {hdr_i}:", [str(c)[:24] for c in sheet[hdr_i]] if hdr_i is not None else "NOT FOUND", flush=True)
