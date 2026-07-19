#!/usr/bin/env python3
"""Compare FDA compilation 2021-2025 NMEs/biologics vs our built cards + canonical CSV."""
import zipfile, re, csv, glob, os
import xml.etree.ElementTree as ET

F = "/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/mcp_major_amendments/2026 Compilation_of_CDER_NME_and_New_Biologic_Approvals_1985-2025.xlsx"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
def cidx(ref):
    L = re.match(r"[A-Z]+", ref).group(0); n=0
    for ch in L: n=n*26+(ord(ch)-64)
    return n-1

with zipfile.ZipFile(F) as z:
    shared=[]
    root=ET.fromstring(z.read("xl/sharedStrings.xml"))
    for si in root.findall(f"{NS}si"):
        shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
    sheet=[]
    for name in z.namelist():
        if re.match(r"xl/worksheets/sheet\d+\.xml$", name):
            r0=ET.fromstring(z.read(name))
            for row in r0.iter(f"{NS}row"):
                cells={}
                for c in row.findall(f"{NS}c"):
                    v=c.find(f"{NS}v"); val=None
                    if v is not None:
                        val=shared[int(v.text)] if c.get("t")=="s" else v.text
                    cells[cidx(c.get("r"))]=val
                if cells: sheet.append([cells.get(i) for i in range(max(cells)+1)])
            break

hdr=sheet[0]
def col(name):
    for i,h in enumerate(hdr):
        if h and name.lower() in str(h).lower(): return i
    return None
C_NAME=col("Proprietary"); C_TYPE=col("NDA/BLA"); C_APP=col("Application Number(1)")
C_YEAR=col("Approval Year"); C_ING=col("Active Ingredient")

def norm_app(s):
    d=re.sub(r"\D","",str(s or ""))
    return d.lstrip("0") or d

# compilation 2021-2025 rows
comp={}
for r in sheet[1:]:
    if len(r)<=max(C_YEAR,C_APP): continue
    yr=str(r[C_YEAR] or "").strip()[:4]
    if yr in ("2021","2022","2023","2024","2025"):
        app=norm_app(r[C_APP])
        if app:
            comp[app]={"name":r[C_NAME],"type":r[C_TYPE],"year":yr,"ing":r[C_ING] if C_ING else ""}
print(f"FDA compilation 2021-2025 NME/biologic rows: {len(comp)}", flush=True)

# our built cards
built={norm_app(os.path.basename(f)[:-5]) for f in glob.glob("data/cards_single/*.json")}
print(f"our built cards: {len(built)}", flush=True)

# canonical CSV 2021-2025 (all, incl <5 docs)
D="/oak/stanford/groups/jamesz/jsilberg/FDA_medr_pdfs/drgusatfda"
csv_apps={}
for r in csv.DictReader(open(f"{D}/nme_paperclip_coverage_through2026.csv")):
    if r["approval_year"] in ("2021","2022","2023","2024","2025"):
        csv_apps[norm_app(r["appno"])]={"drug":r["drug"],"ndocs":int(r["n_docs_in_paperclip"])}

# 1) compilation entries NOT built
missing=[(a,c) for a,c in comp.items() if a not in built]
print(f"\n=== compilation 2021-2025 NOT in our built cards: {len(missing)} ===", flush=True)
for a,c in sorted(missing, key=lambda x:x[1]["year"]):
    incsv=csv_apps.get(a)
    tag=f"CSV has it, ndocs={incsv['ndocs']}" if incsv else "NOT in our canonical CSV at all"
    print(f"  {c['year']} {c['type'] or '?':<4} {a:<8} {str(c['name'])[:26]:<26} [{tag}]", flush=True)

# 2) our built cards NOT in compilation (sanity: did we build non-NME?)
extra=[a for a in built if a not in comp]
print(f"\n=== our built cards NOT in FDA compilation 2021-2025: {len(extra)} ===", flush=True)
for a in sorted(extra):
    print(f"  {a} ({csv_apps.get(a,{}).get('drug','?')})", flush=True)
