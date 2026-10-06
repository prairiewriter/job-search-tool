#!/usr/bin/env python3
"""
job_search.py  -  daily remote/hybrid technical-writing job digest

Sources
  * Adzuna - keyword search, plus a remote-flavoured pass and an optional
    local-commute pass around one city
  * We Work Remotely, Remotive, RemoteOK
  * Company career boards via public Greenhouse / Lever / Ashby / Workable /
    Teamtailor / SmartRecruiters APIs (see COMPANY_BOARDS)

Pipeline
  fetch -> title/company filter -> location filter -> freshness -> scam filter
  -> pay classification -> de-duplicate -> "new since last run" (SQLite) -> report/email

A job is only remembered as "seen" AFTER the email has been sent successfully,
and every run also saves a report to jobs_report_YYYY-MM-DD.html (auto-deleted
after REPORT_RETENTION_DAYS). Credentials live in secrets.env next to this
script - never paste them into this file.

======================================================================
BEFORE YOU RUN THIS: read README.md, then edit the "PERSONALIZE" block
just below. Everything in that block is specific to YOU - your country,
your commute radius, your pay floor, your title/company preferences.
Everything after it is shared logic that should work for any technical
writer without changes.
======================================================================
"""

import argparse
import os
import re
import smtplib
import sqlite3
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path

HERE = Path(__file__).parent

# ==============================================================================
# 1. LOAD SECRETS FROM secrets.env
# ==============================================================================
env_file = HERE / "secrets.env"
if env_file.exists():
    with open(env_file, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip().strip("'\"")

# ==============================================================================
# 2A. PERSONALIZE  -  edit every value in this block for your own situation.
#     Nothing below this block needs to change for normal use.
# ==============================================================================

# Adzuna operates per-country. Common codes: ca (Canada), us, gb, au, de, fr,
# nl, ie, sg, in, etc. Pick the country whose job market you want searched.
# Full list: https://developer.adzuna.com/  (see "Country" in the docs)
COUNTRY_CODE = "ca"

# Words/phrases that mean "I, personally, am eligible for this job" when they
# appear in a REMOTE listing's location text. Include your own country, any
# common alternate names for it, your city/province/state if listings might
# name it directly, and the usual "no specific country required" phrases.
# These are matched as lowercase substrings, so partial words are fine.
OK_REGIONS = ["canada", "north america", "north-america",
              "americas", "amer", "worldwide", "anywhere", "global", "international",
              "any location"]
# The mirror image: phrases that mean "NOT eligible", checked only for REMOTE
# listings (an on-site listing is excluded on location regardless). Add your
# own exclusions if a particular country/region keeps showing up that you
# can't work from.
US_ONLY = ["usa only", "us only", "u.s. only", "united states only", "us-only",
           "remote - us", "remote (us", "remote, us", "remote us", "us remote",
           "us-remote", "us-based", "us based", "must reside in the us",
           "must be located in the us"]

# HYBRID roles: cities within a commute you're actually willing to make,
# lowercase. A hybrid job in one of these cities is shown with no comment.
# Any OTHER hybrid job found by Adzuna (which is itself scoped to
# COUNTRY_CODE) is still shown, just flagged with its city so you can judge
# the commute yourself, rather than silently hidden.
INCLUDE_HYBRID = True
HYBRID_CITIES = ["toronto", "mississauga"]   # EXAMPLE - replace with your own commutable cities

# Adzuna's local-radius pass: set to "" to disable this pass entirely (you'll
# still get hybrid results from the plain keyword searches, just without the
# extra geography-targeted pass). Distance is in kilometres.
LOCAL_WHERE = "Toronto, Ontario"   # EXAMPLE - replace with your city, or "" to turn this pass off
LOCAL_DISTANCE_KM = 40

# The lowest annual pay you'd consider. Listed hourly pay is converted to a
# yearly figure automatically (see HOURS_PER_YEAR below), so set this in
# whatever your local annual-salary currency/expectation is.
MIN_SALARY_THRESHOLD = 70000   # EXAMPLE - set your own minimum
HOURS_PER_YEAR = 2000          # used only to convert hourly rates to /yr

# Companies you never want to see (staffing mills, past bad experiences,
# recruiters who repost the same role everywhere). Lowercase, partial match.
EXCLUDED_COMPANIES = []   # e.g. ["some staffing agency"]

# How your own country is named in report headings and log messages.
HOME_LABEL = "Canada"
# Lowercase words that specifically mean "your own country" (not "worldwide"
# or "North America" - just your country/province/state by name). Used in a
# couple of places where the script needs to recognize your specific country
# rather than any OK_REGIONS match.
HOME_COUNTRY_TERMS = ["canada"]   # add your province/state if needed

# ==============================================================================
# 2B. SHARED DEFAULTS  -  reasonable for most technical writers as-is.
#     Adjust only if your own title/search preferences differ.
# ==============================================================================
DB_FILE = HERE / "job_history.db"   # NEVER auto-deleted: this is what makes
                                     # "only new jobs" work across days.
REPORT_RETENTION_DAYS = 7           # old jobs_report_*.html files older than
                                     # this are deleted automatically.

SEARCH_QUERIES = [
    "Technical Writer",
    "Technical Writer/ Editor",
    "Technical Documentation Specialist",
    "API Writer",
    "Documentation Engineer",
    "Information Developer",
    "Information Architect",
]

# Adzuna passes: plain query, "<query> remote", and a local search around LOCAL_WHERE.
ADZUNA_ALSO_SEARCH_REMOTE = True
ADZUNA_MAX_PAGES = 3                 # 50 results per page; stops early when a page isn't full

REQUIRED_TITLE_KEYWORDS = [
    "technical writer", "tech writer", "documentation",
    "technical editor", "writer/ editor", "writer / editor",
    "technical author", "api writer", "docs engineer",
    "documentation engineer", "programmer writer", "developer educator",
    "information developer", "information architect", "knowledge base specialist",
]

EXCLUDED_TITLE_KEYWORDS = [
    "intern", "internship", "co-op", "coop", "buyer", "product owner", "marketing", "scientist",
    "proposal", "accountant", "recruiter", "sales", "solutions specialist",
    "content engineer", "content designer", "copywriter",
    "content marketing", "developer content writer",
]

SCAM_PHRASES = [
    "no prior ai experience is required",
    "no prior experience is required",
    "no prio is required",
]

MAX_JOB_AGE_DAYS = 30

# --- Company career boards (public APIs, no key needed) -----------------------
# This is a STARTING LIST, not a definitive one - add companies YOU care about.
# More companies = more results; this is the single biggest lever for finding
# more jobs. To add one, open one of its job postings and read the URL:
#   boards.greenhouse.io/SLUG or job-boards.greenhouse.io/SLUG -> "greenhouse"
#   jobs.lever.co/SLUG                                          -> "lever"
#   jobs.ashbyhq.com/SLUG                                       -> "ashby"
#   apply.workable.com/SLUG                                     -> "workable"
#   SLUG.teamtailor.com (or a custom domain shown in the address bar)
#                                                                -> "teamtailor"
#   jobs.smartrecruiters.com/SLUG                                -> "smartrecruiters"
# A wrong slug just prints "not found" when you run the script - delete it.
# Not every company uses one of these six platforms; some run a fully custom
# careers portal that has no public API to connect to. If a company you want
# isn't fetchable this way, the console will make that obvious (a 404, or it
# simply won't be in COMPANY_BOARDS at all) - check that company's site by hand.
COMPANY_BOARDS = {
    "greenhouse": [
        "stripe", "gitlab", "datadog", "cloudflare", "twilio", "mongodb", "okta",
        "airtable", "asana", "figma", "dropbox", "coinbase", "vercel", "anthropic",
        "elastic", "samsara", "brex", "gusto", "mixpanel", "fivetran", "pagerduty",
    ],
    "lever": ["spotify"],
    "ashby": ["openai", "ramp", "linear", "notion", "supabase", "posthog",
              "clickhouse", "cohere", "trustly"],
    # Workable slug = the part after apply.workable.com/
    "workable": ["genetec-inc"],
    # Teamtailor entry = the full career-site host (subdomain can be custom,
    # e.g. "vention.na.teamtailor.com" rather than plain "vention.teamtailor.com" -
    # check the address bar on an actual job posting to be sure).
    "teamtailor": ["vention.na.teamtailor.com"],
    # SmartRecruiters slug = the part after jobs.smartrecruiters.com/
    "smartrecruiters": [],
}
# Companies whose careers pages I checked and could NOT place on a public-API
# platform (custom-built portals). Left here as a note, not fetched:
#   Coveo    - custom portal at careers.coveo.com
#   Telesat  - custom portal (careers.telesat.com), not a recognized standard ATS

USER_AGENT_HEADER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# ==============================================================================
# 3. SMALL HELPERS
# ==============================================================================
STATS = {}   # per-source diagnostics: {"Adzuna": {"fetched": 120, "errors": [...]}}
NOT_FOUND = []   # company boards that returned HTTP 404 (wrong slug): delete these


def norm_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def strip_tags(text, repl=" "):
    return norm_ws(re.sub(r"<[^>]+>", repl, text or ""))


def normalize_salary(v):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v * HOURS_PER_YEAR if v < 1000 else v


def format_pay(sal_min, sal_max, currency="", note=""):
    cur = f" {currency}" if currency else ""
    if sal_min is not None and sal_max is not None:
        text = f"${sal_min:,.0f} - ${sal_max:,.0f}/yr{cur}"
    elif sal_min is not None:
        text = f"From ${sal_min:,.0f}/yr{cur}"
    elif sal_max is not None:
        text = f"Up to ${sal_max:,.0f}/yr{cur}"
    else:
        return "Pay not listed"
    return text + note


def make_job(source, jid, title, company, location, url, description="", created="",
             is_remote=True, is_hybrid=False, sal_min=None, sal_max=None,
             currency="", pay_text=None, pay_estimated=False):
    sal_min, sal_max = normalize_salary(sal_min), normalize_salary(sal_max)
    note = " (estimate, not employer-listed)" if pay_estimated else ""
    return {
        "id": f"{source}:{jid}",
        "source": source,
        "title": strip_tags(title, ""),
        "company": norm_ws(company) or "Unknown Company",
        "location": norm_ws(location),
        "url": url or "",
        "description": strip_tags(description),
        "created_date": created or "",
        "is_remote": is_remote,
        "is_hybrid": is_hybrid,
        "min_salary": sal_min,
        "max_salary": sal_max,
        "pay_text": pay_text or format_pay(sal_min, sal_max, currency, note),
        "pay_estimated": pay_estimated,
        "loc_flag": "",
    }


def parse_date(raw):
    """Best-effort parse of ISO / RFC-2822 dates. Returns aware datetime or None."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    for parse in (parsedate_to_datetime,
                  lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))):
        try:
            dt = parse(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def cleanup_old_reports(keep_days=REPORT_RETENTION_DAYS):
    """Delete saved jobs_report_*.html files older than keep_days. Never
    touches DB_FILE or secrets.env - only the disposable HTML backups."""
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = []
    for f in HERE.glob("jobs_report_*.html"):
        try:
            file_date = datetime.strptime(f.stem.replace("jobs_report_", ""), "%Y-%m-%d")
        except ValueError:
            continue   # unexpected filename shape: leave it alone rather than guess
        if file_date < cutoff:
            f.unlink()
            removed.append(f.name)
    if removed:
        print(f"Cleaned up {len(removed)} old report file(s): {', '.join(removed)}")


def run_source(label, fn, *args, item=None):
    """Run one fetch; never crash the whole run; record what happened."""
    tag = f"{label}/{item}" if item else label
    entry = STATS.setdefault(label, {"fetched": 0, "errors": []})
    try:
        jobs = fn(*args)
        entry["fetched"] += len(jobs)
        print(f"  {tag}: {len(jobs)} jobs")
        return jobs
    except requests.HTTPError as e:
        code = e.response.status_code if getattr(e, "response", None) is not None else "?"
        hint = " (check the company slug)" if code == 404 else ""
        if code == 404:
            NOT_FOUND.append(tag)
        msg = f"{tag}: HTTP {code}{hint}"
    except Exception as e:
        msg = f"{tag}: {type(e).__name__}: {str(e)[:100]}"
    entry["errors"].append(msg)
    print(f"  {msg}   <-- FAILED")
    return []


# ==============================================================================
# 4. DATABASE (state) - read and write are separate on purpose
# ==============================================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS seen_jobs (
                        id TEXT PRIMARY KEY, title TEXT, company TEXT, first_seen TEXT)""")
    conn.commit()
    conn.close()


def get_new_unseen_jobs(jobs):
    """Read-only: returns jobs none of whose ids have been reported before."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    seen = {row[0] for row in conn.execute("SELECT id FROM seen_jobs")}
    conn.close()
    return [j for j in jobs if not any(i in seen for i in j["all_ids"])]


def mark_seen(jobs):
    """Call ONLY after the report was delivered."""
    init_db()   # the table may not exist yet (e.g. first run with --force)
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    conn.executemany(
        "INSERT OR IGNORE INTO seen_jobs (id, title, company, first_seen) VALUES (?,?,?,?)",
        [(i, j["title"], j["company"], today) for j in jobs for i in j["all_ids"]])
    conn.commit()
    conn.close()


# ==============================================================================
# 5. FETCHERS - each returns normalized jobs and RAISES on failure
#    (run_source records the failure). None of them filter by location;
#    that happens in one place: is_acceptable_location().
# ==============================================================================
def _adzuna_page(query, where, page):
    url = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY_CODE}/search/{page}"
    params = {"app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY, "results_per_page": 50,
              "what": query, "sort_by": "date", "max_days_old": MAX_JOB_AGE_DAYS,
              "content-type": "application/json"}
    if where:
        params.update({"where": where, "distance": LOCAL_DISTANCE_KM})
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=USER_AGENT_HEADER, timeout=15)
            if resp.status_code == 503:
                last_err = RuntimeError("HTTP 503 from Adzuna")
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json().get("results", [])
        except requests.HTTPError:
            raise
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise last_err or RuntimeError("Adzuna failed")


def fetch_adzuna_jobs(query, where=None):
    out = []
    for page in range(1, ADZUNA_MAX_PAGES + 1):
        results = _adzuna_page(query, where, page)
        for r in results:
            title = strip_tags(r.get("title"), "")
            loc_name = (r.get("location") or {}).get("display_name", HOME_LABEL)
            desc = strip_tags(r.get("description"))
            low = f"{title} {desc} {loc_name}".lower()
            remote_said = any(w in low for w in ("remote", "work from home", "telecommut"))
            is_hybrid_txt = "hybrid" in low
            is_local = any(c in loc_name.lower() for c in HYBRID_CITIES)
            # A country-level location ("Canada", no city) usually means a Canada-wide
            # posting, and Adzuna's short snippet often omits the word "remote".
            # Keep those, flagged, instead of silently treating them as on-site.
            country_wide = loc_name.strip().lower() == HOME_LABEL.lower()
            location = loc_name   # eligibility is decided later by region_status()/loc_flag,
                                   # never by annotating this string at fetch time
            job = make_job(
                "Adzuna", r.get("id", ""), title,
                (r.get("company") or {}).get("display_name", ""), location,
                r.get("redirect_url", ""), desc, r.get("created", ""),
                is_remote=remote_said or country_wide,
                is_hybrid=is_hybrid_txt or (is_local and not remote_said),
                sal_min=r.get("salary_min"), sal_max=r.get("salary_max"), currency="CAD",
                pay_estimated=str(r.get("salary_is_predicted", "0")) == "1")
            if country_wide and not remote_said:
                job["note"] = f"{HOME_LABEL}-wide listing: confirm it is remote"
            out.append(job)
        if len(results) < 50:
            break
        time.sleep(1.0)
    return out


# Remote Rocketship: removed. Its "API" (REMOTE_ROCKETSHIP_API_KEY / openclaw
# endpoint) does not correspond to any documented or discoverable public or
# member API - it appears to have been invented, not real. Their membership
# grants site access, not API access. Check remoterocketship.com manually
# instead of relying on this script for that source.


def fetch_remotewx_jobs():
    """https://github.com/remotewx/api - free, keyless, documented public API.
    Fetched ONCE per run (their own launch post asks for weekly polling at
    most, not rapid repeated calls) - title filtering happens downstream in
    the normal pipeline, so no server-side query is needed here."""
    resp = requests.get("https://remotewx.com/api", params={"limit": 500},
                        headers=USER_AGENT_HEADER, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if "jobs" not in payload:
        raise RuntimeError(f"unexpected response shape: {str(payload)[:150]}")
    out = []
    for r in payload.get("jobs", []):
        region = (r.get("region") or "").lower()
        loc = "Worldwide" if region in ("anywhere", "") else region.replace("-", " ").title()
        out.append(make_job("Remotewx", r.get("id"), r.get("title", ""), r.get("company", ""),
                            loc, r.get("url", ""), r.get("description", ""), r.get("createdAt", ""),
                            pay_text=(r.get("salary") or "Pay not listed")))
    return out


def fetch_weworkremotely_jobs():
    resp = requests.get("https://weworkremotely.com/remote-jobs.rss",
                        headers=USER_AGENT_HEADER, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    out = []
    for item in root.findall("./channel/item"):
        full_title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        company, sep, title = full_title.partition(":")
        if not sep:
            company, title = "We Work Remotely", full_title
        # No hard-coded "Worldwide": use the feed's region if it has one, else leave
        # blank so the location filter flags it "verify Canada eligibility".
        out.append(make_job("We Work Remotely", link, title, company,
                            item.findtext("region") or "", link,
                            item.findtext("description") or "", item.findtext("pubDate") or ""))
    return out


def fetch_remotive_jobs(params):
    resp = requests.get("https://remotive.com/api/remote-jobs", params=params,
                        headers=USER_AGENT_HEADER, timeout=20)
    resp.raise_for_status()
    return [make_job("Remotive", r.get("id"), r.get("title", ""), r.get("company_name", ""),
                     r.get("candidate_required_location", ""), r.get("url", ""),
                     r.get("description", ""), r.get("publication_date", ""),
                     pay_text=(r.get("salary") or "Pay not listed"))
            for r in resp.json().get("jobs", [])]


def fetch_remoteok_jobs():
    resp = requests.get("https://remoteok.com/api", headers=USER_AGENT_HEADER,
                        allow_redirects=True, timeout=20)
    resp.raise_for_status()
    return [make_job("RemoteOK", r.get("id"), r.get("position", ""), r.get("company", ""),
                     r.get("location", ""), r.get("url", ""), r.get("description", ""),
                     r.get("date", ""), sal_min=r.get("salary_min"),
                     sal_max=r.get("salary_max"), currency="USD")
            for r in resp.json() if isinstance(r, dict) and r.get("id")]


def board_flags(loc, workplace=""):
    low = f"{loc} {workplace}".lower()
    is_remote = any(w in low for w in ("remote", "anywhere", "worldwide"))
    is_hybrid = "hybrid" in low or (not is_remote and any(c in low for c in HYBRID_CITIES))
    return is_remote, is_hybrid


def fetch_greenhouse(slug):
    resp = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                        headers=USER_AGENT_HEADER, timeout=30)
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []):
        loc = norm_ws((j.get("location") or {}).get("name", ""))
        rem, hyb = board_flags(loc)
        out.append(make_job("Greenhouse", f'{slug}-{j.get("id")}', j.get("title", ""), slug, loc,
                            j.get("absolute_url", ""), "", j.get("updated_at", ""),
                            is_remote=rem, is_hybrid=hyb))
    return out


def fetch_lever(slug):
    resp = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                        headers=USER_AGENT_HEADER, timeout=30)
    resp.raise_for_status()
    out = []
    for j in resp.json():
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        loc = norm_ws("; ".join(locs))
        wp = j.get("workplaceType", "") or ""
        rem, hyb = board_flags(loc, wp)
        created = ""
        if j.get("createdAt"):
            created = datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).isoformat()
        out.append(make_job("Lever", f'{slug}-{j.get("id")}', j.get("text", ""), slug, loc,
                            j.get("hostedUrl", ""), j.get("descriptionPlain", ""), created,
                            is_remote=rem, is_hybrid=hyb))
    return out


def fetch_ashby(slug):
    resp = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                        headers=USER_AGENT_HEADER, timeout=30)
    resp.raise_for_status()
    out = []
    for j in resp.json().get("jobs", []):
        locs = [j.get("location") or ""]
        for s in j.get("secondaryLocations") or []:
            locs.append(s.get("location", "") if isinstance(s, dict) else str(s))
        loc = norm_ws("; ".join(l for l in locs if l))
        rem, hyb = board_flags(loc, j.get("workplaceType", "") or "")
        if j.get("isRemote"):
            rem = True
        out.append(make_job("Ashby", f'{slug}-{j.get("id")}', j.get("title", ""), slug, loc,
                            j.get("jobUrl") or j.get("applyUrl", ""),
                            j.get("descriptionPlain", ""), j.get("publishedAt", ""),
                            is_remote=rem, is_hybrid=hyb))
    return out


def fetch_workable(slug):
    """https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true - public, no key."""
    resp = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
                        params={"details": "true"}, headers=USER_AGENT_HEADER, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for j in data.get("jobs", []):
        loc_obj = j.get("location") or {}
        loc = ", ".join(str(v) for v in
                        (loc_obj.get("city"), loc_obj.get("region"), loc_obj.get("country"))
                        if v) or loc_obj.get("location_str", "")
        workplace = (j.get("workplace") or "").lower()   # remote / hybrid / on_site, if present
        rem, hyb = board_flags(loc, workplace)
        if j.get("remote") is True or workplace == "remote":
            rem = True
        if workplace == "hybrid":
            hyb = True
        out.append(make_job("Workable", j.get("shortcode") or j.get("id") or j.get("code"),
                            j.get("title", ""), data.get("name", slug), loc,
                            j.get("url") or j.get("shortlink", ""), j.get("description", ""),
                            j.get("published_on") or j.get("created_at", ""),
                            is_remote=rem, is_hybrid=hyb))
    return out


def fetch_teamtailor(host):
    """https://{host}/jobs.json - Teamtailor's public JSON Feed 1.1. `host` is the
    full career-site domain (usually {slug}.teamtailor.com, sometimes custom)."""
    resp = requests.get(f"https://{host}/jobs.json", headers=USER_AGENT_HEADER, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for it in data.get("items", []):
        # JSON Feed 1.1 guarantees title/url/content_html/date_published on every item;
        # exact nested structured-location field isn't confirmed, so location/remote
        # status is read from the visible text as a fallback (same approach as Adzuna).
        text = strip_tags(it.get("content_html", ""))
        rem, hyb = board_flags(text)
        out.append(make_job("Teamtailor", it.get("id") or it.get("url"), it.get("title", ""),
                            data.get("title", host.split(".")[0]), "", it.get("url", ""),
                            text, it.get("date_published", ""), is_remote=rem, is_hybrid=hyb))
    return out


def fetch_smartrecruiters(slug):
    """https://developers.smartrecruiters.com/docs/endpoints - public Posting API, no key."""
    out, offset = [], 0
    while True:
        resp = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                            params={"limit": 100, "offset": offset}, headers=USER_AGENT_HEADER,
                            timeout=30)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        for j in content:
            loc = j.get("location") or {}
            loc_str = ", ".join(str(v) for v in (loc.get("city"), loc.get("region"),
                                                  loc.get("country")) if v)
            remote_flag = bool(loc.get("remote"))
            rem, hyb = board_flags(loc_str, j.get("typeOfEmployment", {}).get("label", "")
                                    if isinstance(j.get("typeOfEmployment"), dict) else "")
            jad = j.get("jobAdId") or j.get("id")
            url = j.get("ref") or (f"https://jobs.smartrecruiters.com/{slug}/{jad}" if jad else "")
            out.append(make_job("SmartRecruiters", j.get("id"), j.get("name", ""), slug, loc_str,
                                url, "", j.get("releasedDate", ""),
                                is_remote=rem or remote_flag, is_hybrid=hyb))
        offset += len(content)
        if len(content) < 100 or offset >= data.get("totalFound", 0):
            break
        time.sleep(0.5)
    return out


BOARD_FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
                  "workable": fetch_workable, "teamtailor": fetch_teamtailor,
                  "smartrecruiters": fetch_smartrecruiters}


def collect_all_jobs():
    raw = []
    print("Fetching...")

    if ADZUNA_APP_ID and ADZUNA_APP_KEY:
        passes = []
        for q in SEARCH_QUERIES:
            passes.append((q, None))
            if ADZUNA_ALSO_SEARCH_REMOTE:
                passes.append((f"{q} remote", None))
            if LOCAL_WHERE:
                passes.append((q, LOCAL_WHERE))
        for q, where in passes:
            raw += run_source("Adzuna", fetch_adzuna_jobs, q, where,
                              item=f"{q}{' @ ' + where if where else ''}")
            time.sleep(1.0)
    else:
        STATS["Adzuna"] = {"fetched": 0, "errors": ["skipped: ADZUNA_APP_ID / ADZUNA_APP_KEY not set"]}
        print("  Adzuna: skipped (no keys)")

    # Remotewx is no longer fetched: its endpoint returned invalid JSON on every run.

    raw += run_source("We Work Remotely", fetch_weworkremotely_jobs)
    for params in ({"category": "writing"}, {"search": "technical writer"},
                   {"search": "documentation"}):
        raw += run_source("Remotive", fetch_remotive_jobs, params, item=str(list(params.values())[0]))
    raw += run_source("RemoteOK", fetch_remoteok_jobs)

    for kind, slugs in COMPANY_BOARDS.items():
        for slug in slugs:
            raw += run_source(kind.capitalize(), BOARD_FETCHERS[kind], slug, item=slug)
    return raw


# ==============================================================================
# 6. FILTERS
# ==============================================================================
def is_relevant_title(title):
    t = title.lower()
    # Whole words only, so "intern" no longer rejects "International" or "Internal".
    if any(re.search(rf"\b{re.escape(x)}s?\b", t) for x in EXCLUDED_TITLE_KEYWORDS):
        return False
    return any(r in t for r in REQUIRED_TITLE_KEYWORDS)


def is_blocked_company(name):
    c = name.lower().strip()
    return any(b in c for b in EXCLUDED_COMPANIES)


DESC_US_ONLY_PATTERNS = [
    r"remote\s*\((?:united states|usa|us|u\.s\.)\)",
    r"remote\s*[-,]\s*(?:united states|usa|us)\b",
    r"\b(?:united states|usa|us|u\.s\.)[ -]only\b",
    r"authorized to work in the (?:united states|us|u\.s\.)",
    r"must (?:be located|reside|live) in the (?:united states|us|u\.s\.)",
]


def says_us_only(desc):
    """True when the posting text restricts the role to the US (e.g. a board tags it
    'Anywhere' but the description says 'Remote (United States)'). Skipped if you
    are in the US, or if the posting also names your own country."""
    if COUNTRY_CODE == "us":
        return False
    d = desc.lower()
    if any(t in d for t in HOME_COUNTRY_TERMS):
        return False
    return any(re.search(p, d) for p in DESC_US_ONLY_PATTERNS)


def region_status(loc, desc=""):
    """For a REMOTE job: 'ok' (mentions Canada/worldwide...), 'verify' (bare
    'Remote'/blank, region unknown), or 'no' (US-only or another region)."""
    l, d = loc.lower(), desc.lower()
    if says_us_only(desc):
        return "no"
    if any(r in l for r in OK_REGIONS):
        return "ok"
    if any(p in l for p in US_ONLY):
        return "no"
    leftover = re.sub(r"[^a-z]+", " ", l.replace("remote", " ")).strip()
    if leftover:                        # e.g. "Bengaluru, India", "United States", "UK"
        return "no"
    if any(p in d for p in US_ONLY) and not any(t in d for t in HOME_COUNTRY_TERMS):
        return "no"
    return "verify"


def is_acceptable_location(job):
    loc, desc = job["location"], job["description"]
    if job["is_remote"]:
        status = region_status(loc, desc)
        if status == "no":
            return False
        job["loc_flag"] = "" if status == "ok" else f"remote, but verify {HOME_LABEL} is eligible"
        if job.get("note"):
            job["loc_flag"] = job["note"]
        return True
    if INCLUDE_HYBRID and job["is_hybrid"]:
        l, d = loc.lower(), desc.lower()
        if any(c in l or c in d for c in HYBRID_CITIES):
            return True   # a short-commute city: no flag needed
        if job["source"] == "Adzuna":
            # Adzuna searches are already scoped to COUNTRY_CODE, so the location
            # text itself doesn't need to separately say your country's name.
            job["loc_flag"] = f'hybrid in {loc or "an unspecified city"} - check the commute'
            return True
        return any(t in l for t in HOME_COUNTRY_TERMS)
    return False


def is_fresh_listing(job):
    dt = parse_date(job.get("created_date"))
    if dt and datetime.now(timezone.utc) - dt > timedelta(days=MAX_JOB_AGE_DAYS):
        return False
    year = datetime.now().year
    for y in re.findall(r"(?:duration|expires|closed|posted|date)[:\s].*?\b(20\d\d)\b",
                        job.get("description", "").lower()):
        if int(y) < year - 1:
            return False
    return True


def is_scam(job):
    desc = job.get("description", "").lower()
    high_pay = any(s and s >= 180000 for s in (job.get("max_salary"), job.get("min_salary")))
    return high_pay and any(p in desc for p in SCAM_PHRASES)


def classify_pay(job):
    """'meets' (listed pay >= threshold), 'unlisted' (unknown / estimated), 'below'."""
    if job.get("pay_estimated"):
        return "unlisted"
    lo, hi = job.get("min_salary"), job.get("max_salary")
    if hi is not None:
        return "meets" if hi >= MIN_SALARY_THRESHOLD else "below"
    if lo is not None:
        return "meets" if lo >= MIN_SALARY_THRESHOLD else "unlisted"  # "from $X": max unknown
    return "unlisted"


def clean_company_name(name):
    c = re.sub(r"\b(inc|corp|corporation|canada|ltd|limited|llc|group|gmbh)\b", "", name.lower())
    return re.sub(r"[^\w\s]", "", c).strip()


def deduplicate_jobs(jobs):
    """Merge same company + (near-)same title, or same URL. Merged jobs keep every
    original id (all_ids) so none of them is re-reported as new later."""
    def is_dup(a, b):
        if a["url"] and a["url"] == b["url"]:
            return True
        if clean_company_name(a["company"]) != clean_company_name(b["company"]):
            return False
        ta = re.sub(r"[^\w\s]", "", a["title"].lower()).strip()
        tb = re.sub(r"[^\w\s]", "", b["title"].lower()).strip()
        return ta == tb or SequenceMatcher(None, ta, tb).ratio() >= 0.85

    unique = []
    for j in jobs:
        dup = next((p for p in unique if is_dup(p, j)), None)
        if dup is None:
            j["all_ids"] = [j["id"]]
            unique.append(j)
            continue
        dup["all_ids"].append(j["id"])
        base = j["location"].split(f" ({HOME_LABEL}")[0]
        if base and base not in dup["location"]:
            dup["location"] += f" | {base}"
        if dup.get("loc_flag") and not j.get("loc_flag"):
            dup["loc_flag"] = ""
        if dup["pay_class"] != "meets" and j["pay_class"] == "meets":
            for k in ("pay_text", "min_salary", "max_salary", "pay_class"):
                dup[k] = j[k]
    return unique


# ==============================================================================
# 7. REPORTING
# ==============================================================================
def render_job(j):
    flag = (f' <span style="color:#b45f06;">[{escape(j["loc_flag"])}]</span>'
            if j.get("loc_flag") else "")
    pay_style = "color:#274e13;font-weight:bold;" if j["pay_class"] == "meets" else "color:#555;"
    n = len(j["all_ids"])
    merged = f' <i>({n} duplicate listings merged)</i>' if n > 1 else ""
    return (f'<li style="margin-bottom:14px;"><b>{escape(j["title"])}</b> - {escape(j["company"])} '
            f'<i>({escape(j["source"])}; {escape(j["location"] or "location not listed")})</i>{flag}{merged}<br>'
            f'<span style="{pay_style}">Pay:</span> {escape(j["pay_text"])}<br>'
            f'<a href="{escape(j["url"])}" style="color:#1155cc;">{escape(j["url"])}</a></li>')


def render_section(title, jobs, color):
    if not jobs:
        return ""
    jobs = sorted(jobs, key=lambda j: (j["pay_class"] != "meets", j["company"].lower()))
    meets = [j for j in jobs if j["pay_class"] == "meets"]
    other = [j for j in jobs if j["pay_class"] != "meets"]
    h = [f'<h3 style="color:{color};font-size:18px;margin:22px 0 2px;border-bottom:2px solid {color};">'
         f'{escape(title)} ({len(jobs)})</h3>']
    if meets:
        h.append(f'<p style="margin:8px 0 2px;"><b>Listed pay ${MIN_SALARY_THRESHOLD:,.0f}+/yr ({len(meets)})</b></p>'
                 f'<ul style="padding-left:20px;">{"".join(render_job(j) for j in meets)}</ul>')
    if other:
        h.append(f'<p style="margin:8px 0 2px;"><b>Pay not listed or unverified ({len(other)})</b></p>'
                 f'<ul style="padding-left:20px;">{"".join(render_job(j) for j in other)}</ul>')
    return "\n".join(h)


def generate_reports(new_jobs, open_jobs, report_date):
    def split(jobs):
        return ([j for j in jobs if j["is_remote"]],
                [j for j in jobs if not j["is_remote"] and j["is_hybrid"]])

    new_r, new_h = split(new_jobs)
    old_r, old_h = split(open_jobs)
    html = [f'<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#222;'
            f'line-height:1.5;margin:20px;"><h2>Tech Writing Jobs - {report_date}</h2>',
            f'<p>{len(new_jobs)} new since the last report; {len(open_jobs)} distinct '
            f'matching jobs are currently open.</p>',
            render_section(f"NEW - Remote ({HOME_LABEL} / worldwide)", new_r, "#274e13"),
            render_section("NEW - Hybrid (local commute)", new_h, "#274e13"),
            render_section("STILL OPEN - Remote (reported before)", old_r, "#0b5394"),
            render_section("STILL OPEN - Hybrid (reported before)", old_h, "#0b5394"),
            "</body></html>"]
    text = [f"Tech Writing Jobs - {report_date}",
            f"{len(new_jobs)} new; {len(open_jobs)} currently open.", ""]
    text += [f"- {j['title']} | {j['company']} | {j['location']} | {j['pay_text']}\n  {j['url']}"
             for j in new_jobs]
    return "\n".join(text), "\n".join(x for x in html if x)


def send_email(subject, text_body, html_body):
    """Returns True only if the message was actually accepted by the SMTP server."""
    server_name = os.getenv("JOBSEARCH_SMTP_SERVER", "smtp.gmail.com")
    port = int(os.getenv("JOBSEARCH_SMTP_PORT", "587"))
    user = os.getenv("JOBSEARCH_SMTP_USER")
    password = os.getenv("JOBSEARCH_SMTP_PASSWORD")
    recipient = os.getenv("JOBSEARCH_EMAIL_TO") or user
    if not user or not password:
        print("Email NOT sent: missing JOBSEARCH_SMTP_USER / JOBSEARCH_SMTP_PASSWORD in secrets.env.")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, recipient
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(server_name, port, timeout=20) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        print(f"Report emailed to {recipient}")
        return True
    except Exception as e:
        print(f"Email FAILED: {e}")
        return False


# ==============================================================================
# 8. MAIN
# ==============================================================================
def show_dropped(label, before, after, detail, limit=25):
    kept = {id(j) for j in after}
    gone = [j for j in before if id(j) not in kept]
    if not gone:
        return
    print(f"\nDropped by {label} ({len(gone)}):")
    for j in gone[:limit]:
        print(f'  {j["company"]}: {j["title"]}  |  {detail(j)}')
    if len(gone) > limit:
        print(f"  ... and {len(gone) - limit} more")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Daily technical-writing job digest")
    ap.add_argument("--force", action="store_true",
                    help="treat every matching job as new (ignore the seen-history)")
    ap.add_argument("--dry-run", action="store_true",
                    help="do not send email or update history; still saves the HTML report")
    args = ap.parse_args(argv)

    cleanup_old_reports()
    raw = collect_all_jobs()
    # The same posting is often returned by several Adzuna/Remotive passes.
    raw = list({j["id"]: j for j in raw}.values())

    funnel = {"fetched (unique)": len(raw)}
    title_ok = [j for j in raw if is_relevant_title(j["title"])]
    funnel["title match"] = len(title_ok)
    not_blocked = [j for j in title_ok if not is_blocked_company(j["company"])]
    funnel["after blocked-company filter"] = len(not_blocked)

    loc_ok, loc_rejected = [], []
    for j in not_blocked:
        (loc_ok if is_acceptable_location(j) else loc_rejected).append(j)
    funnel["after location filter"] = len(loc_ok)

    fresh = [j for j in loc_ok if is_fresh_listing(j)]
    funnel["after freshness filter"] = len(fresh)
    clean = [j for j in fresh if not is_scam(j)]
    funnel["after scam filter"] = len(clean)

    for j in clean:
        j["pay_class"] = classify_pay(j)
    # A below-floor copy of a posting beats an "unlisted" copy of the same posting.
    def pkey(j):
        return (clean_company_name(j["company"]), re.sub(r"[^\w\s]", "", j["title"].lower()).strip())
    below_keys = {pkey(j) for j in clean if j["pay_class"] == "below"}
    meets_keys = {pkey(j) for j in clean if j["pay_class"] == "meets"}
    paid_ok = [j for j in clean if j["pay_class"] != "below"
               and not (j["pay_class"] == "unlisted" and pkey(j) in below_keys
                        and pkey(j) not in meets_keys)]
    funnel["after pay filter (listed pay too low dropped)"] = len(paid_ok)

    unique = deduplicate_jobs(paid_ok)
    funnel["distinct after de-duplication"] = len(unique)
    new_jobs = unique if args.force else get_new_unseen_jobs(unique)
    funnel["new (all, because --force)" if args.force else "new since last report"] = len(new_jobs)

    print("\n--- Source health ---")
    for label, stat in STATS.items():
        err = f" - {len(stat['errors'])} problem(s): {'; '.join(stat['errors'])[:300]}" if stat["errors"] else ""
        print(f"  {label}: {stat['fetched']} fetched{err}")

    if NOT_FOUND:
        print("\n--- Boards not found: delete these from COMPANY_BOARDS ---")
        print("  " + ", ".join(NOT_FOUND))

    print("\n--- Filter funnel ---")
    for k, v in funnel.items():
        print(f"  {v:6}  {k}")
    if loc_rejected:
        print(f"\nTitle matched but rejected on LOCATION ({len(loc_rejected)}):")
        for j in loc_rejected[:40]:
            print(f'  {j["company"]}: {j["title"]}  |  {j["location"] or "(none)"}')
    show_dropped("freshness filter", loc_ok, fresh, lambda j: f'posted {j["created_date"] or "?"}')
    show_dropped("scam filter", fresh, clean, lambda j: f'{j["pay_text"]}')
    show_dropped("pay filter", clean, paid_ok, lambda j: f'{j["pay_text"]}')
    print()

    if new_jobs:
        print(f"Jobs in this report ({len(new_jobs)}):")
        for j in sorted(new_jobs, key=lambda j: (j["pay_class"] != "meets", j["company"].lower())):
            flag = f'  [{j["loc_flag"]}]' if j.get("loc_flag") else ""
            print(f'  {j["company"]}: {j["title"]}  |  {j["location"]}  |  {j["pay_text"]}{flag}')
            print(f'      {j["url"]}')
        print()

    today = datetime.now().strftime("%Y-%m-%d")
    open_jobs = [j for j in unique if j not in new_jobs]
    text_report, html_report = generate_reports(new_jobs, open_jobs, today)

    if not new_jobs:
        print("No new jobs today. Skipping email.")
        return

    report_path = HERE / f"jobs_report_{today}.html"
    report_path.write_text(html_report, encoding="utf-8")
    print(f"Report saved to {report_path}")

    if args.dry_run:
        print("--dry-run: no email sent, history not updated.")
        return

    if send_email(f"Tech Writing Jobs - {len(new_jobs)} new ({today})", text_report, html_report):
        mark_seen(new_jobs)
    else:
        print("These jobs stay 'new' and will be reported again next run "
              "(they are in the saved HTML report meanwhile).")
        for j in new_jobs:
            print(f'  {j["title"]} - {j["company"]} ({j["location"]}) {j["url"]}')


if __name__ == "__main__":
    main()
