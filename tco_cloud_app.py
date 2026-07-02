"""
Multi-Cloud TCO Comparison Agent
================================
Compares fixed 1-year / 3-year infrastructure costs across AWS, Azure & GCP.
Region: India (Mumbai) — the region common to all three clouds; shown in results.

Input: natural language (via Groq), Excel/CSV upload, or cloud SKU names (any cloud).
Pricing: live via Infracost. AWS uses real reserved rates; Azure & GCP use live
on-demand reduced by published commitment discounts (RI / CUD).

SETUP: paste your two API keys below, then run:  streamlit run tco_app.py
"""

import json
import os
import requests
import pandas as pd
import streamlit as st

# ============================================================
#  API KEYS
#  - For LOCAL runs: paste your keys in the fallback strings below.
#  - For DEPLOYMENT: leave them as-is and set keys in Streamlit
#    Cloud "Secrets" (they override the fallback automatically).
# ============================================================
def _get_key(name, fallback):
    try:
        return st.secrets[name]
    except Exception:
        return os.environ.get(name, fallback)

GROQ_KEY = _get_key("GROQ_KEY", "PASTE_YOUR_GROQ_KEY_HERE")
INFRACOST_KEY = _get_key("INFRACOST_KEY", "PASTE_YOUR_INFRACOST_KEY_HERE")
# ============================================================

# ---- Owner / author credit ----
OWNER_NAME = "Jitendra"
OWNER_TITLE = "Cloud Solution Architect"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
INFRACOST_URL = "https://pricing.api.infracost.io/graphql"
GROQ_MODEL = "llama-3.3-70b-versatile"
HOURS_PER_MONTH = 730

# Fixed region — Mumbai (common to all three clouds).
REGION = {"aws": "ap-south-1", "azure": "centralindia", "gcp": "asia-south1"}
REGION_DISPLAY = {
    "aws": "Mumbai (ap-south-1)",
    "azure": "Central India (Mumbai region)",
    "gcp": "Mumbai (asia-south1)",
}
REGION_LABEL = "India — Mumbai region"

# Published commitment discounts (AWS uses REAL reserved instead).
DISCOUNT = {
    "azure": {"1yr": 0.40, "3yr": 0.60},
    "gcp":   {"1yr": 0.37, "3yr": 0.55},
}

# ============================================================
# ROLE -> INTENT -> FAMILY
# ============================================================
ROLE_INTENT = [
    (["db", "database", "sql", "postgres", "oracle", "mysql", "mongo",
      "cache", "redis", "memcache", "memory", "in-memory", "sap", "hana"], "mem"),
    (["compute", "batch", "hpc", "render", "analytics", "ml", "ai",
      "processing", "encode", "transcode"], "cpu"),
    (["web", "frontend", "front-end", "nginx", "apache", "app",
      "application", "api", "backend", "back-end", "general",
      "dev", "test", "staging", "qa"], "gp"),
]
INTENT_LABEL = {"gp": "General-purpose", "mem": "Memory-optimized", "cpu": "Compute-optimized"}
SIZE_MAP = {2: "large", 4: "xlarge", 8: "2xlarge", 16: "4xlarge",
            32: "8xlarge", 48: "12xlarge", 64: "16xlarge"}

def _round_up(n):
    for k in [2, 4, 8, 16, 32, 48, 64]:
        if n <= k:
            return k
    return n

def role_to_intent(role):
    r = (role or "").lower()
    for keywords, intent in ROLE_INTENT:
        if any(k in r for k in keywords):
            return intent
    return "gp"

def resolve_intent(vm):
    hint = (vm.get("intent_hint") or "").lower()
    return hint if hint in ("gp", "mem", "cpu") else role_to_intent(vm.get("role"))

# Verified processor families (Mumbai):
#   AWS   Intel yes | AMD NO  | ARM yes
#   Azure Intel yes | AMD yes | ARM yes (general only)
#   GCP   Intel yes | AMD yes | ARM NO
def aws_family(intent, n, proc):
    n = _round_up(n); size = SIZE_MAP[n]; note = ""
    if proc == "AMD":
        proc, note = "Intel", "AMD n/a in AWS India → Intel"
    prefix = ({"gp": "m7g", "mem": "r8g", "cpu": "c7g"}[intent] if proc == "ARM"
              else {"gp": "m7i", "mem": "r7i", "cpu": "c7i"}[intent])
    return f"{prefix}.{size}", proc, note

def azure_family(intent, n, proc):
    n = _round_up(n); note = ""
    if proc == "ARM" and intent != "gp":
        proc, note = "Intel", "ARM only general-purpose → Intel"
    if proc == "AMD":
        prefix = {"gp": "D", "mem": "E", "cpu": "F"}[intent]
        suffix = {"gp": "as_v5", "mem": "as_v5", "cpu": "as_v6"}[intent]
    elif proc == "ARM":
        prefix, suffix = "D", "ps_v5"
    else:
        prefix = {"gp": "D", "mem": "E", "cpu": "F"}[intent]
        suffix = {"gp": "s_v5", "mem": "s_v5", "cpu": "s_v2"}[intent]
    return f"Standard_{prefix}{n}{suffix}", proc, note

def gcp_family(intent, n, proc):
    n = _round_up(n); note = ""
    if proc == "ARM":
        proc, note = "Intel", "ARM n/a in GCP India → Intel"
    name = ({"gp": f"n2d-standard-{n}", "mem": f"n2d-highmem-{n}", "cpu": f"n2d-highcpu-{n}"}[intent]
            if proc == "AMD"
            else {"gp": f"n2-standard-{n}", "mem": f"n2-highmem-{n}", "cpu": f"n2-highcpu-{n}"}[intent])
    return name, proc, note

def processor_choices(intent):
    return ["Intel", "AMD", "ARM"] if intent == "gp" else ["Intel", "AMD"]

# ============================================================
# SKU DECODING (Python fast-path)
# ============================================================
_RATIO = {"gp": 4, "mem": 8, "cpu": 2}
_AWS_SIZE = {"large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16,
             "8xlarge": 32, "12xlarge": 48, "16xlarge": 64}

def decode_sku(sku):
    if not sku:
        return None
    s = str(sku).strip(); sl = s.lower()
    if sl.startswith("standard_"):
        body = s.split("_", 1)[1]
        letter = body[0].upper()
        num = ""
        for ch in body:
            if ch.isdigit():
                num += ch
            elif num:
                break
        n = int(num) if num else 0
        intent = {"D": "gp", "E": "mem", "F": "cpu"}.get(letter)
        if intent and n:
            return n, n * _RATIO[intent], intent
        return None
    if "." in sl and any(sl.startswith(p) for p in
                         ("m7i", "m7a", "m7g", "r7i", "r7a", "r8g", "c7i", "c7a", "c7g")):
        fam, size = sl.split(".", 1)
        n = _AWS_SIZE.get(size)
        intent = {"m": "gp", "r": "mem", "c": "cpu"}.get(fam[0])
        if intent and n:
            return n, n * _RATIO[intent], intent
        return None
    if sl.startswith("n2-") or sl.startswith("n2d-"):
        parts = sl.split("-")
        if len(parts) >= 3 and parts[-1].isdigit():
            n = int(parts[-1]); kind = parts[-2]
            intent = {"standard": "gp", "highmem": "mem", "highcpu": "cpu"}.get(kind)
            if intent and n:
                return n, n * _RATIO[intent], intent
    return None

# ============================================================
# GROQ
# ============================================================
PARSE_SYSTEM = """You extract VM specs from the user's message. Output ONLY valid JSON, no code fences, no prose. Start with { end with }.

The user may give specs as raw numbers OR as a cloud SKU/instance name (any cloud). Decode SKUs into vCPU and RAM.

Output shape:
{"vms": [{"name":"vm-1","role":"web","vcpu":4,"ram_gb":16,"storage_gb":100,"count":3,"intent_hint":""}], "missing": []}

SKU decoding:
- Azure Standard_D<n>s_v5 = general (1:4); Standard_E<n>... = memory (1:8); Standard_F<n>... = compute (1:2); <n>=vCPU.
- AWS m7i/m7a/m7g.<size> = general(1:4); r7i/r7a/r8g = memory(1:8); c7i/c7a/c7g = compute(1:2). size: large=2,xlarge=4,2xlarge=8,4xlarge=16,8xlarge=32,12xlarge=48,16xlarge=64.
- GCP n2-standard-<n>/n2d-standard-<n> = general; n2-highmem-<n> = memory; n2-highcpu-<n> = compute; <n>=vCPU.

Rules:
- Extract every distinct group; input may mix SKUs and raw specs.
- intent_hint: "gp"/"mem"/"cpu" only if derived from a SKU family, else "".
- role: stated role, else the SKU name, else "".
- integers only; strip units; TB->x1024; storage 0 if absent; count 1 if absent.
- missing: groups still lacking vcpu or ram_gb; else [].
"""

SKU_DECODE_SYSTEM = """You are a cloud instance expert. The user gives ONE cloud SKU/instance name. Return JSON only:
{"vcpu":<int>,"ram_gb":<int>,"intent":"gp|mem|cpu","known":true}
intent: gp=general, mem=memory-optimized, cpu=compute-optimized.
If you don't reliably know this exact SKU, return {"vcpu":0,"ram_gb":0,"intent":"gp","known":false}. Never guess."""

def _groq(messages):
    r = requests.post(GROQ_URL,
                      headers={"Authorization": f"Bearer {GROQ_KEY}",
                               "Content-Type": "application/json"},
                      json={"model": GROQ_MODEL, "messages": messages, "temperature": 0},
                      timeout=30)
    return r.json()["choices"][0]["message"]["content"]

def groq_parse(user_text):
    c = _groq([{"role": "system", "content": PARSE_SYSTEM},
               {"role": "user", "content": user_text}])
    s, e = c.find("{"), c.rfind("}")
    return json.loads(c[s:e + 1])

def groq_decode_sku(sku):
    try:
        c = _groq([{"role": "system", "content": SKU_DECODE_SYSTEM},
                   {"role": "user", "content": str(sku)}])
        s, e = c.find("{"), c.rfind("}")
        d = json.loads(c[s:e + 1])
        if d.get("known") and d.get("vcpu") and d.get("ram_gb"):
            intent = d.get("intent") if d.get("intent") in ("gp", "mem", "cpu") else "gp"
            return int(d["vcpu"]), int(d["ram_gb"]), intent
    except Exception:
        pass
    return None

# ============================================================
# INFRACOST
# ============================================================
def infracost_query(query):
    r = requests.post(INFRACOST_URL,
                      headers={"X-Api-Key": INFRACOST_KEY,
                               "Content-Type": "application/json"},
                      json={"query": query}, timeout=30)
    return r.json().get("data", {}).get("products", [])

def aws_ondemand(it):
    q = f'''{{ products(filter: {{vendorName:"aws", service:"AmazonEC2", productFamily:"Compute Instance", region:"{REGION['aws']}",
      attributeFilters:[{{key:"instanceType",value:"{it}"}},{{key:"operatingSystem",value:"Linux"}},{{key:"tenancy",value:"Shared"}},{{key:"capacitystatus",value:"Used"}},{{key:"preInstalledSw",value:"NA"}}]}})
      {{ prices(filter:{{purchaseOption:"on_demand"}}) {{ USD }} }} }}'''
    for p in infracost_query(q):
        for pr in p.get("prices", []):
            if pr.get("USD") and float(pr["USD"]) > 0:
                return float(pr["USD"])
    return None

def aws_reserved(it, term):
    q = f'''{{ products(filter: {{vendorName:"aws", service:"AmazonEC2", productFamily:"Compute Instance", region:"{REGION['aws']}",
      attributeFilters:[{{key:"instanceType",value:"{it}"}},{{key:"operatingSystem",value:"Linux"}},{{key:"tenancy",value:"Shared"}},{{key:"capacitystatus",value:"Used"}},{{key:"preInstalledSw",value:"NA"}}]}})
      {{ prices(filter:{{purchaseOption:"reserved"}}) {{ USD termLength termPurchaseOption termOfferingClass }} }} }}'''
    for p in infracost_query(q):
        for pr in p.get("prices", []):
            if (pr.get("termLength") == term and pr.get("termPurchaseOption") == "No Upfront"
                    and pr.get("termOfferingClass") == "standard"
                    and pr.get("USD") and float(pr["USD"]) > 0):
                return float(pr["USD"])
    return None

def azure_ondemand(sku):
    q = f'''{{ products(filter: {{vendorName:"azure", service:"Virtual Machines", region:"{REGION['azure']}",
      attributeFilters:[{{key:"armSkuName",value:"{sku}"}}]}}) {{ prices(filter:{{purchaseOption:"Consumption"}}) {{ USD }} }} }}'''
    vals = []
    for p in infracost_query(q):
        for pr in p.get("prices", []):
            if pr.get("USD") and float(pr["USD"]) > 0:
                vals.append(float(pr["USD"]))
    return min(vals) if vals else None

def gcp_ondemand(mt):
    q = f'''{{ products(filter: {{vendorName:"gcp", service:"Compute Engine", region:"{REGION['gcp']}",
      attributeFilters:[{{key:"machineType",value:"{mt}"}}]}}) {{ prices(filter:{{purchaseOption:"on_demand"}}) {{ USD }} }} }}'''
    for p in infracost_query(q):
        for pr in p.get("prices", []):
            if pr.get("USD") and float(pr["USD"]) > 0:
                return float(pr["USD"])
    return None

# ============================================================
# PRICING ORCHESTRATION
# ============================================================
def _fetch_cloud(cloud, intent, vcpu, term, proc):
    if cloud == "AWS":
        sku, used, note = aws_family(intent, vcpu, proc)
        hr = aws_reserved(sku, term)
        if hr is not None:
            basis = "real reserved"
        else:
            od = aws_ondemand(sku)
            hr = od * (1 - DISCOUNT["azure"][term]) if od else None
            basis = f"est. -{int(DISCOUNT['azure'][term]*100)}%" if hr else "unavailable"
    elif cloud == "Azure":
        sku, used, note = azure_family(intent, vcpu, proc)
        od = azure_ondemand(sku)
        hr = od * (1 - DISCOUNT["azure"][term]) if od else None
        basis = f"est. RI -{int(DISCOUNT['azure'][term]*100)}%" if hr else "unavailable"
    else:
        sku, used, note = gcp_family(intent, vcpu, proc)
        od = gcp_ondemand(sku)
        hr = od * (1 - DISCOUNT["gcp"][term]) if od else None
        basis = f"est. CUD -{int(DISCOUNT['gcp'][term]*100)}%" if hr else "unavailable"
    if note:
        basis = f"{basis} · {note}"
    return {"sku": sku, "proc": used, "hourly": hr, "basis": basis}

def _best_cloud(cloud, intent, vcpu, term, proc_choice):
    cands = processor_choices(intent) if proc_choice == "Cheapest" else [proc_choice]
    best = None
    for proc in cands:
        r = _fetch_cloud(cloud, intent, vcpu, term, proc)
        if r["hourly"] is not None and (best is None or r["hourly"] < best["hourly"]):
            best = r
    if best is None:
        if cloud == "AWS":
            sku, _, _ = aws_family(intent, vcpu, "Intel")
        elif cloud == "Azure":
            sku, _, _ = azure_family(intent, vcpu, "Intel")
        else:
            sku, _, _ = gcp_family(intent, vcpu, "Intel")
        return {"sku": sku, "proc": "Intel", "hourly": None, "basis": "unavailable"}
    return best

def price_vm(vm, term, proc_choice):
    intent = resolve_intent(vm)
    vcpu = int(vm.get("vcpu") or 0)
    ram = int(vm.get("ram_gb") or 0)
    count = int(vm.get("count") or 1)
    return [
        _row(vm, "AWS", _best_cloud("AWS", intent, vcpu, term, proc_choice), intent, vcpu, ram, count),
        _row(vm, "Azure", _best_cloud("Azure", intent, vcpu, term, proc_choice), intent, vcpu, ram, count),
        _row(vm, "GCP", _best_cloud("GCP", intent, vcpu, term, proc_choice), intent, vcpu, ram, count),
    ]

def _row(vm, cloud, r, intent, vcpu, ram, count):
    hourly = r["hourly"]
    monthly = round(hourly * HOURS_PER_MONTH * count, 2) if hourly is not None else None
    ckey = {"AWS": "aws", "Azure": "azure", "GCP": "gcp"}[cloud]
    return {
        "VM": vm.get("name") or vm.get("role") or "vm",
        "Role": vm.get("role") or "(unspecified)",
        "Intent": INTENT_LABEL[intent],
        "Cloud": cloud,
        "Region": REGION_DISPLAY[ckey],
        "SKU": r["sku"],
        "Proc": r["proc"],
        "vCPU/RAM": f"{vcpu} / {ram} GB",
        "Count": count,
        "Monthly (USD)": monthly if monthly is not None else "Unavailable",
        "Basis": r["basis"],
    }

# ============================================================
# FILE INPUT
# ============================================================
def normalize_columns(df):
    m = {}
    for c in df.columns:
        cl = str(c).strip().lower()
        if cl in ("name", "vm", "vm name", "server", "hostname"): m[c] = "name"
        elif cl in ("role", "type", "purpose", "workload", "tier"): m[c] = "role"
        elif cl in ("sku", "instance", "instance type", "instancetype", "machine type", "machinetype", "size"): m[c] = "sku"
        elif cl in ("vcpu", "cpu", "cores", "vcpus", "core"): m[c] = "vcpu"
        elif cl in ("ram", "ram_gb", "memory", "mem", "ram (gb)", "memory (gb)"): m[c] = "ram_gb"
        elif cl in ("storage", "storage_gb", "disk", "disk_gb", "storage (gb)", "disk (gb)"): m[c] = "storage_gb"
        elif cl in ("count", "qty", "quantity", "instances", "number"): m[c] = "count"
    return df.rename(columns=m)

def rows_from_file(file):
    df = pd.read_csv(file) if file.name.lower().endswith(".csv") else pd.read_excel(file)
    df = normalize_columns(df)
    vms = []
    for i, row in df.iterrows():
        def gv(k, d=None):
            return row[k] if k in df.columns and pd.notna(row.get(k)) else d
        def gi(k, d=0):
            try:
                return int(float(gv(k)))
            except (TypeError, ValueError):
                return d
        vcpu, ram, intent_hint = gi("vcpu", 0), gi("ram_gb", 0), ""
        sku_val = gv("sku", None)
        if (not vcpu or not ram) and sku_val:
            dec = decode_sku(sku_val) or groq_decode_sku(sku_val)
            if dec:
                vcpu, ram, intent_hint = dec
        role = str(gv("role", "") or "")
        if not role and sku_val:
            role = str(sku_val)
        vms.append({"name": str(gv("name", f"vm-{i+1}")), "role": role,
                    "vcpu": vcpu, "ram_gb": ram, "storage_gb": gi("storage_gb", 0),
                    "count": gi("count", 1) or 1, "intent_hint": intent_hint})
    return vms

# ============================================================
# STREAMLIT UI
# ============================================================
st.set_page_config(page_title="Multi-Cloud TCO Comparison", layout="wide")
st.title("☁️ Multi-Cloud TCO Comparison Agent")
st.caption(f"Fixed 1-year / 3-year cost comparison across AWS, Azure & GCP — {REGION_LABEL}.")
_credit = f"Built by **{OWNER_NAME}** · {OWNER_TITLE}"
st.markdown(_credit)

# Visible key check — prevents silent "Unavailable" from a missing key
_key_problem = []
if GROQ_KEY.startswith("PASTE_"):
    _key_problem.append("GROQ_KEY")
if INFRACOST_KEY.startswith("PASTE_"):
    _key_problem.append("INFRACOST_KEY")
if _key_problem:
    st.error("⚠️ Missing API key(s): " + ", ".join(_key_problem) +
             ". Open this file and paste your key(s) at the top (the lines marked "
             "'PASTE YOUR TWO API KEYS HERE'), save, and rerun.")
    st.stop()

with st.sidebar:
    st.header("Options")
    term = st.radio("Commitment term", ["1yr", "3yr"],
                    format_func=lambda x: "1 Year" if x == "1yr" else "3 Years")
    proc_choice = st.radio("Processor preference", ["Cheapest", "Intel", "AMD", "ARM"],
                           help="Applied to all VMs. 'Cheapest' compares available processor "
                                "variants per cloud and picks the lowest. Unavailable processors "
                                "fall back to Intel (noted).")
    st.markdown("---")
    st.markdown(f"**Region:** {REGION_LABEL}")
    st.caption(f"AWS: {REGION_DISPLAY['aws']}  \nAzure: {REGION_DISPLAY['azure']}  \nGCP: {REGION_DISPLAY['gcp']}")
    st.markdown("**Pricing:** AWS = live reserved · Azure/GCP = live on-demand × published discount")
    st.markdown("---")
    st.markdown(f"**{OWNER_NAME}**  \n{OWNER_TITLE}")

st.subheader("1. Provide your VMs")
tab_text, tab_file = st.tabs(["✍️ Type / paste", "📎 Upload Excel / CSV"])
with tab_text:
    st.caption("Type naturally, or use SKU names — e.g. *\"3 web 4 vCPU 16GB, 2x Standard_D8s_v5, 1x r7i.2xlarge\"*")
    user_text = st.text_area("VM specifications", height=120,
                             placeholder="3 web servers 4 vCPU 16GB 100GB, 2 databases 8 vCPU 64GB 500GB disk",
                             label_visibility="collapsed")
with tab_file:
    st.caption("Columns (any order, case-insensitive): **name, role, sku, vcpu, ram, storage, count**. "
               "Provide either vcpu+ram OR a sku.")
    uploaded = st.file_uploader("Upload .xlsx / .xls / .csv", type=["xlsx", "xls", "csv"],
                                label_visibility="collapsed")

go = st.button("Compare Prices", type="primary")

if go:
    vms = []
    if uploaded is not None:
        with st.spinner("Reading spreadsheet..."):
            try:
                vms = rows_from_file(uploaded)
            except Exception as e:
                st.error(f"Could not read the file: {e}"); st.stop()
    elif user_text.strip():
        with st.spinner("Parsing your specs..."):
            try:
                vms = groq_parse(user_text).get("vms", [])
            except Exception as e:
                st.error(f"Could not parse input: {e}"); st.stop()
    else:
        st.warning("Type your VMs or upload a spreadsheet first."); st.stop()

    if not vms:
        st.error("No VMs detected. Please rephrase or check your file."); st.stop()

    # resolve any VM still missing specs via SKU decode (Python -> Groq)
    unresolved = []
    for v in vms:
        if not v.get("vcpu") or not v.get("ram_gb"):
            cand = v.get("role") or v.get("name") or ""
            dec = decode_sku(cand) or groq_decode_sku(cand)
            if dec:
                v["vcpu"], v["ram_gb"], v["intent_hint"] = dec
            else:
                unresolved.append(v.get("name") or cand or "a VM")

    st.subheader("2. Understood specifications")
    st.dataframe(pd.DataFrame([{
        "Name": v.get("name"), "Role": v.get("role") or "(unspecified)",
        "vCPU": v.get("vcpu"), "RAM (GB)": v.get("ram_gb"),
        "Storage (GB)": v.get("storage_gb"), "Count": v.get("count"),
    } for v in vms]), use_container_width=True, hide_index=True)

    if unresolved:
        st.warning("Couldn't identify specs for: " + ", ".join(str(u) for u in unresolved) +
                   ". Please add vCPU and RAM (or use a recognized SKU). These show as Unavailable.")

    st.subheader(f"3. Cost comparison — {'1 Year' if term=='1yr' else '3 Years'} committed  ·  {REGION_LABEL}")
    all_rows = []
    progress = st.progress(0.0)
    for i, vm in enumerate(vms):
        all_rows.extend(price_vm(vm, term, proc_choice))
        progress.progress((i + 1) / len(vms))
    progress.empty()

    disp = pd.DataFrame(all_rows).copy()
    disp["Monthly (USD)"] = disp["Monthly (USD)"].apply(
        lambda x: f"${x:,.2f}" if isinstance(x, (int, float)) else x)
    st.dataframe(disp, use_container_width=True, hide_index=True)

    st.subheader("4. Total monthly cost per cloud")
    totals = {}
    for cloud in ["AWS", "Azure", "GCP"]:
        vals = [r["Monthly (USD)"] for r in all_rows
                if r["Cloud"] == cloud and isinstance(r["Monthly (USD)"], (int, float))]
        totals[cloud] = round(sum(vals), 2) if vals else None
    cheapest = min((c for c in totals if totals[c] is not None), key=lambda c: totals[c], default=None)
    cols = st.columns(3)
    for col, cloud in zip(cols, ["AWS", "Azure", "GCP"]):
        t = totals[cloud]
        col.metric(cloud + (" ✅ cheapest" if cloud == cheapest else ""),
                   f"${t:,.2f}/mo" if t is not None else "Unavailable")
    annual = {c: (totals[c] * 12 if totals[c] is not None else None) for c in totals}
    st.caption("Annualized: " + " · ".join(
        f"{c}: ${annual[c]:,.0f}/yr" for c in annual if annual[c] is not None))

    st.markdown("---")
    st.caption(
        f"**Scope & method:** Indicative TCO, compute only, {REGION_LABEL}, USD. AWS uses live "
        "reserved (No Upfront, standard). Azure & GCP use live on-demand reduced by published "
        "commitment discounts (Azure RI, GCP CUD) as an estimate — actual committed pricing may "
        "vary. Each cloud's region shown per row. Excludes storage, egress, networking, OS "
        "licensing, support. Validate against each provider's calculator before quoting."
    )
