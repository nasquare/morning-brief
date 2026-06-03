"""
Morning Brief generator.

Runs once a day inside GitHub Actions. It:
  1. Reads your RSS feeds (from feeds.txt) and, if set up, your Gmail newsletters.
  2. Groups stories that are about the same thing (so you don't read duplicates).
  3. Sorts everything into the topics you care about.
  4. Writes the result to data.json, which your dashboard page reads.

You normally never need to touch this file. The two things you MIGHT tweak
later are clearly marked CONFIG sections just below.
"""

import os
import re
import json
import email
import imaplib
import datetime as dt
from email.header import decode_header
from html import unescape

import feedparser
from rapidfuzz import fuzz


# ============================================================
# CONFIG 1 — the topics you care about (edit keywords anytime)
# Stories get sorted into the FIRST category whose keywords they
# match best. Anything that matches nothing lands in "Everything else".
# Order here = order they appear on your dashboard.
# ============================================================
CATEGORIES = [
    ("Crypto & Web3", [
        "crypto", "bitcoin", "btc", "ethereum", "eth", "web3", "nft",
        "defi", "token", "onchain", "on-chain", "stablecoin", "airdrop",
        "wallet", "solana", "blockchain", "dao", "memecoin",
    ]),
    ("Art & Curation", [
        "art", "artist", "gallery", "curator", "curation", "exhibition",
        "museum", "auction", "sculpture", "painting", "biennale",
        "collector", "studio visit", "art fair",
    ]),
    ("Gaming & Esports", [
        "gaming", "game", "esports", "esport", "twitch", "steam",
        "tournament", "valorant", "league of legends", "publisher",
        "player count", "console", "playstation", "xbox", "nintendo",
    ]),
    ("Markets & Trading", [
        "markets", "stocks", "stock", "equities", "fed", "rates",
        "inflation", "earnings", "bonds", "treasury", "trading",
        "macro", "recession", "yield", "ipo", "etf", "s&p",
    ]),
    ("CPG & Lifestyle", [
        "cpg", "consumer", "brand", "retail", "beverage", "spirits",
        "liqueur", "dtc", "packaging", "fmcg", "wellness", "fashion",
    ]),
    ("Design & Architecture", [
        "design", "architecture", "interior", "furniture", "studio",
        "typography", "minimalism", "modernist", "spatial", "material",
    ]),
]

# ============================================================
# CONFIG 2 — small knobs
# ============================================================
DAYS_BACK = 2          # how far back to look for stories
SIM_THRESHOLD = 78     # 0-100; higher = stricter about calling two stories "the same"
GMAIL_LABEL = "Newsletters"  # the Gmail label your newsletters get filed under
MAX_PER_CATEGORY = 30
# ============================================================


def clean_text(html_or_text: str) -> str:
    """Strip HTML tags / collapse whitespace so we have plain readable text."""
    if not html_or_text:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_or_text,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def first_sentences(text: str, n_chars: int = 320) -> str:
    """A short, clean excerpt for the card summary."""
    text = text.strip()
    if len(text) <= n_chars:
        return text
    cut = text[:n_chars]
    # try to end on a sentence boundary
    last = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if last > 120:
        return cut[:last + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def within_window(published) -> bool:
    if not published:
        return True  # keep undated items rather than silently dropping them
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS_BACK)
    try:
        ts = dt.datetime(*published[:6], tzinfo=dt.timezone.utc)
    except Exception:
        return True
    return ts >= cutoff


# ---------------------------------------------------------------
# Source 1: RSS / web feeds
# ---------------------------------------------------------------
def load_feed_urls(path="feeds.txt"):
    urls = []
    if not os.path.exists(path):
        return urls
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def pull_rss():
    items = []
    for url in load_feed_urls():
        try:
            parsed = feedparser.parse(url)
            source = clean_text(parsed.feed.get("title", url)) or url
            for e in parsed.entries:
                pub = e.get("published_parsed") or e.get("updated_parsed")
                if not within_window(pub):
                    continue
                body = clean_text(
                    e.get("summary", "")
                    or (e.get("content", [{}])[0].get("value", "") if e.get("content") else "")
                )
                date_iso = ""
                if pub:
                    try:
                        date_iso = dt.datetime(*pub[:6], tzinfo=dt.timezone.utc).isoformat()
                    except Exception:
                        pass
                items.append({
                    "title": clean_text(e.get("title", "")) or "(untitled)",
                    "url": e.get("link", ""),
                    "source": source,
                    "date": date_iso,
                    "body": body,
                })
        except Exception as err:
            print(f"  ! feed failed ({url}): {err}")
    print(f"RSS: pulled {len(items)} items")
    return items


# ---------------------------------------------------------------
# Source 2: Gmail (only runs if the secrets + label exist)
# ---------------------------------------------------------------
def _decode(value):
    if not value:
        return ""
    parts = decode_header(value)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out


def pull_gmail():
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        print("Gmail: skipped (no secrets set yet)")
        return []

    items = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(user, pw)
        # try the label folder; if it doesn't exist, fall back to the inbox
        status, _ = M.select(f'"{GMAIL_LABEL}"', readonly=True)
        if status != "OK":
            print(f'Gmail: label "{GMAIL_LABEL}" not found, reading INBOX instead')
            M.select("INBOX", readonly=True)

        since = (dt.datetime.now() - dt.timedelta(days=DAYS_BACK)).strftime("%d-%b-%Y")
        status, data = M.search(None, f'(SINCE "{since}")')
        ids = data[0].split() if data and data[0] else []
        for num in ids[-80:]:  # cap so a busy inbox can't blow up
            status, msg_data = M.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode(msg.get("Subject"))
            sender = _decode(msg.get("From"))
            # friendly source name = the part before <email>
            source = re.sub(r"<.*?>", "", sender).strip().strip('"') or sender

            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/plain":
                        body = part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace")
                        break
                if not body:
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            body = clean_text(part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8", errors="replace"))
                            break
            else:
                body = msg.get_payload(decode=True).decode(
                    msg.get_content_charset() or "utf-8", errors="replace")
                if msg.get_content_type() == "text/html":
                    body = clean_text(body)

            date_iso = ""
            try:
                date_iso = email.utils.parsedate_to_datetime(msg.get("Date")).isoformat()
            except Exception:
                pass

            items.append({
                "title": subject or "(no subject)",
                "url": "",  # email has no public link; dashboard says "from your inbox"
                "source": source,
                "date": date_iso,
                "body": clean_text(body),
            })
        M.logout()
    except Exception as err:
        print(f"Gmail: error ({err}) — continuing without it")
    print(f"Gmail: pulled {len(items)} items")
    return items


# ---------------------------------------------------------------
# De-duplication: group stories that are about the same thing
# ---------------------------------------------------------------
def same_story(a, b):
    """True if two items look like coverage of the same story.
    Titles carry the strongest signal for news, so they're weighted heavily,
    with the body used as a tie-breaker/confirmation."""
    title_sim = fuzz.token_set_ratio(a["title"].lower(), b["title"].lower())
    body_sim = fuzz.token_set_ratio(a["body"][:200].lower(), b["body"][:200].lower())
    if title_sim >= SIM_THRESHOLD:                 # near-identical headline
        return True
    if title_sim >= 62 and body_sim >= 50:         # similar headline + confirming body
        return True
    return False


def dedupe(items):
    clusters = []  # each: {"rep": item, "members": [items]}
    seen_urls = set()
    for item in items:
        u = item.get("url", "").strip().rstrip("/")
        if u and u in seen_urls:
            for c in clusters:
                if any(m.get("url", "").strip().rstrip("/") == u for m in c["members"]):
                    c["members"].append(item)
                    break
            continue
        if u:
            seen_urls.add(u)

        placed = False
        for c in clusters:
            if same_story(item, c["rep"]):
                c["members"].append(item)
                if len(item["body"]) > len(c["rep"]["body"]):
                    c["rep"] = item  # keep the most detailed version as representative
                placed = True
                break
        if not placed:
            clusters.append({"rep": item, "members": [item]})
    return clusters


# ---------------------------------------------------------------
# Categorize + score
# ---------------------------------------------------------------
def categorize(cluster):
    rep = cluster["rep"]
    haystack = (rep["title"] + " " + rep["body"]).lower()
    title_l = rep["title"].lower()
    best_cat, best_score = "Everything else", 0
    for name, keywords in CATEGORIES:
        score = 0
        for kw in keywords:
            hits = haystack.count(kw)
            if hits:
                score += hits + (2 if kw in title_l else 0)  # title hits weigh more
        if score > best_score:
            best_cat, best_score = name, score
    return best_cat, best_score


def build():
    raw = pull_rss() + pull_gmail()
    clusters = dedupe(raw)
    print(f"Grouped {len(raw)} items into {len(clusters)} stories")

    buckets = {name: [] for name, _ in CATEGORIES}
    buckets["Everything else"] = []

    for cid, c in enumerate(clusters):
        rep = c["rep"]
        cat, score = categorize(c)
        also_in = sorted({m["source"] for m in c["members"]
                          if m["source"] != rep["source"]})
        buckets[cat].append({
            "id": f"s{cid}",
            "title": rep["title"],
            "url": rep["url"],
            "source": rep["source"],
            "date": rep["date"],
            "summary": first_sentences(rep["body"]) or "(no preview available)",
            "also_in": also_in,
            "n_sources": len(c["members"]),
            "_score": score,
        })

    ordered = []
    for name, _ in CATEGORIES:
        items = buckets[name]
        items.sort(key=lambda x: (x["n_sources"], x["date"]), reverse=True)
        if items:
            ordered.append({"name": name, "items": items[:MAX_PER_CATEGORY]})
    if buckets["Everything else"]:
        misc = buckets["Everything else"]
        misc.sort(key=lambda x: (x["n_sources"], x["date"]), reverse=True)
        ordered.append({"name": "Everything else", "items": misc[:MAX_PER_CATEGORY]})

    now = dt.datetime.now(dt.timezone.utc)
    sgt = now + dt.timedelta(hours=8)
    out = {
        "generated_at": now.isoformat(),
        "generated_at_label": sgt.strftime("%a %d %b, %-I:%M %p") + " SGT",
        "total_stories": sum(len(b["items"]) for b in ordered),
        "categories": ordered,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Wrote data.json — {out['total_stories']} stories across {len(ordered)} sections")


if __name__ == "__main__":
    build()
