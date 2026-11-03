# Ahsan RemoteRadar

Personal job agent for remote SEO roles. It runs by itself on GitHub Actions
(`.github/workflows/radar.yml`) and publishes a password-locked, read-only dashboard
on GitHub Pages.

- No personal data is stored in this repository. The database, profile and CV live in a
  private Hugging Face dataset; keys are GitHub secrets.
- Everything on the published page is encrypted; it opens only with the dashboard password.
- Emails are never sent automatically: the tool makes Gmail drafts, and the owner sends them.

Run tests: `pip install -r requirements.txt && pytest`
