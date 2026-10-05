"""Offline tests: replay real TinyFish responses (captured live) through the pipeline."""
import json, os, tempfile
from datetime import datetime
import jobfinder as jf

jf.SEEN_FILE = os.path.join(tempfile.mkdtemp(), "seen.json")

SEARCH = [
 {"date":"7 days ago","title":"Senior Product Designer @ Lendable","url":"https://jobs.ashbyhq.com/lendable/207bb4cf-b1ae-4298-bb17-19dbcdcfa20a","snippet":"Senior Product Designer. Location. London. Hybrid."},
 {"date":"Sep 24, 2026","title":"Staff Product Design @ Gen Digital Inc.","url":"https://jobs.ashbyhq.com/gen-digital/924b1114-d90e-44db-9b8e-5878b0d23128","snippet":"Staff Product Designer Location: London, Hybrid"},
 {"date":"7 days ago","title":"Job Application for Senior Product Designer at Iterable","url":"https://job-boards.greenhouse.io/iterable/jobs/8209782","snippet":"6+ years of product design experience"},
 {"date":"Sep 26, 2026","title":"Sonar - Senior Product Designer","url":"https://jobs.lever.co/sonarsource/8bd7fc1e-1a6f-4d20-b609-8127d61fea10?lever-source=Otta","snippet":"Sonar is looking for a passionate, seasoned product designer."},
 {"date":"Sep 26, 2026","title":"Sonar - Senior Product Designer","url":"https://jobs.lever.co/sonarsource/8bd7fc1e-1a6f-4d20-b609-8127d61fea10","snippet":"duplicate of the above, no tracking param"},
 {"date":"Sep 22, 2026","title":"Software Engineer - Machine Learning (London, United ...","url":"http://job-boards.greenhouse.io/figma/jobs/6166497004?gh_jid=6166497004","snippet":"Figma empowers teams"},
 {"date":"Sep 24, 2026","title":"Product Design Intern - Winter 2027","url":"http://job-boards.greenhouse.io/stackadapt/jobs/4398009009","snippet":"designing digital products"},
]
SONAR_TEXT = "## Senior Product Designer\n\nSan Mateo, CA\n\nProduct Development\n\n### **Who is Sonar?**\n\nOur team operates across hubs in Austin, London, Tokyo.\n\n### Salary Range\n\nThe US base salary range is **$175,000 – $250,000 USD**"
FETCH_OK = [
 {"url":"https://jobs.lever.co/sonarsource/8bd7fc1e-1a6f-4d20-b609-8127d61fea10","final_url":"x","text":SONAR_TEXT},
 {"url":"https://job-boards.greenhouse.io/iterable/jobs/8209782","final_url":"https://job-boards.greenhouse.io/iterable?error=true","text":"# Current openings at Iterable\n\nThere are no current openings."},
 {"url":"https://job-boards.greenhouse.io/stackadapt/jobs/4398009009","final_url":"x","text":"## Product Design Intern\n\nLondon, UK\n\nWe offer visa sponsorship for graduates. Salary £30,000 - £35,000"},
]

class Stub:
    calls = {"search":0,"fetch":0,"agent":0}
    def search(self, q, c=None, r=None): self.calls["search"]+=1; return SEARCH
    def fetch(self, urls):
        self.calls["fetch"]+=1
        errs=[{"url":u,"error":"empty_content"} for u in urls if "ashbyhq" in u]
        return [r for r in FETCH_OK if r["url"] in urls], errs
    def agent(self, url, goal):
        self.calls["agent"]+=1
        return {"location":"London, UK","remote":"hybrid","summary":"Design lead for consumer lending products.","requirements":["6+ years"],"visa_sponsorship":"unknown","salary":"£90,000 - £110,000"}

# unit checks
assert jf.canon("http://www.Jobs.Lever.co/a/b/?lever-source=Otta&utm_x=1") == "https://jobs.lever.co/a/b"
assert jf.canon("http://job-boards.greenhouse.io/figma/jobs/1?gh_jid=1").endswith("gh_jid=1")
assert jf.split_title("Senior Product Designer @ Lendable","x") == ("Senior Product Designer","Lendable","")
assert jf.split_title("Sonar - Senior Product Designer","https://jobs.lever.co/sonarsource/1")[:2] == ("Senior Product Designer","Sonar")
assert jf.split_title("Job Application for Senior Product Designer at Iterable","x")[1] == "Iterable"
assert jf.split_title("Acme hiring UX Designer in London | LinkedIn","x") == ("UX Designer","Acme","London")
assert jf.detect_level("Product Design Intern - Winter 2027") == "intern"
assert jf.detect_level("Staff Product Designer") == "lead" and jf.detect_level("Product Designer") == "mid"
assert jf.visa_status("We are unable to sponsor visas") == "no"
assert jf.visa_status("We offer visa sponsorship") == "yes" and jf.visa_status("nothing") == "unknown"
assert jf.find_salary("range $175,000 – $250,000 USD").startswith("$175,000")
assert jf.is_dead(FETCH_OK[1]) and not jf.is_dead(FETCH_OK[0])
assert jf.parse_posted("7 days ago", datetime(2026,10,5)).day == 28

# full pipeline: basic search
out = jf.run({"role":"product designer","location":"London","sources":"lever,ashby,greenhouse"}, Stub(), log=lambda m: None)
titles = {r["title"]: r for r in out["results"]}
print(json.dumps({t:(r["score"],r["location"],r["verified"],r["reasons"]) for t,r in titles.items()}, indent=1))
st = out["stats"]; print(st)
assert "Senior Product Designer" in [r["title"] for r in out["results"]]            # Ashby via Agent fallback
assert not any("Sonar" == r["company"] for r in out["results"])                      # San Mateo: dropped for location
assert not any(r["company"]=="Iterable" for r in out["results"])                     # dead link: dropped
assert not any("Software Engineer" in r["title"] for r in out["results"])           # irrelevant role
assert st["dropped"]["dead"] == 1 and st["raw_results"] > st["after_dedupe"]        # dedupe collapsed Sonar twin
assert st["endpoint_calls"]["agent"] >= 1 and st["endpoint_calls"]["fetch"] >= 1

# advanced: seniority + visa filters keep only the intern with sponsorship
out2 = jf.run({"role":"product designer","location":"London","levels":"intern","visa":True,"sources":"greenhouse"}, Stub(), log=lambda m: None)
print([ (r["title"], r["visa"], r["salary"], r["new"]) for r in out2["results"]])
assert [r["level"] for r in out2["results"]] == ["intern"] and out2["results"][0]["visa"] == "yes"

# second run marks previously seen jobs as not new
out3 = jf.run({"role":"product designer","location":"London","sources":"lever,ashby,greenhouse"}, Stub(), log=lambda m: None)
assert all(not r["new"] for r in out3["results"])
print("ALL OFFLINE TESTS PASSED")
