#!/usr/bin/env python3
"""For each 'missing' 2021-2025 NME, query paperclip LIVE and classify buildability
by whether a review doc + label actually exist (ignore the stale CSV count)."""
import subprocess, re

PAPERCLIP = "/home/users/jsilberg/.local/bin/paperclip"
MISSING = [
    ("214383","Pepaxto","2021"),("214231","Zegalogue","2021"),("211964","Qelbree","2021"),
    ("215341","Kerendia","2021"),("215206","Qulipta","2021"),("214487","Tavneos","2021"),
    ("215973","Sunlenca","2022"),("214375","Xenoview","2022"),("216017","Iomervu","2024"),
    ("215244","Forzinity","2025"),("219469","Lynkuet","2025"),("220305","Komzifti","2025"),
    ("219947","Redemplo","2025"),("219972","Hyrnuo","2025"),("761434","Voyxact","2025"),
    ("219792","Kygevvi","2025"),("218571","Cardamyst","2025"),("761427","Lerochol","2025"),
    ("219491","Nuzolvence","2025"),("761458","Exdensur","2025"),("219083","Myqorzo","2025"),
    ("761152","Yartemlea","2025"),("220152","Nereus","2025"),
]
REVIEW = re.compile(r"(IntegratedR|MultidisciplineR|MedR|SumR|SummaryReview)", re.I)
LABEL  = re.compile(r"(s000|Orig1s000|s001).*[Ll]bl\.pdf|[Ll]bl\.pdf", re.I)
LETTER = re.compile(r"ltr\.pdf", re.I)

buildable, thin, empty = [], [], []
for app, name, yr in MISSING:
    out = subprocess.run([PAPERCLIP,"sql","--source","fda",
        f"SELECT SUBSTRING(public_url FROM 40 FOR 55) AS f FROM documents WHERE public_url ILIKE '%{app}%'"],
        capture_output=True, text=True, timeout=30).stdout
    files = [ln for ln in out.split("\n") if ".pdf" in ln.lower()]
    has_rev = any(REVIEW.search(f) for f in files)
    has_lbl = any("lbl.pdf" in f.lower() for f in files)
    has_ltr = any("ltr.pdf" in f.lower() for f in files)
    rev_types = sorted({REVIEW.search(f).group(1) for f in files if REVIEW.search(f)})
    tag = f"{yr} {name:<12} rev={rev_types or '-'} lbl={has_lbl} ltr={has_ltr} ({len(files)} docs)"
    if has_rev and has_lbl:
        buildable.append((app,tag))
    elif has_lbl or has_ltr:
        thin.append((app,tag))
    else:
        empty.append((app,tag))

print(f"=== BUILDABLE (has review + label): {len(buildable)} ===", flush=True)
for a,t in buildable: print(f"  {a}  {t}", flush=True)
print(f"\n=== THIN (label/letter only, no review doc): {len(thin)} ===", flush=True)
for a,t in thin: print(f"  {a}  {t}", flush=True)
print(f"\n=== EMPTY (no usable docs): {len(empty)} ===", flush=True)
for a,t in empty: print(f"  {a}  {t}", flush=True)
print(f"\nSUMMARY: {len(buildable)} buildable, {len(thin)} thin, {len(empty)} empty (of {len(MISSING)})", flush=True)
