"""
job_scraper.py — Multi-source UK job listing scraper
Sources: Reed API, Adzuna API

Setup:
    1. Run: python job_scraper.py
       (installs dependencies, generates config.ini and keywords.txt)
    2. Fill in config.ini with your API keys and search settings
    3. Add your search keywords to keywords.txt
    4. Run again to start scraping

API keys:
    Reed   : https://www.reed.co.uk/developers
    Adzuna : https://developer.adzuna.com

Email (optional):
    Uses Gmail SMTP. Generate an App Password at:
    myaccount.google.com -> Security -> 2-Step Verification -> App Passwords
"""

import subprocess
import sys

# ── Dependency bootstrap ───────────────────────────────────────────────────────

REQUIRED = ["httpx", "tenacity"]

def ensure_dependencies():
    missing = []
    for pkg in REQUIRED:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"  Installing missing packages: {', '.join(missing)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *missing],
            stdout=subprocess.DEVNULL,
        )
        print("  Done.\n")

ensure_dependencies()

# ── Imports ────────────────────────────────────────────────────────────────────

import argparse
import configparser
import csv
import hashlib
import json
import random
import smtplib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

# ── Constants ──────────────────────────────────────────────────────────────────

DB_PATH       = "jobs.db"
CSV_PATH      = "jobs.csv"
CONFIG_PATH   = "config.ini"
KEYWORDS_PATH = "keywords.txt"
REQUEST_DELAY = (1.5, 3.5)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

# ── Config ─────────────────────────────────────────────────────────────────────

def create_default_config(path: str = CONFIG_PATH):
    """Write a blank config.ini template if one doesn't exist."""
    if Path(path).exists():
        return
    content = (
        "[keys]\n"
        "# Get your Reed API key at https://www.reed.co.uk/developers\n"
        "reed_key =\n"
        "\n"
        "# Get your Adzuna credentials at https://developer.adzuna.com\n"
        "adzuna_id =\n"
        "adzuna_key =\n"
        "\n"
        "[search]\n"
        "# Location to search, e.g. Scotland, London, United Kingdom\n"
        "location =\n"
        "\n"
        "# Search radius in miles, e.g. 50\n"
        "distance_miles =\n"
        "\n"
        "[filter]\n"
        "# Optional: comma-separated terms to filter results by title/description\n"
        "# Leave blank to keep all results\n"
        "# Example: unity, game, gameplay, unreal, xr, vr, multiplayer\n"
        "terms =\n"
        "\n"
        "[email]\n"
        "# Optional: fill in to receive a summary email after each run\n"
        "# Use a Gmail App Password (not your normal password):\n"
        "# myaccount.google.com -> Security -> 2-Step Verification -> App Passwords\n"
        "smtp_host = smtp.gmail.com\n"
        "smtp_port = 587\n"
        "sender =\n"
        "password =\n"
        "recipient =\n"
        "\n"
        "[options]\n"
        "# Set to true to also export a jobs.json alongside jobs.csv\n"
        "export_json = false\n"
    )
    Path(path).write_text(content, encoding="utf-8")
    print(f"  Created {path} — fill in your API keys and search settings, then rerun.\n")


def create_default_keywords(path: str = KEYWORDS_PATH):
    """Write a blank keywords.txt template if one doesn't exist."""
    if Path(path).exists():
        return
    content = (
        "# Job search keywords — one per line\n"
        "# Lines starting with # are ignored\n"
        "#\n"
        "# Example:\n"
        "#   Unity developer\n"
        "#   gameplay programmer\n"
        "#   XR developer\n"
    )
    Path(path).write_text(content, encoding="utf-8")
    print(f"  Created {path} — add your search keywords, then rerun.\n")


def load_config(path: str = CONFIG_PATH) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if Path(path).exists():
        config.read(path)
    return config


def load_keywords(path: str = KEYWORDS_PATH) -> list[str]:
    """Load keywords from a text file, one per line. Lines starting with # are comments."""
    p = Path(path)
    if not p.exists():
        return []
    keywords = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keywords.append(line)
    return keywords


def load_relevant_terms(config: configparser.ConfigParser) -> set[str]:
    """Load relevance filter terms from [filter] terms in config.ini."""
    raw = config.get("filter", "terms", fallback="").strip()
    if not raw:
        return set()
    return {t.strip().lower() for t in raw.split(",") if t.strip()}

# ── Validation ─────────────────────────────────────────────────────────────────

def validate(
    location: str,
    distance_str: str,
    keywords_list: list[str],
    keywords_file: str,
    reed_key: str,
    adzuna_id: str,
    adzuna_key: str,
) -> tuple[int, list[str]]:
    """Validate all required config. Returns (distance_int, errors)."""
    errors = []

    if not reed_key and not (adzuna_id and adzuna_key):
        errors.append(
            "  - At least one set of API keys must be configured in config.ini\n"
            "      Reed   : https://www.reed.co.uk/developers\n"
            "      Adzuna : https://developer.adzuna.com"
        )
    if not location:
        errors.append("  - [search] location is required in config.ini (e.g. Scotland)")
    if not distance_str:
        errors.append("  - [search] distance_miles is required in config.ini (e.g. 50)")
    if not keywords_list:
        errors.append(
            f"  - No keywords found in {keywords_file}\n"
            "      Add at least one search term, one per line"
        )

    distance = 0
    if distance_str:
        try:
            distance = int(distance_str)
            if distance <= 0:
                errors.append("  - [search] distance_miles must be a positive integer")
        except ValueError:
            errors.append(
                f"  - [search] distance_miles must be a whole number, got: '{distance_str}'"
            )

    return distance, errors

# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class JobListing:
    title: str
    company: str
    location: str
    url: str
    salary: str = ""
    date_posted: str = ""
    source: str = ""
    description: str = ""
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def uid(self) -> str:
        """Dedup key: prefer URL, fall back to title+company."""
        key = self.url.strip() if self.url.strip() else f"{self.title}{self.company}"
        return hashlib.md5(key.encode()).hexdigest()

# ── HTTP helpers ───────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8))
def _get(url: str, **kwargs) -> httpx.Response:
    time.sleep(random.uniform(*REQUEST_DELAY))
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=15) as client:
        r = client.get(url, **kwargs)
        r.raise_for_status()
        return r

def _salary_range(lo, hi) -> str:
    if lo and hi:
        return f"£{int(lo):,}–£{int(hi):,}"
    if lo:
        return f"£{int(lo):,}+"
    if hi:
        return f"up to £{int(hi):,}"
    return ""

# ── Source: Reed API ───────────────────────────────────────────────────────────

def scrape_reed(
    keywords: str,
    location: str,
    api_key: str,
    distance_miles: int,
    max_results: int = 25,
) -> list[JobListing]:
    url = (
        f"https://www.reed.co.uk/api/1.0/search"
        f"?keywords={quote_plus(keywords)}"
        f"&locationName={quote_plus(location)}"
        f"&distanceFromLocation={distance_miles}"
        f"&resultsToTake={max_results}"
    )
    try:
        r = _get(url, auth=(api_key, ""))
        data = r.json()
    except Exception as e:
        print(f"      Reed error: {e}")
        return []

    results = []
    for job in data.get("results", []):
        results.append(JobListing(
            title=job.get("jobTitle", ""),
            company=job.get("employerName", ""),
            location=job.get("locationName", location),
            url=job.get("jobUrl", ""),
            salary=_salary_range(job.get("minimumSalary"), job.get("maximumSalary")),
            date_posted=job.get("date", ""),
            source="reed",
            description=job.get("jobDescription", "")[:300],
        ))
    return results

# ── Source: Adzuna API ─────────────────────────────────────────────────────────

def scrape_adzuna(
    keywords: str,
    location: str,
    app_id: str,
    app_key: str,
    country: str = "gb",
    max_results: int = 25,
) -> list[JobListing]:
    url = (
        f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
        f"?app_id={app_id}&app_key={app_key}"
        f"&results_per_page={max_results}"
        f"&what={quote_plus(keywords)}"
        f"&where={quote_plus(location)}"
        f"&content-type=application/json"
    )
    try:
        r = _get(url)
        data = r.json()
    except Exception as e:
        print(f"      Adzuna error: {e}")
        return []

    results = []
    for job in data.get("results", []):
        results.append(JobListing(
            title=job.get("title", ""),
            company=job.get("company", {}).get("display_name", ""),
            location=job.get("location", {}).get("display_name", location),
            url=job.get("redirect_url", ""),
            salary=_salary_range(job.get("salary_min"), job.get("salary_max")),
            date_posted=job.get("created", ""),
            source="adzuna",
            description=job.get("description", "")[:300],
        ))
    return results

# ── Storage ────────────────────────────────────────────────────────────────────

class JobStore:
    COLS = (
        "uid", "title", "company", "location", "url",
        "salary", "date_posted", "source", "description", "scraped_at",
    )

    def __init__(self, db_path: str = DB_PATH):
        self.con = sqlite3.connect(db_path)
        self.con.execute(f"""
            CREATE TABLE IF NOT EXISTS jobs (
                {', '.join(self.COLS)},
                PRIMARY KEY (uid)
            )
        """)
        self.con.commit()

    def add_new(self, listings: list[JobListing]) -> list[JobListing]:
        added = []
        for job in listings:
            try:
                self.con.execute(
                    f"INSERT INTO jobs VALUES ({', '.join('?' * len(self.COLS))})",
                    (job.uid, job.title, job.company, job.location, job.url,
                     job.salary, job.date_posted, job.source,
                     job.description, job.scraped_at),
                )
                added.append(job)
            except sqlite3.IntegrityError:
                pass
        self.con.commit()
        return added

    def export_csv(self, path: str = CSV_PATH):
        rows = self.con.execute(
            "SELECT * FROM jobs ORDER BY scraped_at DESC"
        ).fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(self.COLS)
            writer.writerows(rows)
        return len(rows)

    def export_json(self, path: str = "jobs.json"):
        rows = self.con.execute(
            "SELECT * FROM jobs ORDER BY scraped_at DESC"
        ).fetchall()
        jobs = [dict(zip(self.COLS, row)) for row in rows]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2, ensure_ascii=False)
        return len(jobs)

    def stats(self) -> dict:
        total = self.con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_source = self.con.execute(
            "SELECT source, COUNT(*) FROM jobs GROUP BY source"
        ).fetchall()
        return {"total": total, "by_source": dict(by_source)}

# ── Email ──────────────────────────────────────────────────────────────────────

def send_summary_email(
    new_jobs: list[JobListing],
    stats: dict,
    config: configparser.ConfigParser,
):
    host      = config.get("email", "smtp_host",  fallback="").strip()
    port      = config.getint("email", "smtp_port", fallback=587)
    sender    = config.get("email", "sender",     fallback="").strip()
    password  = config.get("email", "password",   fallback="").strip()
    recipient = config.get("email", "recipient",  fallback="").strip()

    if not all([host, sender, password, recipient]):
        return  # email not configured, skip silently

    ran_at = datetime.now(timezone.utc).strftime("%d %b %Y at %H:%M UTC")

    if not new_jobs:
        subject = "Job Scraper — no new listings found"
        body = (
            f"The scraper ran on {ran_at} and found no new listings.\n\n"
            f"All-time total: {stats['total']} jobs in database."
        )
    else:
        subject = f"Job Scraper — {len(new_jobs)} new listing{'s' if len(new_jobs) != 1 else ''}"
        lines = [
            f"Found {len(new_jobs)} new listing(s) on {ran_at}:\n",
            "─" * 40,
        ]
        for job in new_jobs:
            lines.append(f"\n{job.title}")
            lines.append(f"{job.company} — {job.location}")
            if job.salary:
                lines.append(f"Salary: {job.salary}")
            if job.date_posted:
                lines.append(f"Posted: {job.date_posted}")
            lines.append(f"Source: {job.source}")
            lines.append(f"{job.url}")
            lines.append("─" * 40)
        lines.append(f"\nAll-time total: {stats['total']} jobs in database.")
        by_source = "  |  ".join(
            f"{src}: {count}" for src, count in stats["by_source"].items()
        )
        lines.append(f"By source: {by_source}")
        body = "\n".join(lines)

    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        print(f"  Email sent → {recipient}")
    except Exception as e:
        print(f"  Email failed: {e}")

# ── Runner ─────────────────────────────────────────────────────────────────────

def run(
    location: str,
    distance: int,
    keywords_list: list[str],
    relevant_terms: set[str],
    config: configparser.ConfigParser,
    reed_key: str = "",
    adzuna_id: str = "",
    adzuna_key: str = "",
    export_json: bool = False,
):
    sources_active = []
    if reed_key:
        sources_active.append("Reed")
    if adzuna_id and adzuna_key:
        sources_active.append("Adzuna")

    print(f"\n{'─' * 50}")
    print(f"  Job scraper starting")
    print(f"  Location : {location} (within {distance} miles)")
    print(f"  Keywords : {len(keywords_list)} search terms")
    print(f"  Sources  : {', '.join(sources_active)}")
    if relevant_terms:
        print(f"  Filter   : {', '.join(sorted(relevant_terms))}")
    else:
        print(f"  Filter   : none (keeping all results)")
    print(f"{'─' * 50}\n")

    store = JobStore()
    all_listings: list[JobListing] = []

    for i, keywords in enumerate(keywords_list, 1):
        print(f"[{i}/{len(keywords_list)}] '{keywords}'")
        before = len(all_listings)

        if reed_key:
            results = scrape_reed(keywords, location, reed_key, distance)
            print(f"      Reed    → {len(results)}")
            all_listings += results

        if adzuna_id and adzuna_key:
            results = scrape_adzuna(keywords, location, adzuna_id, adzuna_key)
            print(f"      Adzuna  → {len(results)}")
            all_listings += results

        print(f"      subtotal: {len(all_listings) - before} raw")

    print(f"\n{'─' * 50}")
    print(f"  Raw total             : {len(all_listings)}")

    if relevant_terms:
        filtered = [
            j for j in all_listings
            if any(term in (j.title + " " + j.description).lower() for term in relevant_terms)
        ]
        print(f"  After relevance filter: {len(filtered)}")
    else:
        filtered = all_listings

    new = store.add_new(filtered)
    print(f"  New (deduped)         : {len(new)}")

    csv_count = store.export_csv()
    print(f"  CSV exported          : {csv_count} total rows → {CSV_PATH}")

    if export_json:
        json_count = store.export_json()
        print(f"  JSON exported         : {json_count} total rows → jobs.json")

    stats = store.stats()
    print(f"\n  All-time totals by source:")
    for source, count in stats["by_source"].items():
        print(f"    {source:<10} {count}")
    print(f"    {'TOTAL':<10} {stats['total']}")
    print(f"{'─' * 50}\n")

    send_summary_email(new, stats, config)

# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    create_default_config()
    create_default_keywords()

    parser = argparse.ArgumentParser(
        description="Scrape UK job listings from Reed and Adzuna.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Config is loaded from config.ini — CLI args override it.\n"
            "Keywords are loaded from keywords.txt by default.\n"
            "\n"
            "API keys:\n"
            "  Reed   : https://www.reed.co.uk/developers\n"
            "  Adzuna : https://developer.adzuna.com\n"
        ),
    )
    parser.add_argument(
        "--location",
        metavar="LOCATION",
        help="Override location from config.ini",
    )
    parser.add_argument(
        "--distance",
        metavar="MILES",
        help="Override search radius from config.ini",
    )
    parser.add_argument(
        "--keywords",
        metavar="FILE",
        help="Path to a keywords file (default: keywords.txt)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also export results as jobs.json",
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        default=CONFIG_PATH,
        help=f"Path to config file (default: {CONFIG_PATH})",
    )

    args = parser.parse_args()
    config = load_config(args.config)

    reed_key   = config.get("keys", "reed_key",   fallback="").strip()
    adzuna_id  = config.get("keys", "adzuna_id",  fallback="").strip()
    adzuna_key = config.get("keys", "adzuna_key", fallback="").strip()

    location     = (args.location or config.get("search", "location",       fallback="")).strip()
    distance_str = (args.distance  or config.get("search", "distance_miles", fallback="")).strip()
    export_json  = args.json or config.getboolean("options", "export_json",  fallback=False)

    keywords_file  = args.keywords or KEYWORDS_PATH
    keywords_list  = load_keywords(keywords_file)
    relevant_terms = load_relevant_terms(config)

    distance, errors = validate(
        location, distance_str, keywords_list, keywords_file,
        reed_key, adzuna_id, adzuna_key,
    )

    if errors:
        print("\n  Configuration errors — please fix the following:\n")
        for e in errors:
            print(e)
        print(f"\n  Edit {args.config} and {keywords_file} then rerun.\n")
        sys.exit(1)

    run(
        location=location,
        distance=distance,
        keywords_list=keywords_list,
        relevant_terms=relevant_terms,
        config=config,
        reed_key=reed_key,
        adzuna_id=adzuna_id,
        adzuna_key=adzuna_key,
        export_json=export_json,
    )