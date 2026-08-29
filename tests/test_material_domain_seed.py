import csv,pathlib,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
import build_material_domain_seed as seed
class Tests(unittest.TestCase):
 def setUp(self): self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)
 def tearDown(self): self.tmp.cleanup()
 def paths(self,overrides=None):
  specs={"ubs":("Emails",["a@ubs.com","b@ubs.com"]),"morgan_stanley":("Email",["a@morganstanley.com","b@morganstanley.com"]),"merrill":("Email",["a@ml.com","b@ml.com"]),"raymond_james":("email",["a@raymondjames.com","b@raymondjames.com"]),"alex_brown":("email",["a@alexbrown.com","b@alexbrown.com"])}
  specs.update(overrides or {});out={}
  for slug,(col,values) in specs.items():
   p=self.root/f"{slug}_2026-08-20.csv"
   with p.open("w",encoding="utf-8",newline="") as f:
    w=csv.DictWriter(f,fieldnames=[col]);w.writeheader();w.writerows([{col:v} for v in values])
   out[slug]=p
  return out
 def rules(self,p): return {r["domain"]:r for r in p["rules"]}
 def test_plural_ubs_and_alex_to_rj(self):
  r=self.rules(seed.build_seed(self.paths()))
  self.assertEqual((r["ubs.com"]["audienceCode"],r["ubs.com"]["status"]),("ubs","active"))
  self.assertEqual((r["alexbrown.com"]["audienceCode"],r["alexbrown.com"]["status"]),("rj","active"))
 def test_distinct_email_evidence(self):
  r=self.rules(seed.build_seed(self.paths({"ubs":("Emails",["same@ubs.com","same@ubs.com"])})))
  self.assertEqual((r["ubs.com"]["evidenceCount"],r["ubs.com"]["status"]),(1,"pending"))
 def test_cross_audience_pending(self):
  r=self.rules(seed.build_seed(self.paths({"ubs":("Emails",["a@shared.example","b@shared.example"]),"merrill":("Email",["c@shared.example","d@shared.example"])})))["shared.example"]
  self.assertEqual((r["audienceCode"],r["reason"]),("","cross_roster_ambiguous"))
 def test_anomalies_and_singleton_pending(self):
  r=self.rules(seed.build_seed(self.paths({"morgan_stanley":("Email",["a@morganstanley.co","b@morganstanley.co"]),"merrill":("Email",["a@yext.com","b@yext.com"]),"raymond_james":("email",["a@solo-rj.example"])})))
  for d in ("ms.com","morganstanley.co","yext.com","solo-rj.example"): self.assertEqual(r[d]["status"],"pending")
 def test_deterministic(self):
  paths=self.paths();self.assertEqual(seed.build_seed(paths),seed.build_seed(paths))
if __name__=="__main__": unittest.main()
