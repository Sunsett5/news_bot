#!/usr/bin/env python3
"""
News topic bot.

Reads config.json (feeds + topic keywords), fetches each RSS/Atom feed,
keyword-pre-filters entries, then for candidates:
  1. Fetches the full article text (not just the RSS snippet)
  2. Asks an LLM (Gemini by default, or Typhoon) whether the article is
     genuinely about the flagged topic(s), and to write a short summary
  3. Only notifies on Discord if the LLM confirms real relevance

Handles updated/republished versions of the same story via fuzzy title
matching against recently-seen stories, with a cooldown so re-edits
from the same outlet don't spam you.
"""

import difflib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import trafilatura

from llm import check_relevance_and_summarize

CONFIG_PATH = Path("config.json")
SEEN_PATH = Path("seen.json")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

MAX_SEEN_ENTRIES = 3000
DEFAULT_SIMILARITY_THRESHOLD = 0.6
DEFAULT_UPDATE_COOLDOWN_HOURS = 6
COMPARISON_WINDOW_DAYS = 10
ARTICLE_FETCH_TIMEOUT = 15


def now_ts():
    return datetime.now(timezone.utc).timestamp()


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_seen():
    if SEEN_PATH.exists():
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_seen(seen_list):
    trimmed = seen_list[-MAX_SEEN_ENTRIES:]
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


def entry_id(entry):
    return entry.get("id") or entry.get("link") or (
        entry.get("title", "") + entry.get("published", "")
    )


def normalize_title(title):
    title = title.lower()
    title = re.sub(r"[^a-z0-9\s\u0E00-\u0E7F]", " ", title)  # keep Thai script too
    title = re.sub(r"\s+", " ", title).strip()
    return title


def matches_topics(entry, topics):
    haystack = f"{entry.get('title', '')} {entry.get('summary', '')}".lower()
    return [t for t in topics if t.lower() in haystack]


def find_similar_story(norm_title, seen_list, threshold, window_days):
    cutoff = now_ts() - window_days * 86400
    best_record = None
    best_score = 0.0
    for record in seen_list:
        if record.get("first_seen", 0) < cutoff:
            continue
        score = difflib.SequenceMatcher(None, norm_title, record.get("norm_title", "")).ratio()
        if score > best_score:
            best_score = score
            best_record = record
    if best_score >= threshold:
        return best_record
    return None


def fetch_article_text(url, rss_fallback_text):
    """
    Try to pull the full article body from the page. Falls back to the
    RSS summary/content if the fetch or extraction fails (paywalls,
    network issues, unusual page structure, etc).
    """
    try:
        downloaded = trafilatura.fetch_url(url, no_ssl=True)
        if downloaded:
            extracted = trafilatura.extract(downloaded, favor_recall=True)
            if extracted and len(extracted.strip()) > 200:
                return extracted
    except Exception as e:
        print(f"Article fetch failed for {url}: {e}", file=sys.stderr)
    return rss_fallback_text


def send_discord_notification(entry, matched_topics, summary, is_update=False):
    title = entry.get("title", "Untitled")
    link = entry.get("link", "")
    source = entry.get("source_feed_title", "")
    topics_str = ", ".join(matched_topics)

    label = "🔄 Update to a story you were notified about" if is_update else "🆕 New match"

    description_parts = [f"**{label}**", f"**Matched topic(s):** {topics_str}"]
    if summary:
        description_parts.append(f"**Summary:** {summary}")

    embed = {
        "title": title[:256],
        "url": link,
        "description": "\n".join(description_parts)[:4096],
    }
    if source:
        embed["footer"] = {"text": source}

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
    if resp.status_code >= 300:
        print(f"Discord webhook error {resp.status_code}: {resp.text}", file=sys.stderr)


def main():
    if not DISCORD_WEBHOOK_URL:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable not set.", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    feeds = config.get("feeds", [])
    topics = config.get("topics", [])
    threshold = config.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD)
    cooldown_hours = config.get("update_cooldown_hours", DEFAULT_UPDATE_COOLDOWN_HOURS)
    llm_provider = config.get("llm_provider", "gemini")
    summary_language = config.get("summary_language", "th")

    seen = load_seen()
    seen_ids = {r["id"] for r in seen}

    new_count = 0
    update_count = 0
    dropped_count = 0

    for feed_url in feeds:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"Failed to parse feed {feed_url}: {e}", file=sys.stderr)
            continue

        feed_title = parsed.feed.get("title", feed_url) if hasattr(parsed, "feed") else feed_url

        for entry in parsed.entries:
            uid = entry_id(entry)
            if uid in seen_ids:
                continue  # byte-for-byte repeat

            keyword_matched = matches_topics(entry, topics)
            if not keyword_matched:
                continue

            title = entry.get("title", "")
            link = entry.get("link", "")
            rss_text = entry.get("summary", "") or entry.get("description", "")
            entry["source_feed_title"] = feed_title

            # Pull full article text, then ask the LLM whether it's
            # genuinely about the flagged topic(s), and to summarize it.
            article_text = fetch_article_text(link, rss_text)
            llm_result = check_relevance_and_summarize(
                article_title=title,
                article_text=article_text,
                candidate_topics=keyword_matched,
                provider=llm_provider,
                summary_language=summary_language,
            )

            norm_title = normalize_title(title)

            if not llm_result.get("is_relevant"):
                # Keyword coincidence, not actually about the topic -> drop silently.
                seen.append({
                    "id": uid, "norm_title": norm_title, "title": title,
                    "link": link, "first_seen": now_ts(), "last_notified": 0,
                })
                seen_ids.add(uid)
                dropped_count += 1
                continue

            confirmed_topics = llm_result.get("matched_topics") or keyword_matched
            summary = llm_result.get("summary", "")

            similar = find_similar_story(norm_title, seen, threshold, COMPARISON_WINDOW_DAYS)

            if similar is None:
                send_discord_notification(entry, confirmed_topics, summary, is_update=False)
                seen.append({
                    "id": uid, "norm_title": norm_title, "title": title,
                    "link": link, "first_seen": now_ts(), "last_notified": now_ts(),
                })
                seen_ids.add(uid)
                new_count += 1
                time.sleep(1)
            else:
                elapsed_hours = (now_ts() - similar.get("last_notified", 0)) / 3600
                if elapsed_hours >= cooldown_hours:
                    send_discord_notification(entry, confirmed_topics, summary, is_update=True)
                    similar["last_notified"] = now_ts()
                    similar["title"] = title
                    similar["link"] = link
                    update_count += 1
                    time.sleep(1)
                seen.append({
                    "id": uid, "norm_title": norm_title, "title": title,
                    "link": link, "first_seen": now_ts(), "last_notified": similar["last_notified"],
                })
                seen_ids.add(uid)

    save_seen(seen)
    print(
        f"Done. {new_count} new stories, {update_count} updates sent, "
        f"{dropped_count} keyword-matched-but-not-relevant articles dropped."
    )


if __name__ == "__main__":
    main()
