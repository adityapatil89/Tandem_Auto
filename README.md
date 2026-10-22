# Tandem Auto

A working web app for a car dealership's business sales: build a list of target companies, score them, draft a first message, and track test drives and quotes through four stages (Discover, Deep dive, Demand, Deal).

Flask + SQLite backend, plain HTML/CSS/JS front end. No build step.

## Run it on your computer
```
python -m venv .venv
.venv\Scripts\activate        # Windows (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
set SECRET_KEY=any-long-random-text   # Mac/Linux: export SECRET_KEY=...
python app.py
```
Open http://127.0.0.1:5000 and create an account.

## What works
- Sign up, log in, log out (hashed passwords, HttpOnly session cookie, login rate limit).
- Each account only sees its own companies.
- Add companies by hand or import a CSV (columns: name or company, optional city, website, type, signal). Columns that look like personal contact details (email, phone, LinkedIn, contact) are ignored. Up to 800 rows.
- Rule-based scoring with the reasons stored and shown (see `score_company` in `app.py`).
- Stage flow, notes, editable draft messages, Budget/Authority/Need/Timing, test drives, quotes, won value.
- Download deals as CSV or JSON.
- "Book a demo" emails are stored in the `demo_requests` table.

## Optional AI research (untested)
Set `ANTHROPIC_API_KEY` (and optionally `ANTHROPIC_MODEL`) and a "Research this company" button appears in Deep dive. It asks the Claude API to look up the company on the public web and appends labelled notes. This path was written but could not be tested without a key, so try it on a few companies first and always verify the output.

## Optional company search (Tavily)
Set `TAVILY_API_KEY` (free key from app.tavily.com) and a "Find companies" panel appears in Discover. The user enters a city and buyer type, the server searches the public web through Tavily, and the results are shown as candidates. Nothing is saved until the user edits the name if needed and clicks Add. The server drops results that look like news, directories, listicles, blogs or reports (see `JUNK_DOMAINS` and `looks_like_company_site` in `app.py`) and keeps one result per website, but it is a general web search, so a few non-companies can still get through. Edit the lists in `app.py` if you see the same junk site twice. Each user is limited to `FIND_DAILY_LIMIT` searches a day (default 10) so one person cannot use up a shared free allowance. Tested with a simulated Tavily response; not yet tested against the real service.

## Environment variables
| Name | Purpose |
|---|---|
| `SECRET_KEY` | Required in production. Signs login cookies. |
| `DATABASE_PATH` | Where the SQLite file lives (default `tandem.db`). |
| `HTTPS` | Set to `1` once served over HTTPS so cookies are marked Secure. |
| `ANTHROPIC_API_KEY` | Turns on the optional research button. |
| `TAVILY_API_KEY` | Turns on the "Find companies" search in Discover. |
| `FIND_DAILY_LIMIT` | Searches per user per day (default 10). |
| `FIND_COUNTRY` | Word added to the search query (default `India`). |

## Deploy
GitHub Pages cannot run this, because it needs a Python server. Use a host that runs Python, for example Render, Railway or Fly.io:
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Set `SECRET_KEY` and `HTTPS=1`.
- SQLite needs a persistent disk. Point `DATABASE_PATH` at it, or move to PostgreSQL if you run several servers.

## Before real customers use it
- Add password reset and email verification.
- Add Privacy and Terms pages and check your data-protection obligations.
- Back up the database.
