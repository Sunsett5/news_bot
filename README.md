# News Topic Bot

Watches a list of RSS/Atom feeds for articles matching your topics, and
posts new matches to a Discord channel via webhook.

## Setup (5 minutes)

1. **Create a new GitHub repo** and push these files to it (`bot.py`,
   `config.json`, `requirements.txt`, `.github/workflows/news-bot.yml`).

2. **Get a Discord webhook URL**
   - In Discord: Server Settings → Integrations → Webhooks → New Webhook
   - Pick the channel you want notifications in, copy the Webhook URL.

3. **Get an LLM API key** (used to check whether a keyword-matched
   article is genuinely about your topic, and to write a summary)
   - **Gemini (default):** go to [Google AI Studio](https://aistudio.google.com/apikey),
     create a free API key. No credit card needed. Free tier is generous
     enough for this bot's volume (a handful of articles a day).
   - **Typhoon (Thai-specialized alternative):** sign up at
     [opentyphoon.ai](https://opentyphoon.ai), grab a free API key from
     the playground. Worth trying if you find Gemini's Thai summaries
     or relevance judgments lacking nuance — just change
     `"llm_provider"` in `config.json` to `"typhoon"`.

4. **Add secrets to your repo**
   - In your GitHub repo: Settings → Secrets and variables → Actions →
     New repository secret
   - Add `DISCORD_WEBHOOK_URL` (the webhook URL from step 2)
   - Add `GEMINI_API_KEY` (or `TYPHOON_API_KEY` if using Typhoon)

5. **Edit `config.json`**
   - `feeds`: list of RSS/Atom feed URLs you want monitored. Most news
     sites have one — often at `/feed`, `/rss`, or linked in the site
     footer. Paste the ones you want here.
   - `topics`: keywords/phrases to match against article titles and
     summaries (case-insensitive, substring match).
   - `llm_provider`: `"gemini"` (default) or `"typhoon"`.
   - `summary_language`: `"th"` (default), `"en"`, or `"match"` to
     mirror whatever language the article itself is in.

6. **Commit and push.** The workflow runs automatically every 30 minutes
   (edit the cron schedule in `news-bot.yml` to change frequency), or
   you can trigger it manually from the Actions tab → "News Bot" →
   "Run workflow".

## How it works

- `bot.py` fetches each feed, checks titles/summaries for your keywords,
  and posts anything new to Discord as an embed with a link.
- `seen.json` tracks which stories have already been notified, so you
  won't get repeats. The workflow commits this file back to the repo
  after each run so state persists between scheduled runs.

### Two-stage filtering: keywords, then LLM relevance check

Keyword matching alone gives false positives — an article can mention
a topic word in passing without actually being *about* it. So the bot
now works in two stages:

1. **Keyword pre-filter** (free, instant): titles/summaries are
   checked against your `topics` list. This is just a candidate filter.
2. **LLM relevance check** (only runs on candidates): the bot fetches
   the *full* article text (not just the RSS snippet), and asks the
   LLM to (a) confirm whether the article is genuinely about the
   flagged topic(s), and (b) write a short summary. If the LLM says
   it's not actually relevant, the article is silently dropped — no
   notification, no summary. If it is relevant, the summary is
   included right in the Discord message.

This keeps LLM usage low (only on already-keyword-matched articles,
which should be a small fraction of everything in your feeds), so a
personal-scale bot like this comfortably fits inside Gemini's or
Typhoon's free tier.

**If the LLM call fails** (rate limit, network issue, bad response),
the bot fails safe: it treats the article as relevant using the
keyword match alone and skips the summary, rather than silently
dropping a real match due to an API hiccup.

**Choosing a provider:** both are free at this volume, so the practical
way to choose is to try both for a few days on the same Thai-language
feeds and see whose summaries and relevance judgments you trust more.
Gemini is a large general-purpose multilingual model; Typhoon is
purpose-built for Thai and may catch nuance (idiom, local context)
that a general model misses. Neither is better in every case.

### Handling updated/republished versions of the same story

Outlets frequently re-publish a story with a new headline as it develops
("Wildfire forces evacuations" → "Wildfire forces evacuations as winds
pick up"). The bot doesn't just dedupe on exact article ID — it also
fuzzy-compares each new article's title against recently-seen stories:

- **No similar story found** → posted as a `🆕 New match`.
- **Similar story found, and it's been a while** → posted as a
  `🔄 Update to a story you were notified about`, so you can tell it's
  a follow-up rather than a fresh story.
- **Similar story found, but you were just notified recently** → held
  back, so minor copy-edits from the same outlet don't spam you.

Two settings in `config.json` control this:

- `similarity_threshold` (0–1, default `0.6`): how similar two titles
  need to be to count as "the same story." Lower = catches more
  updates but risks false matches between unrelated stories; higher =
  stricter, may miss reworded updates. `0.6` comfortably separates
  genuine updates (typically 0.6–0.9 similarity) from unrelated
  articles (typically under 0.3).
- `update_cooldown_hours` (default `6`): minimum gap between update
  notifications for the same story.

## Notes / things you might want to tweak later

- **Keyword matching is simple substring matching.** If your topics are
  broad or fuzzy (e.g. "AI safety debates" rather than a company name),
  you may get false positives/negatives — this can later be swapped for
  an LLM relevance check per headline if you want smarter filtering.
- **Rate limits:** Discord webhooks are rate-limited; the script sleeps
  1 second between messages, which is enough for typical volumes.
- **Adding more feeds:** just add more URLs to the `feeds` array in
  `config.json` — no code changes needed.
- **Cross-feed matching:** updates are currently caught regardless of
  which feed they came from (a title from one outlet can match a
  similar title from another), since the comparison runs against all
  recently-seen stories, not just ones from the same feed.
