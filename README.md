# Daily Technical Writing Job Search

A Python script, written with Claude's help, that checks about ten job sources every day — company career pages,
aggregators, and niche boards — filters for technical-writing roles you're
actually eligible for (remote worldwide, or hybrid within a commute you'll
accept), and emails you only the ones you haven't seen before.

It does **not** replace direct outreach or networking. In a niche field like
technical writing, this is a safety net that makes sure you never miss a
posting — it won't manufacture opportunities that don't exist yet.

**Provided as is.** It worked for its author, but job-board APIs change without notice, and I can't promise support or results. Use it, adapt it, and share it.

## Files you should have

| File | What it is |
|---|---|
| `job_search.py` | The script itself |
| `secrets.env.example` | Template for your credentials — copy and rename it |
| `README.md` | This file |
| `requirements.txt` | Python dependency list (`pip install -r requirements.txt`) |
| `.gitignore` | Keeps your credentials and job history out of Git |
| `LICENSE` | MIT license |

## What you'll need

- **Windows, Mac, or Linux** with Python 3.8 or newer. Check with `python --version` (Windows) or `python3 --version` (Mac/Linux).
- **The `requests` library.** Install it once: `pip install requests` (Windows) or `pip3 install requests` (Mac/Linux). Everything else the script uses is in Python's standard library.
- **A free Adzuna account** (step 1 below).
- **An email account you can send FROM** — Gmail, Outlook, or similar (step 2 below).

---

## Step 1 — Get a free Adzuna key

Adzuna is the main job-search source. Sign up at
**https://developer.adzuna.com/signup**, then create an "Application" in
their dashboard. You'll get an **App ID** and an **App Key** — copy both
somewhere safe for step 2. The free tier allows 250 calls/day; this script
uses roughly 20–60 per run, so you won't come close to the limit.

## Step 2 — Set up your credentials

1. In the same folder as `job_search.py`, copy `secrets.env.example` to a
   new file named exactly `secrets.env` (remove `.example` — not
   `secrets.env.txt`, which is a common accidental result of renaming in
   Windows Explorer; run `dir secrets*` afterward to check the exact name).
2. Open `secrets.env` in a text editor and fill in your real Adzuna
   App ID/Key and your email details. Follow the comments in that file —
   Gmail needs an **App Password**, not your normal password; the file
   explains how to generate one.
3. **Never share this file or paste it anywhere public.** It's the one
   file in this whole setup that's genuinely private.

## Step 3 — Personalize the script

Open `job_search.py` in a text editor (VS Code, Notepad++, anything) and find
the section headed:

```
2A. PERSONALIZE  -  edit every value in this block for your own situation.
```

It's near the top of the file. Every value in that block is something
specific to *you*:

- **`COUNTRY_CODE`** — which country's job market Adzuna searches. `"ca"` for
  Canada, `"us"`, `"gb"`, `"au"`, `"de"`, etc.
- **`OK_REGIONS`** — words that mean "I'm eligible" when they appear in a
  remote listing's location. Include your own country, common alternate
  names for it, and the usual "open to anyone" phrases.
- **`US_ONLY`** — phrases that mean "not eligible." Add any other
  country/region that keeps showing up that you can't work from.
- **`HOME_LABEL`** / **`HOME_COUNTRY_TERMS`** — how your own country is
  named in the emailed report, and the specific words (lowercase) that name
  your country/province/state. These feed into some of the eligibility logic
  beyond the simple lists above.
- **`HYBRID_CITIES`** — cities within a commute you'd actually accept, for
  hybrid roles. Anywhere else hybrid is found, it's still shown, just
  flagged with its city so you can judge it yourself.
- **`LOCAL_WHERE`** / **`LOCAL_DISTANCE_KM`** — Adzuna's local-radius search
  pass. Set `LOCAL_WHERE = ""` to turn this pass off entirely.
- **`MIN_SALARY_THRESHOLD`** — the lowest annual pay you'd consider.
- **`EXCLUDED_COMPANIES`** — any companies/staffing agencies you never want
  to see again.

Everything **after** that block (title keywords, scam phrases, the list of
companies under `COMPANY_BOARDS`) is a shared starting point that should
work reasonably well as-is — but feel free to tune it too, especially
`COMPANY_BOARDS`. **Adding more companies there is the single best way to
get more results** — it's a thin market, and the company list determines
how much of it you actually see. Instructions for adding a company are
right above that list in the file.

## Step 4 — Test it

Open a terminal in the script's folder and run:

```
python job_search.py --dry-run
```

(On Mac/Linux, use `python3` instead of `python`.)

This fetches and filters everything but sends no email and doesn't record
anything as "seen" — safe to run as many times as you like. Watch the
console output: it prints one line per source, a funnel showing how many
jobs survive each filtering step, and (if anything looks off) exactly which
jobs got dropped and why. If a source says "not found" or shows an error,
that's worth reading — it usually means a company slug needs fixing.

Once that looks right, drop `--dry-run` to send yourself a real email and
start recording history:

```
python job_search.py
```

Add `--force` to any run to treat every matching job as "new" — useful the
very first time, to see everything currently open rather than just future
changes.

## Step 5 — Automate it

### Windows (Task Scheduler)

1. Find your real Python path. In PowerShell: `where.exe python` (not the
   plain `where` alias — it does something different). Use the path under
   `Programs\Python\...`, **not** the one under `WindowsApps` — that one is
   a Microsoft Store stub, not a real interpreter, and won't run reliably
   from a scheduled task.
2. Open **Task Scheduler** → **Create Basic Task**. Name it, set the
   trigger to **Daily** at a time of your choosing (morning works well, so
   boards have updated overnight).
3. Action: **Start a program**.
   - **Program/script**: the full python.exe path from step 1.
   - **Add arguments**: the full path to the script in quotes, e.g.
     `"C:\Users\you\jobsearch\job_search.py"`
   - **Start in**: the folder itself, e.g. `C:\Users\you\jobsearch` (no
     quotes, no trailing backslash) — this is how the script finds
     `secrets.env` and its history file.
4. Finish, then right-click the task → **Properties**.
   - If you sign in with a PIN rather than a full password, leave
     **"Run only when user is logged on"** selected (the default) — no
     password needed. This runs fine from a locked screen, just not after
     a full shutdown.
   - On the **Conditions** tab: uncheck "Start the task only if the
     computer is on AC power" (otherwise it silently skips on battery), and
     check "Wake the computer to run this task" if you want it to fire even
     while asleep.
5. Right-click → **Run** once to test, then check your email and the
   folder for a new `jobs_report_*.html` file.

### Mac / Linux (cron)

```
crontab -e
```

Add a line like this (adjust the path and time; `0 7` means 7:00 AM daily):

```
0 7 * * * cd /path/to/jobsearch && /usr/bin/python3 job_search.py >> run.log 2>&1
```

The `>> run.log` part saves the console output somewhere you can check
later, since cron won't show it to you directly.

---

## Troubleshooting

- **"No such file or directory"** — the filename you typed doesn't match
  what's actually on disk. Run `dir *.py` (Windows) or `ls *.py` (Mac/Linux)
  to see the real filename.
- **Email never arrives** — check spam, and re-read the console output; it
  prints "Email sent" or "Email FAILED: <reason>" every run.
- **A source shows "0 fetched" with errors** — not necessarily a problem;
  read the specific error. A timeout usually clears up on the next run.
- **"Boards not found: delete these from COMPANY_BOARDS"** at the end of a
  run — those company names are wrong (HTTP 404). Delete them from
  `COMPANY_BOARDS`, or fix the slug by checking the company's job-posting URL.
- **Everything shows as "new" again after it worked yesterday** — this
  means `job_history.db` wasn't updated, almost always because email failed
  on the previous run (jobs are only marked "seen" after a successful
  send). Fix the email problem and it'll catch up automatically.
- **Environment variables set with `setx`/`export` don't seem to work** —
  use `secrets.env` instead; it's more reliable, since variables set in one
  terminal window often aren't visible in a different one.

## Known limitations, worth knowing up front

- **The market is thin.** Out of several thousand jobs fetched per run,
  expect single digits to survive every filter on most days. That's not a
  bug — it's genuinely how small the pool of remote/eligible technical
  writing roles is at any given moment.
- **Not every company runs on a public-API platform.** Some use fully
  custom career portals with no API to connect to. If a specific company
  you care about isn't showing results, check their careers page by hand —
  the script may simply have no way to see it.
- **A few sources are newer and less proven** than the core ones
  (Greenhouse, Lever, Ashby, Adzuna): Teamtailor and SmartRecruiters were
  added based on their documented public APIs but haven't been tested
  against as much real-world data. Keep an eye on their lines in the
  console output for a while.
- **Some postings are caught by their description, not their tag.** A board may
  label a job "Anywhere" while the text says "Remote (United States)". The script
  checks descriptions for common US-only wording and drops those jobs, but it
  can't catch every phrasing, so always read the posting before applying.
- **Free APIs ask to be used considerately.** A few of these sources are
  small, free services asking for reasonable call volumes, not relentless
  polling. Running this more than once a day isn't necessary and risks
  getting rate-limited.

## Before you put your copy on GitHub

If you fork or copy this to your own repository, never commit `secrets.env`, `job_history.db`, or the `jobs_report_*.html` files. The included `.gitignore` excludes them. Also replace the example values in the personalization block with your own, or leave them as placeholders.

## License

MIT. See `LICENSE`.
