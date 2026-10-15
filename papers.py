"""Finding papers: link parsing, Semantic Scholar / arXiv lookups, and Claude helpers."""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests
from anthropic import Anthropic

S2 = "https://api.semanticscholar.org"
S2_KEY = os.environ.get("S2_API_KEY", "").strip()
FIELDS = "paperId,title,abstract,authors,year,venue,publicationDate,externalIds,url"
UA = {"User-Agent": "reading-group-bot/1.0 (paper reading group)"}

SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-haiku-4-5-20251001")
PICK_MODEL = os.environ.get("PICK_MODEL", "claude-sonnet-5-5")

# ----------------------------------------------------------------- link parsing

ARXIV_ID = r"([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/[0-9]{7})"
ARXIV_URL = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf|html)|huggingface\.co/papers|alphaxiv\.org/(?:abs|overview))/" + ARXIV_ID, re.I)
ARXIV_BARE = re.compile(r"(?<![\w./])([0-9]{4}\.[0-9]{4,5})(?:v\d+)?(?![\w/])")
DOI = re.compile(r"(10\.\d{4,9}/[^\s<>|\"']+)")
S2_URL = re.compile(r"semanticscholar\.org/(?:paper|p)/(?:[^/\s]+/)?([0-9a-f]{40})", re.I)
URL = re.compile(r"https?://[^\s<>|]+")

# Links that count as "someone shared a paper" without needing a command.
PAPER_HOSTS = re.compile(
    r"https?://(?:[\w-]+\.)*(?:arxiv\.org|doi\.org|semanticscholar\.org|openreview\.net|aclanthology\.org|"
    r"proceedings\.mlr\.press|proceedings\.neurips\.cc|papers\.nips\.cc|neurips\.cc/virtual|openaccess\.thecvf\.com|"
    r"biorxiv\.org|medrxiv\.org|dl\.acm\.org|ieeexplore\.ieee\.org|huggingface\.co/papers|alphaxiv\.org|"
    r"nature\.com/articles|science\.org/doi|pnas\.org/doi|jmlr\.org/papers|ojs\.aaai\.org|ijcai\.org/proceedings)"
    r"[^\s<>|]*|https?://[^\s<>|]+\.pdf\b", re.I)


def clean_slack(text: str) -> str:
    """Slack wraps links as <https://x|label>; unwrap them."""
    return re.sub(r"<(https?://[^>|]+)(?:\|[^>]*)?>", r"\1", text or "")


def strip_version(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id)


def paper_links(text: str) -> list[str]:
    """URLs in a message that look like papers."""
    seen, out = set(), []
    for m in PAPER_HOSTS.finditer(clean_slack(text)):
        u = m.group(0).rstrip(".,;)>")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def any_links(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;)>") for m in URL.finditer(clean_slack(text))]


def ref_for(text: str) -> str | None:
    """Turn a link or id into a Semantic Scholar lookup key, if possible."""
    m = ARXIV_URL.search(text)
    if m:
        return "ARXIV:" + strip_version(m.group(1))
    m = S2_URL.search(text)
    if m:
        return m.group(1).lower()
    m = DOI.search(text)
    if m:
        doi = m.group(1).rstrip(".,;)")
        am = re.match(r"10\.48550/arxiv\.(.+)", doi, re.I)
        return "ARXIV:" + strip_version(am.group(1)) if am else "DOI:" + doi
    m = ARXIV_BARE.search(text)
    if m:
        return "ARXIV:" + m.group(1)
    return None


def norm_title(t: str) -> str:
    t = re.sub(r"^\s*\[[^\]]+\]\s*", "", t or "")  # drop "[2506.15799] " prefix
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


# ------------------------------------------------------------- Semantic Scholar

_s2_down = False


def s2_unavailable() -> bool:
    """True once Semantic Scholar has kept refusing requests this run. Later calls then return None at once."""
    return _s2_down


def s2(method: str, path: str, **kw) -> requests.Response | None:
    global _s2_down
    if _s2_down:
        return None
    headers = dict(UA)
    if S2_KEY:
        headers["x-api-key"] = S2_KEY
    attempts = 4
    for attempt in range(attempts):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            r = requests.request(method, S2 + path, headers=headers, timeout=30, **kw)
        except requests.RequestException:
            continue
        if r.status_code not in (429, 500, 502, 503, 504):
            return r
    # Without this, a rate-limited run spends minutes per paper and hits the job timeout.
    _s2_down = True
    print(f"[s2] giving up on {path}; skipping Semantic Scholar for the rest of this run", file=sys.stderr)
    print("::warning::Semantic Scholar is rate-limiting the bot, so lookups and the daily pick were skipped "
          "and will be retried next run." + ("" if S2_KEY else " Add an S2_API_KEY secret to fix this."))
    return None


def s2_lookup(ref: str) -> dict | None:
    r = s2("GET", f"/graph/v1/paper/{requests.utils.quote(ref, safe=':/')}", params={"fields": FIELDS})
    return r.json() if r is not None and r.status_code == 200 else None


def s2_title(title: str) -> dict | None:
    r = s2("GET", "/graph/v1/paper/search/match", params={"query": title[:300], "fields": FIELDS})
    if r is None or r.status_code != 200:
        return None
    data = r.json().get("data") or []
    if not data:
        return None
    # guard against loose matches
    a, b = set(norm_title(title).split()), set(norm_title(data[0].get("title", "")).split())
    return data[0] if a and len(a & b) / len(a | b) >= 0.6 else None


def arxiv_api(arxiv_id: str) -> dict | None:
    try:
        r = requests.get("https://export.arxiv.org/api/query", params={"id_list": arxiv_id},
                         headers=UA, timeout=30)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entry = ET.fromstring(r.text).find("a:entry", ns)
        if entry is None or entry.find("a:title", ns) is None:
            return None
        clean = lambda s: re.sub(r"\s+", " ", s or "").strip()
        return {"title": clean(entry.find("a:title", ns).text),
                "abstract": clean(entry.find("a:summary", ns).text),
                "authors": [{"name": clean(a.find("a:name", ns).text)} for a in entry.findall("a:author", ns)],
                "year": int((entry.find("a:published", ns).text or "0")[:4] or 0) or None}
    except Exception:
        return None


BLOCKED_TITLES = re.compile(r"verifying your browser|just a moment|access denied|attention required|captcha|"
                            r"page not found|^404|sign in|log in", re.I)


def openreview_meta(url: str) -> dict:
    m = re.search(r"[?&]id=([\w\-]+)", url)
    if not m:
        return {}
    for api in ("https://api2.openreview.net/notes", "https://api.openreview.net/notes"):
        try:
            notes = requests.get(api, params={"id": m.group(1)}, headers=UA, timeout=30).json().get("notes") or []
        except Exception:
            continue
        if notes:
            c = notes[0].get("content", {})
            val = lambda k: (c.get(k) or {}).get("value") if isinstance(c.get(k), dict) else c.get(k)
            if val("title"):
                return {"title": val("title"), "abstract": val("abstract") or ""}
    return {}


def page_meta(url: str) -> dict:
    """Title/abstract from a paper landing page's citation_* or og: meta tags."""
    if "openreview.net" in url:
        return openreview_meta(url)
    try:
        r = requests.get(url, headers=UA, timeout=30)
        if "html" not in r.headers.get("content-type", ""):
            return {}
        text = r.text[:400_000]
    except requests.RequestException:
        return {}

    def meta(*names: str) -> str:
        for n in names:
            m = re.search(rf'<meta[^>]+(?:name|property)=["\']{re.escape(n)}["\'][^>]*content=["\']([^"\']+)', text, re.I) \
                or re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:name|property)=["\']{re.escape(n)}["\']', text, re.I)
            if m:
                return html.unescape(m.group(1)).strip()
        return ""
    title = meta("citation_title", "og:title", "twitter:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    if not title or BLOCKED_TITLES.search(title):
        return {}
    return {"title": title, "abstract": meta("citation_abstract", "og:description", "description", "twitter:description")}


def to_record(p: dict | None, url: str | None = None) -> dict | None:
    if not p or not p.get("title"):
        return None
    arxiv = (p.get("externalIds") or {}).get("ArXiv") or p.get("arxiv")
    return {
        "paperId": p.get("paperId"),
        "arxiv": arxiv,
        "title": p["title"].strip(),
        "abstract": (p.get("abstract") or "").strip(),
        "authors": [a.get("name", "") for a in (p.get("authors") or [])],
        "year": p.get("year"),
        "venue": p.get("venue") or "",
        "url": f"https://arxiv.org/abs/{arxiv}" if arxiv else (url or p.get("url") or ""),
    }


def resolve(text: str, allow_any_url: bool = False) -> dict | None:
    """A link, id or title -> paper record. Returns None if it can't be identified."""
    text = clean_slack(text).strip()
    ref = ref_for(text)
    urls = any_links(text)
    url = urls[0] if urls else None
    if ref:
        rec = to_record(s2_lookup(ref), url if not ref.startswith("ARXIV:") else None)
        if ref.startswith("ARXIV:"):
            aid = ref[6:]
            if rec is None:
                a = arxiv_api(aid)
                rec = to_record({**a, "arxiv": aid}) if a else None
            elif not rec["abstract"]:
                a = arxiv_api(aid)
                if a:
                    rec["abstract"] = a["abstract"]
        if rec:
            return rec
    if url and (allow_any_url or PAPER_HOSTS.match(url)):
        m = page_meta(url)
        if m.get("title"):
            found = s2_title(m["title"])
            rec = to_record(found, url) if found else None
            if rec:
                rec["url"] = rec["url"] if rec.get("arxiv") else url
                rec["abstract"] = rec["abstract"] or m.get("abstract", "")
                return rec
            return {"paperId": None, "arxiv": None, "title": m["title"], "abstract": m.get("abstract", ""),
                    "authors": [], "year": None, "venue": "", "url": url}
        return None
    if not url and len(text) >= 8:  # plain title
        return to_record(s2_title(text))
    return None


def recommendations(positive_ids: list[str], negative_ids: list[str]) -> list[dict]:
    r = s2("POST", "/recommendations/v1/papers", params={"fields": FIELDS, "limit": 100},
           json={"positivePaperIds": positive_ids, "negativePaperIds": negative_ids})
    if r is None or r.status_code != 200:
        raise RuntimeError(f"Semantic Scholar recommendations failed: {r and r.status_code} {r and r.text[:300]}")
    out = []
    for p in r.json().get("recommendedPapers", []):
        rec = to_record(p)
        if rec and len(rec["title"]) >= 15 and (rec["abstract"] or rec["arxiv"]):
            out.append(rec)
    return out


# ------------------------------------------------------------------- Claude

_client = None


def claude() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()
    return _client


def ask(model: str, prompt: str, max_tokens: int = 300) -> str:
    msg = claude().messages.create(model=model, max_tokens=max_tokens,
                                   messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def summary20(rec: dict) -> str:
    """At most 20 words on what the paper does."""
    if not rec.get("abstract"):
        return ""
    try:
        text = ask(SUMMARY_MODEL,
                   f"Title: {rec['title']}\nAbstract: {rec['abstract'][:4000]}\n\n"
                   "In at most 20 words, say what this paper does and its key idea or result. Be concrete. "
                   "No preamble, no quotes, don't start with 'This paper'.", 100)
    except Exception as e:
        print(f"[summary] {e}", file=sys.stderr)
        return ""
    words = text.replace("\n", " ").strip().strip('"').split()
    return " ".join(words[:20]).rstrip(",;:") + ("…" if len(words) > 20 else "")


def pick(cands: list[dict], liked: list[str], disliked: list[str], focus: str) -> dict:
    pool = cands[:20]
    if len(pool) == 1:
        return pool[0]
    listing = "\n\n".join(f"[{i}] {c['title']} ({c.get('year') or 'n.d.'})\n{(c.get('abstract') or '')[:800]}"
                          for i, c in enumerate(pool))
    prompt = ("You pick one paper a day for an ML research reading group.\n\n"
              "Papers the group has proposed, read, or liked:\n" + "\n".join(f"- {t}" for t in liked[:50]) +
              "\n\nPapers the group disliked:\n" + ("\n".join(f"- {t}" for t in disliked[:30]) or "(none)") +
              (f"\n\nGroup focus: {focus}" if focus else "") +
              f"\n\nCandidates:\n{listing}\n\n"
              "Pick the single candidate this group would most enjoy discussing: a substantive, interesting idea "
              "close to their interests, not a survey or a minor benchmark tweak. Reply with only JSON: {\"pick\": index}")
    try:
        # Sonnet 5.5 thinks by default and thinking counts toward max_tokens, so a small cap leaves no text.
        i = json.loads(re.search(r"\{.*\}", ask(PICK_MODEL, prompt, 4000), re.S).group(0))["pick"]
        if isinstance(i, int) and 0 <= i < len(pool):
            return pool[i]
    except Exception as e:
        print(f"[pick] falling back to first candidate: {e}", file=sys.stderr)
    return pool[0]
