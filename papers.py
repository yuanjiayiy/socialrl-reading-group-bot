"""Finding papers: link parsing, Semantic Scholar / arXiv lookups, PDFs, and Claude helpers."""
from __future__ import annotations

import base64
import datetime as dt
import html
import io
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
FIELDS = "paperId,title,abstract,authors,year,venue,publicationDate,externalIds,openAccessPdf,citationCount,url"
UA = {"User-Agent": "reading-group-bot/1.0 (paper reading group)"}

SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-haiku-4-5-20251001")
PICK_MODEL = os.environ.get("PICK_MODEL", "claude-sonnet-5-5")
DIGEST_MODEL = os.environ.get("DIGEST_MODEL", "claude-sonnet-5-5")
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "0"))  # 0 = no age filter

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
        "publicationDate": p.get("publicationDate"),
        "citationCount": p.get("citationCount"),
        "pdf_url": (p.get("openAccessPdf") or {}).get("url"),
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
        detail = f"{r.status_code} {r.text[:300]}" if r is not None else "no response"
        raise RuntimeError(f"Semantic Scholar recommendations failed: {detail}")
    out = []
    for p in r.json().get("recommendedPapers", []):
        rec = to_record(p)
        if not rec or len(rec["title"]) < 15 or not (rec["abstract"] or has_pdf(rec)):
            continue  # drop junk records
        if MAX_AGE_DAYS and rec["publicationDate"]:
            if (dt.date.today() - dt.date.fromisoformat(rec["publicationDate"])).days > MAX_AGE_DAYS:
                continue
        out.append(rec)
    out.sort(key=lambda rec: not has_pdf(rec))  # stable: keep S2 order, PDFs first
    return out


def has_pdf(rec: dict) -> bool:
    return bool(rec.get("arxiv") or rec.get("pdf_url"))


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

    def about(c: dict) -> str:
        cites = c.get("citationCount")
        bits = [c.get("venue") or "preprint", c.get("publicationDate") or str(c.get("year") or "n.d."),
                f"{cites} citations" if cites is not None else "citations unknown"]
        return ", ".join(bits)
    listing = "\n\n".join(f"[{i}] {c['title']} ({about(c)})\n{(c.get('abstract') or '(no abstract)')[:900]}"
                          for i, c in enumerate(pool))
    prompt = (f"You pick one paper a day for an ML research reading group. Today is {dt.date.today().isoformat()}.\n\n"
              "Papers the group has proposed, read, or liked:\n" + "\n".join(f"- {t}" for t in liked[:50]) +
              "\n\nPapers the group disliked:\n" + ("\n".join(f"- {t}" for t in disliked[:30]) or "(none)") +
              (f"\n\nGroup focus: {focus}" if focus else "") +
              f"\n\nCandidates:\n{listing}\n\n"
              "Choose the candidate the group would most want to read in depth and discuss. Favor papers with a "
              "substantive, transferable method over surveys, position papers, or incremental benchmark results. "
              "Use citation count as a signal of impact, judged relative to the paper's age: a paper from the last "
              "few months can't have many citations yet, so don't let an older, much-cited paper win on that alone. "
              "Reply with only JSON: {\"pick\": index}")
    try:
        # Sonnet 5.5 thinks by default and thinking counts toward max_tokens, so a small cap leaves no text.
        i = json.loads(re.search(r"\{.*\}", ask(PICK_MODEL, prompt, 4000), re.S).group(0))["pick"]
        if isinstance(i, int) and 0 <= i < len(pool):
            return pool[i]
    except Exception as e:
        print(f"[pick] falling back to first candidate: {e}", file=sys.stderr)
    return pool[0]


# ------------------------------------------------------------------- digest

def fetch_pdf(rec: dict) -> bytes | None:
    urls = []
    if rec.get("arxiv"):
        urls.append(f"https://arxiv.org/pdf/{rec['arxiv']}")
    if rec.get("pdf_url"):
        urls.append(rec["pdf_url"])
    for u in urls:
        try:
            r = requests.get(u, headers=UA, timeout=90)
            if r.ok and r.content[:5] == b"%PDF-" and len(r.content) < 30_000_000:
                return trim_pdf(r.content)
        except requests.RequestException as e:
            print(f"[pdf] {u}: {e}", file=sys.stderr)
    return None


def trim_pdf(data: bytes, max_pages: int = 90) -> bytes:
    """Keep the PDF under the API's page limit (main text + early appendix)."""
    try:
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) <= max_pages:
            return data
        w = PdfWriter()
        for page in reader.pages[:max_pages]:
            w.add_page(page)
        buf = io.BytesIO()
        w.write(buf)
        return buf.getvalue()
    except Exception:
        return data


DIGEST_INSTRUCTIONS = """You are a sharp research colleague writing a method-level digest for an ML reading group of \
expert readers. They already know the field, so skip generic background. They want to understand HOW the method works \
well enough to explain it to someone else or reimplement a toy version.

Papers the group has proposed, read, or liked (use these to tailor the "why" and to name the closest prior work they know):
{liked}
{focus}
Write exactly three tagged sections and nothing else:

<tldr>2-3 sentences: the problem, the key idea, and the headline result with real numbers.</tldr>

<why>1-2 sentences connecting this paper to specific papers from the group's list (name them). If the link is loose, say so.</why>

<digest>
Use these parts, each starting with a bold label on its own line:
*Setup & problem* - the formal setting (inputs, outputs, assumptions) and the specific limitation of prior work being attacked.
*Method* - the core mechanism step by step: architecture, objective, and/or algorithm. Write the key equations in plain text \
(e.g. L = E_t[ ||eps - eps_theta(x_t, t)||^2 ]) and define every symbol. Say what happens at training time vs inference time, \
and give rough cost/complexity if relevant.
*What's actually new* - the precise delta vs the closest prior work (named). Separate the genuine idea from engineering.
*Details that matter* - data, scale, hyperparameters, and tricks that the ablations show are load-bearing.
*Evidence* - main experiments, strongest baselines, key numbers, and the single most informative ablation.
*Caveats* - weaknesses, confounds, missing baselines, or claims you'd verify before trusting.
*Try it* - the smallest experiment that would reproduce the core idea; code link if the paper gives one.
</digest>

Formatting rules (this is posted to Slack): use Slack mrkdwn only - *bold* with single asterisks, _italic_, \
"•" for bullets, `code` for symbols/equations. No # headings, no tables, no LaTeX. Keep the digest around 500-900 words. \
Never invent numbers or details; if the paper doesn't say, write "not reported"."""


def write_digest(rec: dict, pdf: bytes | None, liked: list[str], focus: str) -> dict:
    meta = (f"Title: {rec['title']}\nAuthors: {', '.join(rec['authors'][:12])}\n"
            f"Venue/date: {rec.get('venue') or 'preprint'} {rec.get('publicationDate') or rec.get('year') or ''}\n")
    instructions = DIGEST_INSTRUCTIONS.format(
        liked="\n".join(f"- {t}" for t in liked[:40]) or "(none)",
        focus=f"\nThe group's stated focus: {focus}\n" if focus else "")
    content: list = []
    if pdf:
        content.append({"type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf",
                                   "data": base64.standard_b64encode(pdf).decode()}})
        content.append({"type": "text", "text": meta + "\nThe full paper is attached.\n\n" + instructions})
    else:
        content.append({"type": "text", "text": meta + f"\nAbstract (full text unavailable):\n{rec.get('abstract')}\n\n"
                        + instructions + "\n\nOnly the abstract is available: be explicit about what you can't tell."})
    # Thinking shares max_tokens with the answer, so leave plenty of room for a ~900-word digest.
    msg = claude().messages.create(model=DIGEST_MODEL, max_tokens=16000, messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if b.type == "text")

    def tag(name: str) -> str:
        m = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
        return to_mrkdwn(m.group(1).strip()) if m else ""
    return {"tldr": tag("tldr"), "why": tag("why"), "digest": tag("digest") or to_mrkdwn(text)}


def to_mrkdwn(t: str) -> str:
    t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", t)
    t = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", t, flags=re.M)
    t = re.sub(r"^\s*[-*]\s+", "• ", t, flags=re.M)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", t)
    return t


def chunks(text: str, limit: int = 2900) -> list[str]:
    out, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            out.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 2 > limit:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return [c for c in out if c.strip()]
