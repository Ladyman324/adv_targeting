"""Build the deterministic initial material-domain policy from current rosters."""
import argparse,csv,hashlib,json,pathlib,re,sys
from collections import defaultdict
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from firm_rosters import latest
OUT=ROOT/"api"/"shared"/"material-domain-seed.json"
EMAIL=re.compile(r"(?i)([a-z0-9.!#$%&'*+/=?^_`{|}~-]+)@([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63})")
SOURCES={"ubs":("ubs",("Emails","Email","email")),"morgan_stanley":("mswm",("Email","email")),"merrill":("ml",("Email","email")),"raymond_james":("rj",("email","Email")),"alex_brown":("rj",("email","Email"))}
PENDING={"ms.com":("mswm","institutional_domain_not_evidenced_by_mswm_roster"),"morganstanley.co":("mswm","single_address_likely_malformed_domain"),"yext.com":("ml","scraper_vendor_domain")}
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def display_path(p):
 try: return p.relative_to(ROOT).as_posix()
 except ValueError: return p.name
def roster_date(p):
 m=re.search(r"_(\d{4}-\d{2}-\d{2})\.[^.]+$",p.name)
 if not m: raise ValueError(f"roster filename has no ISO date: {p.name}")
 return m.group(1)
def read_domains(p,columns):
 with p.open("r",encoding="utf-8-sig",newline="") as f:
  reader=csv.DictReader(f);col=next((c for c in columns if c in (reader.fieldnames or [])),None)
  if not col: raise ValueError(f"{p.name} lacks expected email columns")
  out=defaultdict(set)
  for row in reader:
   for m in EMAIL.finditer(str(row.get(col) or "")): out[m.group(2).lower()].add(m.group(0).lower())
 return out
def build_seed(paths):
 if set(paths)!=set(SOURCES): raise ValueError("all five roster sources are required")
 ev=defaultdict(lambda:defaultdict(set));src=defaultdict(set);files=[]
 for slug in sorted(SOURCES):
  audience,columns=SOURCES[slug];p=pathlib.Path(paths[slug]);ds=read_domains(p,columns)
  for d,emails in ds.items(): ev[d][audience].update(emails);src[d].add(slug)
  files.append({"slug":slug,"audienceCode":audience,"path":display_path(p),"date":roster_date(p),"sha256":digest(p),"domainCount":len(ds),"uniqueEmailCount":len(set().union(*ds.values())) if ds else 0})
 for d,(a,_) in PENDING.items(): ev[d][a]
 rules=[]
 for d in sorted(ev):
  counts={a:len(v) for a,v in sorted(ev[d].items())};nonempty=[a for a,n in counts.items() if n];n=sum(counts.values())
  if d in PENDING: a,reason=PENDING[d];status="pending"
  elif len(nonempty)!=1: a,reason,status="","cross_roster_ambiguous","pending"
  else: a=nonempty[0];status="active" if n>=2 else "pending";reason="unique_roster_domain_with_two_email_witnesses" if status=="active" else "single_email_witness"
  rules.append({"domain":d,"audienceCode":a,"status":status,"evidenceCount":n,"evidenceByAudience":counts,"sourceSlugs":sorted(src[d]),"reason":reason})
 active=[r for r in rules if r["status"]=="active"]
 data={"schemaVersion":1,"generatedAt":max(f["date"] for f in files)+"T00:00:00Z","policy":{"defaultAudienceCode":"generic","audienceCodes":["generic","ubs","mswm","ml","rj"],"minimumDistinctEmails":2,"requiresCrossRosterUniqueness":True,"alexBrownAudienceCode":"rj","seedIsInitialPolicyOnly":True},"sourceFiles":files,"rules":rules,"summary":{"activeRuleCount":len(active),"pendingRuleCount":len(rules)-len(active),"activeByAudience":{a:sum(r["audienceCode"]==a for r in active) for a in ("ubs","mswm","ml","rj")}}}
 data["contentHash"]=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(",",":")).encode()).hexdigest();data["seedVersion"]="material-domains-v1-"+data["contentHash"][:12]
 return data
def current_paths():
 out={s:latest(s) for s in SOURCES}
 if any(p is None or p.suffix.lower()!=".csv" for p in out.values()): raise SystemExit("missing current CSV material roster")
 return out
def main():
 ap=argparse.ArgumentParser();ap.add_argument("--output",type=pathlib.Path,default=OUT);ap.add_argument("--check",action="store_true");a=ap.parse_args()
 data=build_seed(current_paths());text=json.dumps(data,indent=2)+"\n"
 if a.check and (not a.output.exists() or a.output.read_text(encoding="utf-8")!=text): return 1
 if not a.check: a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text,encoding="utf-8")
 print(data["seedVersion"],data["summary"]);return 0
if __name__=="__main__": raise SystemExit(main())


