# Job Scraper

A simple UK job listing scraper that pulls results from Reed and Adzuna, deduplicates them, exports to CSV, and optionally emails you a summary.

---

## Requirements

- Python 3.8+
- That's it — dependencies install automatically on first run

---

## Setup

### 1. First run

```bash
python job_scraper.py
```

This installs the required packages and generates two config files:

- `config.ini` — API keys and search settings
- `keywords.txt` — your search terms

### 2. Get API keys

Both are free:

| Source | Sign up | Free limit |
|--------|---------|------------|
| Reed | https://www.reed.co.uk/developers | 1,000 req/day |
| Adzuna | https://developer.adzuna.com | 250 req/day |

You only need one, but using both gives broader coverage.

### 3. Edit config.ini

```ini
[keys]
reed_key = your-reed-key-here
adzuna_id = your-adzuna-id-here
adzuna_key = your-adzuna-key-here

[search]
# Location to search, e.g. Scotland, London, United Kingdom
location = Scotland
# Search radius in miles
distance_miles = 50

[filter]
# Optional: comma-separated terms to filter results by title/description
# Leave blank to keep all results
terms = unity, game, gameplay, unreal, xr, vr, multiplayer

[email]
# Optional: fill in to receive a summary email after each run
smtp_host = smtp.gmail.com
smtp_port = 587
sender = you@gmail.com
password = your-app-password-here
recipient = you@gmail.com

[options]
# Set to true to also export a jobs.json alongside jobs.csv
export_json = false
```

> **Note:** if you generated `config.ini` with an older version of the script, it may be missing the `[filter]` and `[email]` sections. Just add them manually.

### 4. Edit keywords.txt

One search term per line. Lines starting with `#` are ignored.

```
# Example keywords
Unity developer
gameplay programmer
XR developer
senior Unity engineer
```

### 5. Run

```bash
python job_scraper.py
```

---

## Email setup (optional)

The scraper can email you a summary of new listings after each run. It uses Gmail SMTP.

1. Enable 2-Step Verification on your Google account
2. Go to **myaccount.google.com → Security → 2-Step Verification → App Passwords**
3. Create an App Password (name it "Job Scraper") and copy the 16-character code
4. Add your details to the `[email]` section of `config.ini`

If the `[email]` section is left blank the scraper runs normally and just skips the email step.

---

## Output

| File | Description |
|------|-------------|
| `jobs.csv` | All results, newest first. Opens in Excel. |
| `jobs.db` | SQLite database — results accumulate across runs |
| `jobs.json` | Optional — enable with `export_json = true` in config.ini |

Results are deduplicated across runs and sources — the same listing won't appear twice even if it shows up in multiple keyword searches.

---

## CLI options

All settings in `config.ini` can be overridden on the command line for one-off searches.

```bash
python job_scraper.py --location "United Kingdom" --distance 100
python job_scraper.py --keywords other_keywords.txt
python job_scraper.py --config other_config.ini
python job_scraper.py --json
```

| Flag | Description |
|------|-------------|
| `--location` | Override search location |
| `--distance` | Override search radius in miles |
| `--keywords` | Use a different keywords file |
| `--config` | Use a different config file |
| `--json` | Also export jobs.json |

---

## Scheduling (run automatically)

### Windows — Task Scheduler

1. Open Task Scheduler and create a new Basic Task
2. Set the trigger (e.g. daily at 8am)
3. Set the action to **Start a Program**:
   - Program: `python`
   - Arguments: `job_scraper.py`
   - Start in: `C:\path\to\your\JobScraper`

### Mac / Linux — cron

```bash
# Run daily at 8am
0 8 * * * cd /path/to/JobScraper && python job_scraper.py
```

---

## Tips

- **Broad location, tight keywords** works better than the reverse. Try `Scotland` or `United Kingdom` rather than a specific city if results are thin.
- **Reed is UK-native** and tends to surface more relevant results than Adzuna for UK roles.
- **The relevance filter** (`[filter] terms`) is useful if your keywords are broad and pulling in noise. Leave it blank to keep everything.
- The deduplication means repeat runs only ever add genuinely new listings — safe to run as often as you like.