# Ahsan RemoteRadar

Personal job agent for remote SEO roles. It runs by itself on GitHub Actions
(`.github/workflows/radar.yml`) and publishes a password-locked, read-only dashboard
on GitHub Pages.

- No personal data is stored in this repository. The database, profile and CV live in a
  private Hugging Face dataset; keys are GitHub secrets.
- Everything on the published page is encrypted; it opens only with the dashboard password.
- Emails are never sent automatically: the tool makes Gmail drafts, and the owner sends them.

Run tests: `pip install -r requirements.txt && pytest`

## v6 (18 Nov 2026)

- More international sources: Arbeitnow, Remote First Jobs, Real Work From Anywhere, 4 Day Week,
  Workew and the Hacker News "Who is hiring?" thread (ideas from Ahsan483/job-hunter, MIT).
- **Google search** (`radar/scrapers/google.py`): Gemini searches Google for SEO posts on LinkedIn,
  Indeed, Rozee.pk and company sites, at most every `GOOGLE_SEARCH_EVERY_HOURS` (10). A link is kept
  only if its site is one Google really returned. Off: `GOOGLE_SEARCH=false`.
- **Cover letters** for good "apply on site" jobs (score ≥ `COVER_LETTER_MIN_SCORE`, 70; up to
  `COVER_LETTER_DAILY`, 10, a day), from `profile.md` facts only, with the same numbers check as
  emails. Shown with a Copy button on both dashboards; the Mac dashboard can also write one on demand.
  Off: `COVER_LETTERS=false`.
- Job tables list the newest jobs first (best match first within a day).

## v6.1 (18 Nov 2026)

- Gemini limits: a 429 now says which quota Google reported (per day or per minute). A daily limit stops
  that model for the rest of the run at once instead of retrying for minutes.
- Google search tries a second model when the first one's free search limit is used up
  (`GEMINI_SEARCH_MODEL`, default the check model; `GEMINI_SEARCH_FALLBACK_MODEL`, default the write model).
  If every search fails, it runs again on the next run instead of waiting 10 hours.
- Jobs whose AI check failed on a Gemini limit or a busy server are checked again on the next run (up to 15).
- A cover letter that is not written always says why in Latest activity (profile not ready, daily limit, off).
- Each run ends with "AI calls today: check N, search N, write N".
