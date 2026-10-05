#!/usr/bin/env python3
"""Careers Finder: a TinyFish-powered job agent.

Search  -> discovers live postings across careers platforms and job portals
Fetch   -> opens each posting, verifies it is still live, reads location/salary/visa text
Agent   -> browses what Fetch cannot read (JavaScript-only pages) and portal result pages
Then it deduplicates, scores every posting against your preferences and ranks them.

Usage:
  export TINYFISH_API_KEY=...            # never hardcode or paste keys into chats
  python jobfinder.py serve              # web UI at http://127.0.0.1:8000
  python jobfinder.py search --role "product designer" --location London --levels senior,mid
"""
import argparse
import concurrent.futures as cf
import difflib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

SEARCH_URL = "https://api.search.tinyfish.ai"
FETCH_URL = "https://api.fetch.tinyfish.ai"
AGENT_URL = "https://agent.tinyfish.ai/v1/automation/run"
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".seen_jobs.json")

# Where Search looks. Direct careers platforms cover thousands of companies each.
ATS = {
    "greenhouse": "site:boards.greenhouse.io OR site:job-boards.greenhouse.io",
    "lever": "site:jobs.lever.co",
    "ashby": "site:jobs.ashbyhq.com",
    "workable": "site:apply.workable.com",
    "linkedin": "site:linkedin.com/jobs/view",
    "reed": "site:reed.co.uk/jobs",
}
AGGREGATORS = {"linkedin", "reed", "indeed"}
NO_FETCH_HOSTS = ("linkedin.com", "indeed.com")  # login walls: do not fetch these

DEFAULTS = dict(
    role="", location="", country="", keywords=[], exclude=[], levels=[], visa=False,
    remote_ok=True, max_age_days=30, sources=list(ATS), agent_portals=False,
    fetch_top=25, agent_fallback=3, min_score=30,
)

# --------------------------------------------------------------------------- TinyFish client


class TF:
    """Thin wrapper over the three TinyFish REST endpoints (X-API-Key auth)."""

    def __init__(self, key):
        if not key:
            raise RuntimeError("Set TINYFISH_API_KEY (create one at agent.tinyfish.ai/api-keys).")
        self.h = {"X-API-Key": key}
        self.calls = {"search": 0, "fetch": 0, "agent": 0}
        self._lock = threading.Lock()

    def _count(self, k):
        with self._lock:
            self.calls[k] += 1

    def search(self, query, country=None, recency_minutes=None):
        import requests
        params = {"query": query}
        if country:
            params["location"] = country
        if recency_minutes:
            params["recency_minutes"] = recency_minutes
        for attempt in range(3):
            self._count("search")
            r = requests.get(SEARCH_URL, params=params, headers=self.h, timeout=30)
            if r.status_code == 429:  # free tier is rate limited: back off and retry
                time.sleep(4 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json().get("results", [])
        return []

    def fetch(self, urls):
        import requests
        results, errors = [], []
        for i in range(0, len(urls), 10):  # API accepts up to 10 URLs per call
            chunk = urls[i:i + 10]
            self._count("fetch")
            r = requests.post(FETCH_URL, headers=self.h, timeout=150,
                              json={"urls": chunk, "format": "markdown", "ttl": 0})
            if r.ok:
                j = r.json()
                results += j.get("results", [])
                errors += j.get("errors", [])
            else:
                errors += [{"url": u, "error": f"http_{r.status_code}"} for u in chunk]
        return results, errors

    def agent(self, url, goal):
        import requests
        self._count("agent")
        r = requests.post(AGENT_URL, headers=self.h, json={"url": url, "goal": goal}, timeout=180)
        r.raise_for_status()
        j = r.json()
        res = j.get("result", j.get("resultJson", j))
        if isinstance(res, str):
            m = re.search(r"[\[{].*[\]}]", res, re.S)
            try:
                res = json.loads(m.group(0)) if m else {}
            except ValueError:
                res = {}
        return res if isinstance(res, dict) else {"jobs": res}


# --------------------------------------------------------------------------- parsing helpers

TRACK_KEYS = {"gh_src", "lever-source", "lever-origin", "ref", "source", "src", "trk", "fbclid",
              "gclid", "refid", "trackingid", "mc_cid", "mc_eid", "lang"}


def canon(url):
    """Canonical URL: https, no www, no tracking params, no trailing slash."""
    s = urlsplit(url.strip())
    host = s.netloc.lower().removeprefix("www.")
    q = [(k, v) for k, v in parse_qsl(s.query)
         if not (k.lower().startswith("utm_") or k.lower() in TRACK_KEYS)]
    return urlunsplit(("https", host, s.path.rstrip("/"), urlencode(q), ""))


def tokens(text):
    out = set()
    for t in re.findall(r"[a-z0-9+#]+", text.lower()):
        if t in {"and", "the", "of", "for", "a", "an", "in", "at", "to"}:
            continue
        for suf in ("ing", "ers", "er", "s"):
            if t.endswith(suf) and len(t) - len(suf) >= 4:
                t = t[:-len(suf)]
                break
        out.add(t)
    return out


def slug_company(url):
    s = urlsplit(url)
    parts = [x for x in s.path.split("/") if x]
    if any(h in s.netloc for h in ("greenhouse", "lever", "ashby", "workable")) and parts:
        return re.sub(r"[-_]+", " ", parts[0]).title()
    return ""


def related(a, b):
    na, nb = (re.sub(r"[^a-z0-9]", "", x.lower()) for x in (a, b))
    return len(na) >= 3 and len(nb) >= 3 and (na in nb or nb in na)


def split_title(title, url):
    """Return (title, company, location) from the page titles search engines show."""
    t = re.sub(r"\s*\|\s*LinkedIn$", "", title.strip())
    if m := re.match(r"(.+?) hiring (.+?) in (.+)$", t):
        return m.group(2), m.group(1), m.group(3)
    if m := re.match(r"Job Application for (.+?) at (.+)$", t, re.I):
        return m.group(1), m.group(2), ""
    if " @ " in t:
        a, b = t.rsplit(" @ ", 1)
        return a, b, ""
    slug = slug_company(url)
    if " - " in t and slug:
        a, b = t.split(" - ", 1)
        if related(slug, a):
            return b, a, ""
        if related(slug, b):
            return a, b, ""
    return t, slug, ""


def parse_posted(s, now):
    s = (s or "").strip().lower()
    if not s:
        return None
    if m := re.search(r"(\d+)\s*(hour|day|week|month)s?\s*ago", s):
        n, unit = int(m.group(1)), m.group(2)
        return now - timedelta(hours={"hour": 1, "day": 24, "week": 168, "month": 720}[unit] * n)
    if "today" in s or "just now" in s or "hour" in s:
        return now
    for fmt in ("%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:12].strip() if fmt == "%b %d, %Y" else s[:10], fmt)
        except ValueError:
            pass
    return None


def detect_level(title):
    t = title.lower()
    if re.search(r"\b(intern|internship|placement|graduate|apprentice|working student)\b", t):
        return "intern"
    if re.search(r"\b(junior|jr|entry|associate|assistant)\b", t):
        return "junior"
    if re.search(r"\b(lead|staff|principal|head of|director|vp|manager|chief)\b", t):
        return "lead"
    if re.search(r"\b(senior|sr)\b", t):
        return "senior"
    return "mid"


def guess_location(text, title):
    for ln in [x.strip(" *#|") for x in text.split("\n")[:14]]:
        low = ln.lower()
        if not 2 < len(ln) < 70 or low in title.lower() or title.lower() in low:
            continue
        if re.search(r"\b(apply|salary|about|who|what)\b", low):
            continue
        if ("," in ln and not ln.endswith(".")) or re.search(r"\b(remote|hybrid|on-?site)\b", low):
            return ln
    return ""


def find_salary(text):
    m = re.search(r"([£$€])\s?(\d{2,3}(?:,\d{3})+|\d{2,3}k)\s*(?:-|–|—|to)\s*[£$€]?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}k)",
                  text, re.I)
    return f"{m.group(1)}{m.group(2)} - {m.group(1)}{m.group(3)}" if m else ""


def visa_status(text):
    t = text.lower()
    if re.search(r"(unable|not able|cannot|can't|do not|don't|does not|won't|will not|not)\s+(\w+\s+){0,3}sponsor"
                 r"|no\s+(visa\s+)?sponsorship|sponsorship\s+is\s+not", t):
        return "no"
    if re.search(r"sponsor|skilled worker visa|relocation (support|package|assistance)", t):
        return "yes"
    return "unknown"


DEAD_PHRASES = ("no current openings", "no longer accepting", "job not found", "position has been filled",
                "no longer available", "this job is closed", "page not found", "posting has expired",
                "job has expired", "no longer open")


def is_dead(res):
    if "error=true" in (res.get("final_url") or ""):
        return True
    head = (res.get("text") or "")[:600].lower()
    return any(p in head for p in DEAD_PHRASES)


def norm_company(c):
    return re.sub(r"\b(inc|ltd|limited|llc|plc|gmbh|corp)\b|[^a-z0-9]", "", c.lower())


# --------------------------------------------------------------------------- dedupe, score

def dedupe(cands):
    """Merge exact canonical-URL duplicates, then near-duplicates (same company, similar title)."""
    by_url = {}
    for c in cands:
        c["url"] = canon(c["url"])
        if c["url"] in by_url:
            by_url[c["url"]]["sources"] = sorted(set(by_url[c["url"]]["sources"] + c["sources"]))
        else:
            by_url[c["url"]] = c
    kept = []
    for c in by_url.values():
        twin = next((k for k in kept if norm_company(k["company"]) == norm_company(c["company"])
                     and norm_company(c["company"]) and difflib.SequenceMatcher(
                         None, k["title"].lower(), c["title"].lower()).ratio() > 0.92), None)
        if not twin:
            kept.append(c)
            continue
        twin["sources"] = sorted(set(twin["sources"] + c["sources"]))
        if twin["source"] in AGGREGATORS and c["source"] not in AGGREGATORS:  # prefer the direct link
            for k in ("url", "source", "snippet"):
                twin[k] = c[k]
    return kept


def evaluate(c, p, now):
    """Score one posting 0-100 against preferences. Returns (score, reasons) or None to drop."""
    reasons = []
    title, text = c["title"], c.get("text", "")
    role_t = tokens(p["role"])
    cov = len(role_t & tokens(title)) / len(role_t) if role_t else 1
    if cov < 0.5:
        return None
    score = 45 * cov + (10 if p["role"].lower() in title.lower() else 0)
    reasons.append(f"title matches {int(cov * 100)}% of '{p['role']}'")

    hay = " ".join([title, c.get("snippet", ""), text]).lower()
    if any(x.lower() in hay for x in p["exclude"] if x):
        return None
    hits = [k for k in p["keywords"] if k.lower() in hay]
    if hits:
        score += min(20, 8 * len(hits))
        reasons.append("keywords: " + ", ".join(hits))

    loc_line = c.get("loc_line") or guess_location(text, title)
    c["location"] = loc_line or c.get("location", "")
    head = " ".join([title, c.get("snippet", ""), text[:350], loc_line]).lower()
    # an explicit location line outranks loose mentions elsewhere (e.g. "we have hubs in London")
    auth = f"{title} {loc_line}".lower() if loc_line else head
    toks = [t for t in re.findall(r"[a-z]+", p["location"].lower())
            if t not in {"united", "kingdom", "the", "of", "area", "greater"}]
    remote = bool(re.search(r"\bremote\b", auth))
    if not toks or any(re.search(rf"\b{re.escape(t)}\b", auth) for t in toks):
        score += 15
        reasons.append("location matches")
    elif remote and p["remote_ok"]:
        score += 10
        reasons.append("remote")
    elif loc_line:
        return None  # page states a different location: drop it
    else:
        score += 3
        reasons.append("location unverified")
    c["work_mode"] = "remote" if remote else ("hybrid" if "hybrid" in head else "")

    level = detect_level(title)
    c["level"] = level
    if p["levels"]:
        if level not in p["levels"]:
            return None
        score += 10
        reasons.append(f"level: {level}")

    posted = c.get("posted_dt")
    if posted:
        age = (now - posted).days
        if age > p["max_age_days"]:
            return None
        score += 10 if age <= 7 else 5 if age <= 14 else 0
        c["posted"] = f"{age}d ago" if age else "today"

    c["visa"] = visa_status(text or c.get("snippet", ""))
    if p["visa"]:
        if c["visa"] == "no":
            return None
        if c["visa"] == "yes":
            score += 10
            reasons.append("mentions visa sponsorship")
    c["salary"] = c.get("salary") or find_salary(text)
    return min(100, int(score)), reasons


# --------------------------------------------------------------------------- pipeline

PORTAL_GOAL = ("This is a job search results page. Extract up to 15 job listings and return JSON like "
               '{"jobs":[{"title":"","company":"","location":"","posted":"","url":""}]} where url is the '
               "absolute link to the job. Do not log in and do not apply. If a login wall or CAPTCHA blocks "
               'you, return {"jobs":[],"blocked":true}.')
READ_GOAL = ("Read this job posting and return JSON with keys: title, company, location, salary, "
             "remote (remote/hybrid/onsite), summary (max 80 words), requirements (list of strings), "
             "visa_sponsorship (yes/no/unknown). Do not apply or log in.")


def norm_prefs(d):
    p = dict(DEFAULTS)
    p.update({k: v for k, v in d.items() if k in DEFAULTS and v not in (None, "")})
    for k in ("keywords", "exclude", "levels", "sources"):
        if isinstance(p[k], str):
            p[k] = [x.strip().lower() if k in ("levels", "sources") else x.strip()
                    for x in p[k].split(",") if x.strip()]
    for k in ("max_age_days", "fetch_top", "agent_fallback", "min_score"):
        p[k] = int(p[k])
    p["sources"] = [s for s in p["sources"] if s in ATS] or list(ATS)
    return p


def portal_urls(p):
    r, l = quote_plus(p["role"]), quote_plus(p["location"])
    return {
        "linkedin": f"https://www.linkedin.com/jobs/search/?keywords={r}&location={l}&f_TPR=r604800&sortBy=DD",
        "reed": f"https://www.reed.co.uk/jobs?keywords={r}&location={l}&datecreatedoffset=LastWeek",
    }


def run(prefs, tf, log=print):
    p = norm_prefs(prefs)
    if not p["role"]:
        raise ValueError("role is required")
    now, t0, drop = datetime.utcnow(), time.time(), {"irrelevant": 0, "dead": 0, "filtered": 0}
    cands = []

    # 1) SEARCH: discover postings, one query per platform so every source is represented
    def search_src(src):
        q = " ".join(x for x in [p["role"], p["location"], *p["keywords"][:3], ATS[src]] if x)
        try:
            rows = tf.search(q, p["country"] or None, p["max_age_days"] * 1440)
        except Exception as e:  # one failing source must not sink the run
            log(f"  search[{src}] failed: {e}")
            return []
        out = []
        for r in rows:
            title, company, loc = split_title(r.get("title", ""), r["url"])
            out.append(dict(title=title, company=company, url=r["url"], source=src, sources=[src],
                            snippet=r.get("snippet", ""), loc_line=loc, posted_dt=parse_posted(r.get("date"), now)))
        return out

    log(f"Search: {len(p['sources'])} platforms")
    with cf.ThreadPoolExecutor(3) as ex:
        for rows in ex.map(search_src, p["sources"]):
            cands += rows

    # 2) AGENT on portal result pages (optional; slower and uses credits)
    if p["agent_portals"]:
        urls = portal_urls(p)

        def portal(name):
            try:
                res = tf.agent(urls[name], PORTAL_GOAL)
            except Exception as e:
                log(f"  agent[{name}] failed: {e}")
                return []
            return [dict(title=j.get("title", ""), company=j.get("company", ""), url=j["url"], source=name,
                         sources=[name], snippet="", loc_line=j.get("location", ""),
                         posted_dt=parse_posted(j.get("posted"), now))
                    for j in (res.get("jobs") or []) if j.get("url") and j.get("title")]
        log("Agent: browsing portal result pages")
        with cf.ThreadPoolExecutor(2) as ex:
            for rows in ex.map(portal, [s for s in p["sources"] if s in urls]):
                cands += rows

    found = len(cands)
    cands = dedupe(cands)
    log(f"{found} raw results -> {len(cands)} after dedupe")

    # cheap pre-filter so we only spend Fetch calls on plausible postings
    role_t, shortlist = tokens(p["role"]), []
    for c in cands:
        cov = len(role_t & tokens(c["title"])) / len(role_t) if role_t else 1
        stale = c["posted_dt"] and (now - c["posted_dt"]).days > p["max_age_days"]
        if cov < 0.5 or stale or (p["levels"] and detect_level(c["title"]) not in p["levels"]):
            drop["irrelevant"] += 1
            continue
        c["_pre"] = cov * 50 + (10 if c["source"] not in AGGREGATORS else 0)
        shortlist.append(c)
    shortlist.sort(key=lambda c: -c["_pre"])
    shortlist = shortlist[:p["fetch_top"]]

    # 3) FETCH: open each posting live; detect dead links; read location/salary/visa text
    fetchable = [c for c in shortlist if not any(h in c["url"] for h in NO_FETCH_HOSTS)]
    log(f"Fetch: reading {len(fetchable)} postings live")
    ok, errs = tf.fetch([c["url"] for c in fetchable]) if fetchable else ([], [])
    got = {r["url"]: r for r in ok}
    empty = {e["url"] for e in errs if e.get("error") == "empty_content"}
    for c in fetchable:
        r = got.get(c["url"])
        if r:
            if is_dead(r):
                c["dead"] = True
            else:
                c["text"], c["verified"] = r.get("text") or "", True
                if not c["posted_dt"] and r.get("published_date"):
                    c["posted_dt"] = parse_posted(r["published_date"], now)

    # 4) AGENT fallback: JavaScript-only pages that Fetch could not render
    todo = [c for c in fetchable if c["url"] in empty][:p["agent_fallback"]]
    if todo:
        log(f"Agent: browsing {len(todo)} JavaScript-only postings")

        def read(c):
            try:
                return c, tf.agent(c["url"], READ_GOAL)
            except Exception as e:
                log(f"  agent read failed: {e}")
                return c, {}
        with cf.ThreadPoolExecutor(3) as ex:
            for c, d in ex.map(read, todo):
                if d:
                    c["loc_line"] = d.get("location") or c["loc_line"]
                    c["salary"] = d.get("salary") or ""
                    vs = str(d.get("visa_sponsorship", "")).lower()
                    c["text"] = " ".join([str(d.get("location", "")), str(d.get("remote", "")),
                                          str(d.get("summary", "")), " ".join(map(str, d.get("requirements") or [])),
                                          "visa sponsorship available" if vs == "yes" else
                                          "no visa sponsorship" if vs == "no" else ""])
                    c["verified"] = True

    # 5) score, filter, rank
    seen = json.load(open(SEEN_FILE)) if os.path.exists(SEEN_FILE) else {}
    results = []
    for c in shortlist:
        if c.get("dead"):
            drop["dead"] += 1
            continue
        ev = evaluate(c, p, now)
        if not ev or ev[0] < p["min_score"]:
            drop["filtered"] += 1
            continue
        results.append(dict(
            title=c["title"], company=c["company"], location=c.get("location", ""), url=c["url"],
            sources=c["sources"], score=ev[0], reasons=ev[1], new=c["url"] not in seen,
            salary=c.get("salary", ""), level=c.get("level", ""), work_mode=c.get("work_mode", ""),
            visa=c.get("visa", "unknown"), posted=c.get("posted", ""), verified=bool(c.get("verified"))))
    results.sort(key=lambda r: (-r["score"], not r["new"]))
    for r in results:
        seen.setdefault(r["url"], now.isoformat())
    seen = {u: d for u, d in seen.items() if (now - datetime.fromisoformat(d)).days < 60}
    json.dump(seen, open(SEEN_FILE, "w"))
    return dict(results=results, stats=dict(
        endpoint_calls=tf.calls, raw_results=found, after_dedupe=len(cands), dropped=drop,
        seconds=round(time.time() - t0, 1)))


# --------------------------------------------------------------------------- web UI + CLI

UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, open(UI_FILE, encoding="utf-8").read(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/search":
            return self._send(404, "{}", "application/json")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            out = run(body, TF(os.environ.get("TINYFISH_API_KEY", "")), log=lambda m: print(m, flush=True))
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}
        self._send(200, json.dumps(out), "application/json")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description="TinyFish careers finder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="start the local web UI")
    sv.add_argument("--port", type=int, default=8000)
    s = sub.add_parser("search", help="run one search from the terminal")
    s.add_argument("--role", required=True)
    s.add_argument("--location", default="")
    s.add_argument("--country", default="", help="2-letter code to geo-target search, e.g. GB")
    s.add_argument("--keywords", default="")
    s.add_argument("--exclude", default="")
    s.add_argument("--levels", default="", help="intern,junior,mid,senior,lead")
    s.add_argument("--sources", default="", help=",".join(ATS))
    s.add_argument("--visa", action="store_true", help="needs visa sponsorship")
    s.add_argument("--agent-portals", action="store_true", help="also browse LinkedIn/Reed result pages")
    s.add_argument("--json", dest="json_out", help="write results to this file")
    a = ap.parse_args()
    if a.cmd == "serve":
        print(f"Careers Finder running at http://127.0.0.1:{a.port}")
        ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
        return
    prefs = {k: v for k, v in vars(a).items() if k not in ("cmd", "json_out")}
    out = run(prefs, TF(os.environ.get("TINYFISH_API_KEY", "")))
    for r in out["results"]:
        flag = "NEW " if r["new"] else ""
        print(f"{r['score']:>3}  {flag}{r['title']} @ {r['company']} | {r['location']} {r['salary']}\n     {r['url']}")
    print(json.dumps(out["stats"]))
    if a.json_out:
        json.dump(out, open(a.json_out, "w"), indent=2)


if __name__ == "__main__":
    main()
