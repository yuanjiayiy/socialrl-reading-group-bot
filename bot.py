#!/usr/bin/env python3
"""
Reading-group Slack bot.

1. Someone shares a paper link in the channel -> it's added to 'Paper Voting'
   with title+link, a 20-word description, who proposed it, today's date, and
   empty vote checkboxes.
2. Once a day it recommends one paper with a TL;DR and a method digest
   Claude writes from the full PDF (in a thread). 👍 / 👎 on that post steer
   future picks; ➕ adds it to the voting sheet.
3. "@paperbot read <title or link>" moves a paper from 'Paper Voting' to
   'Already read'.
4. Rows in 'Paper Voting' added more than 4 weeks ago are grouped and collapsed.

Run modes:  python bot.py sync | python bot.py daily [--force]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import papers as P
from sheets import Sheets

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
SEEDS_PATH = os.path.join(ROOT, "seeds.txt")

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "15nOrrRpz6f7ChNmNPMNRtV6D5pJ8APCf_c5hC1vxWuQ")
VOTING_GID = int(os.environ.get("VOTING_GID", "1149238197"))
READ_GID = int(os.environ.get("READ_GID", "575211331"))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles"))
COLLAPSE_AFTER_DAYS = int(os.environ.get("COLLAPSE_AFTER_DAYS", "28"))
COLLAPSE_UNDATED = os.environ.get("COLLAPSE_UNDATED", "false").lower() == "true"
FOCUS = os.environ.get("GROUP_FOCUS", "").strip()
MAX_POSITIVES = 100
FEEDBACK_DAYS = 14
NEW_LOOKUPS_PER_RUN = 40  # Semantic Scholar lookups for sheet rows, to keep runs short

UP = {"+1", "thumbsup", "heart", "fire", "star", "star-struck", "raised_hands"}
DOWN = {"-1", "thumbsdown"}
ADD = {"heavy_plus_sign", "ballot_box_with_ballot", "inbox_tray"}

HELP = """*How to use me*
• *Share a paper link* (arXiv, OpenReview, DOI, ACL, PMLR, …) in this channel and I'll add it to the voting sheet. To add any other link (a blog post, say): `@paperbot add <link>`. If I can't read a title: `@paperbot add <link> title: <title>`
• `@paperbot read <part of the title, or link>` moves a paper from voting to *Already read*. Add `presenter: Name` or `by @someone` to record who presented.
• `@paperbot more <link>` / `@paperbot less <link>` steers recommendations without touching the sheet.
• `@paperbot recommend` sends a recommendation now.
• On my daily pick: 👍 more like this · 👎 less like this · ➕ add it to the voting sheet
I check the channel every 30 minutes, so replies aren't instant."""


def today() -> dt.date:
    return dt.datetime.now(TZ).date()


def date_str(d: dt.date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


# ------------------------------------------------------------------ state

def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    for k, v in {"positives": {}, "negatives": {}, "sent": {}, "recs": {}, "lookup": {},
                 "seed_cache": {}, "last_seen_ts": None, "last_daily": None,
                 "force_daily": False, "collapse_sig": None}.items():
        s.setdefault(k, v)
    return s


def save_state(s: dict) -> None:
    with open(STATE_PATH + ".tmp", "w") as f:
        json.dump(s, f, indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(STATE_PATH + ".tmp", STATE_PATH)


def steer(s: dict, rec: dict, polarity: str, source: str) -> None:
    pid = rec.get("paperId")
    if not pid:
        return
    s["positives"].pop(pid, None)
    s["negatives"].pop(pid, None)
    if polarity in ("pos", "neg"):
        (s["positives"] if polarity == "pos" else s["negatives"])[pid] = {
            "title": rec["title"], "added": dt.datetime.now(TZ).isoformat(), "source": source}


# ------------------------------------------------------------------ slack

class Slack:
    def __init__(self):
        self.c = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        self.channel = os.environ["SLACK_CHANNEL_ID"]
        me = self.c.auth_test()
        self.user_id = me["user_id"]
        self._names: dict[str, str] = {}

    def post(self, text: str, blocks=None, thread_ts=None) -> str:
        return self.c.chat_postMessage(channel=self.channel, text=text, blocks=blocks, thread_ts=thread_ts,
                                       unfurl_links=False, unfurl_media=False)["ts"]

    def history(self, oldest: str) -> list[dict]:
        out, cursor = [], None
        while True:
            r = self.c.conversations_history(channel=self.channel, oldest=oldest, limit=200, cursor=cursor)
            out += r["messages"]
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return sorted(out, key=lambda m: float(m["ts"]))

    def reactions(self, ts: str) -> list[dict]:
        try:
            return (self.c.reactions_get(channel=self.channel, timestamp=ts, full=True).get("message") or {}) \
                .get("reactions") or []
        except SlackApiError:
            return []

    def name(self, user_id: str) -> str:
        """First name, matching how the sheet records people."""
        if user_id not in self._names:
            try:
                prof = self.c.users_info(user=user_id)["user"]
                p = prof.get("profile", {})
                full = p.get("real_name") or prof.get("real_name") or p.get("display_name") or prof.get("name", "")
                self._names[user_id] = full.split()[0] if full else user_id
            except SlackApiError:
                self._names[user_id] = user_id
        return self._names[user_id]


def human(msg: dict, slack: Slack) -> bool:
    return not msg.get("bot_id") and msg.get("user") not in (None, slack.user_id) \
        and msg.get("subtype") in (None, "thread_broadcast", "file_share")


# ------------------------------------------------------- 1. share -> voting

def add_paper(rec: dict, proposer: str, sheets: Sheets, s: dict) -> str:
    existing = sheets.find_existing(rec)
    if existing:
        tab, row = existing
        if tab == "voting":
            who = f" (proposed by {row['proposer']})" if row.get("proposer") else ""
            return f"👀 *{rec['title']}* is already on the voting sheet{who}."
        when = f" on {date_str(row['date'])}" if row.get("date") else ""
        return f"📚 *{rec['title']}* was already read{when}."
    desc = P.summary20(rec)
    sheets.add_to_voting(rec, desc, proposer, date_str(today()))
    steer(s, rec, "pos", "shared")
    return f"📝 Added to the voting sheet: *<{rec['url']}|{rec['title']}>*" + (f"\n_{desc}_" if desc else "")


# ------------------------------------------------------- 3. move to read

def find_in_voting(query: str, rows: list[dict]) -> list[dict]:
    ref = P.ref_for(query)
    if ref and ref.startswith("ARXIV:"):
        hits = [r for r in rows if r.get("arxiv") == ref[6:]]
        if hits:
            return hits
    links = P.any_links(query)
    if links:
        hits = [r for r in rows if r.get("url") and r["url"].rstrip("/") == links[0].rstrip("/")]
        if hits:
            return hits
        rec = P.resolve(query, allow_any_url=True)
        if rec:
            query = rec["title"]
    qn = P.norm_title(query)
    if not qn:
        return []
    exact = [r for r in rows if P.norm_title(r["title"]) == qn]
    if exact:
        return exact
    sub = [r for r in rows if qn in P.norm_title(r["title"])]
    if sub:
        return sub
    qw = set(qn.split())
    scored = [(len(qw & set(P.norm_title(r["title"]).split())) / len(qw), r) for r in rows]
    best = max((sc for sc, _ in scored), default=0)
    return [r for sc, r in scored if sc == best and sc >= 0.6]


def move_command(arg: str, slack: Slack, sheets: Sheets) -> str:
    presenter = ""
    m = re.search(r"\bby\s+<@(\w+)>", arg)
    if m:
        presenter = slack.name(m.group(1))
        arg = arg[:m.start()] + arg[m.end():]
    m = re.search(r"\bpresent(?:er|ed by)\s*[:=]?\s*(.+)$", arg, re.I)
    if m:
        presenter = re.sub(r"<@(\w+)>", lambda x: slack.name(x.group(1)), m.group(1)).strip()
        arg = arg[:m.start()]
    arg = arg.strip(" ,.-")
    if not arg:
        return "Tell me which paper, e.g. `@paperbot read diffusion policy`."
    hits = find_in_voting(arg, sheets.voting_papers())
    if not hits:
        return f"I couldn't find a paper matching “{arg}” on the voting sheet."
    if len(hits) > 1:
        return "Which one?\n" + "\n".join(f"• {h['title']}" for h in hits[:6]) + \
            "\nReply with `@paperbot read` and a more specific part of the title."
    paper = hits[0]
    sheets.move_to_read(paper, presenter, date_str(today()))
    who = presenter or paper.get("presenter")
    return f"✅ Moved *{paper['title']}* to Already read" + (f" (presented by {who})." if who else ".")


# ---------------------------------------------------------- message router

def cant_read(url: str) -> str:
    return (f"❓ I couldn't read the title from {url}. Tell me and I'll add it:\n"
            f"`@paperbot add {url} title: <paper title>`")


def handle(msg: dict, slack: Slack, sheets: Sheets, s: dict) -> None:
    text = msg.get("text", "")
    ts = msg["ts"]
    mention = f"<@{slack.user_id}>"
    if mention in text:
        cmd = text.replace(mention, " ").strip()
        m = re.match(r"^(\w+)\b[:,]?\s*(.*)$", cmd, re.S)
        verb, arg = (m.group(1).lower(), m.group(2)) if m else ("", "")
        if verb in ("read", "done", "discussed", "voted", "move", "presented"):
            reply = move_command(P.clean_slack(arg), slack, sheets)
        elif verb in ("add", "propose", "share"):
            arg = P.clean_slack(arg)
            tm = re.search(r"\btitle\s*:\s*(.+)$", arg, re.I | re.S)
            links = P.any_links(arg[:tm.start()] if tm else arg)
            if tm:  # explicit title, for links I can't read (e.g. OpenReview, PDFs)
                title = tm.group(1).strip()
                rec = P.resolve(title) or {"paperId": None, "arxiv": None, "title": title, "abstract": "",
                                           "authors": [], "year": None, "venue": "", "url": ""}
                if links:
                    rec["url"] = links[0]
                reply = add_paper(rec, slack.name(msg["user"]), sheets, s)
            else:
                replies = []
                for t in (links or ([arg] if arg.strip() else [])):
                    rec = P.resolve(t, allow_any_url=True)
                    replies.append(add_paper(rec, slack.name(msg["user"]), sheets, s) if rec else cant_read(t))
                reply = "\n".join(replies) or "Give me a link, e.g. `@paperbot add https://arxiv.org/abs/…`"
        elif verb in ("more", "less", "like", "dislike", "seed"):
            pol = "neg" if verb in ("less", "dislike") else "pos"
            replies = []
            for t in (P.any_links(arg) or [arg]):
                rec = P.resolve(t, allow_any_url=True)
                if rec and rec.get("paperId"):
                    steer(s, rec, pol, "slack")
                    replies.append(f"{'👍 More' if pol == 'pos' else '👎 Less'} like *{rec['title']}*")
                elif P.s2_unavailable():
                    replies.append(f"⏳ Semantic Scholar isn't responding right now, so I couldn't look up {t}. Try again later.")
                else:
                    replies.append(f"❓ Couldn't find {t} on Semantic Scholar")
            reply = "\n".join(replies)
        elif verb in ("recommend", "now", "pick", "suggest"):
            s["force_daily"] = True
            reply = "On it 👇"
        else:
            reply = HELP
        if reply:
            slack.post(reply, thread_ts=ts)
        return

    # Not addressed to the bot: auto-add any paper links.
    replies = []
    for url in P.paper_links(text):
        rec = P.resolve(url)
        replies.append(add_paper(rec, slack.name(msg["user"]), sheets, s) if rec else cant_read(url))
    if replies:
        slack.post("\n".join(replies), thread_ts=ts)


def sync_messages(slack: Slack, sheets: Sheets, s: dict) -> None:
    oldest = s["last_seen_ts"] or f"{(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).timestamp():.6f}"
    newest = oldest
    for msg in slack.history(oldest):
        if float(msg["ts"]) <= float(oldest):
            continue
        newest = max(newest, msg["ts"], key=float)
        if human(msg, slack):
            try:
                handle(msg, slack, sheets, s)
            except Exception as e:
                print(f"[slack] failed on message {msg['ts']}: {e}", file=sys.stderr)
                try:
                    slack.post(f"⚠️ Something went wrong handling this: `{str(e)[:200]}`", thread_ts=msg["ts"])
                except SlackApiError:
                    pass
        s["last_seen_ts"] = newest
        save_state(s)
    s["last_seen_ts"] = newest


# ------------------------------------------------- 2. reactions on daily picks

def sync_reactions(slack: Slack, sheets: Sheets, s: dict) -> None:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=FEEDBACK_DAYS)
    for ts, r in list(s["recs"].items()):
        if dt.datetime.fromtimestamp(float(ts), dt.timezone.utc) < cutoff:
            continue
        up = down = 0
        adders = []
        for rx in slack.reactions(ts):
            people = [u for u in rx.get("users", []) if u != slack.user_id]
            name = rx["name"].split("::")[0]
            if name in UP:
                up += len(people)
            elif name in DOWN:
                down += len(people)
            elif name in ADD:
                adders += people
        fb = "up" if up > down else "down" if down > up else None
        if fb != r.get("feedback"):
            r["feedback"] = fb
            rec = {"paperId": r["paperId"], "title": r["title"]}
            steer(s, rec, {"up": "pos", "down": "neg"}.get(fb, "remove"), "reaction")
            print(f"[feedback] {fb} ({up}👍/{down}👎) on {r['title']}")
        if adders and not r.get("proposed"):
            r["proposed"] = True
            rec = P.resolve(r["url"], allow_any_url=True) or r
            slack.post(add_paper(rec, slack.name(adders[0]), sheets, s), thread_ts=ts)


# ------------------------------------------------------- 2. daily pick

def seeds(s: dict) -> None:
    if not os.path.exists(SEEDS_PATH):
        return
    for line in open(SEEDS_PATH):
        line = line.strip()
        if not line or line.startswith("#") or line in s["seed_cache"]:
            continue
        neg = line.startswith("-")
        rec = P.resolve(line.lstrip("- ").strip(), allow_any_url=True)
        if P.s2_unavailable():  # a failed lookup, not "no such paper": retry next run
            return
        s["seed_cache"][line] = rec.get("paperId") if rec else None
        if rec:
            steer(s, rec, "neg" if neg else "pos", "seeds.txt")


def sheet_positives(sheets: Sheets, s: dict) -> tuple[dict[str, tuple[str, str]], set[str], set[str]]:
    """Papers on either tab -> {paperId: (title, date)}, plus arXiv ids and titles to exclude."""
    rows = [(p, "voting") for p in sheets.voting_papers()] + [(p, "read") for p in sheets.read_papers()]
    budget = NEW_LOOKUPS_PER_RUN
    out, arxivs, titles = {}, set(), set()
    for p, _ in rows:
        if p.get("arxiv"):
            arxivs.add(p["arxiv"])
        titles.add(P.norm_title(p["title"]))
        key = p.get("arxiv") or P.norm_title(p["title"]) or p.get("url")
        if not key:
            continue
        if key not in s["lookup"]:
            if budget <= 0 or P.s2_unavailable():
                continue
            budget -= 1
            q = f"arXiv:{p['arxiv']}" if p.get("arxiv") else (p.get("url") or p["title"])
            rec = P.resolve(q) if (p.get("arxiv") or p.get("url")) else P.resolve(p["title"])
            if rec is None and p["title"] and not p.get("arxiv"):
                rec = P.resolve(p["title"])
            if P.s2_unavailable():  # a failed lookup, not "no such paper": retry next run
                continue
            s["lookup"][key] = rec.get("paperId") if rec else None
        pid = s["lookup"][key]
        if pid:
            out[pid] = (p["title"], p["date"].isoformat() if p.get("date") else "")
    return out, arxivs, titles


def daily(slack: Slack, sheets: Sheets, s: dict) -> bool:
    """Post the paper of the day. False means Semantic Scholar was unavailable and nothing was posted."""
    from_sheet, sheet_arxiv, sheet_titles = sheet_positives(sheets, s)
    if P.s2_unavailable():
        return False
    pos = {pid: (t, d) for pid, (t, d) in from_sheet.items()}
    for pid, e in s["positives"].items():
        pos[pid] = (e["title"], e.get("added", "")[:10])
    for pid in s["negatives"]:
        pos.pop(pid, None)
    if not pos:
        slack.post("I don't have any papers to learn from yet. Share a paper link here or add some to seeds.txt.")
        return True
    ranked = sorted(pos.items(), key=lambda kv: kv[1][1], reverse=True)
    pos_ids = [pid for pid, _ in ranked[:MAX_POSITIVES]]
    neg_ids = [pid for pid, _ in sorted(s["negatives"].items(), key=lambda kv: kv[1].get("added", ""),
                                        reverse=True)[:MAX_POSITIVES]]

    seen = set(s["sent"]) | set(pos) | set(s["negatives"])
    sent_titles = {P.norm_title(v["title"]) for v in s["sent"].values()}
    try:
        recommended = P.recommendations(pos_ids, neg_ids)
    except RuntimeError:
        if P.s2_unavailable():
            return False
        raise
    cands = [c for c in recommended
             if c["paperId"] not in seen and c.get("arxiv") not in sheet_arxiv
             and P.norm_title(c["title"]) not in sheet_titles | sent_titles]
    if not cands:
        slack.post("I couldn't find a new paper today. 👍 a few more papers to give me more to go on.")
        return True
    liked = [t for _, (t, _) in ranked]
    rec = P.pick(cands, liked, [e["title"] for e in s["negatives"].values()], FOCUS)
    pdf = P.fetch_pdf(rec)
    try:
        d = P.write_digest(rec, pdf, liked, FOCUS)
    except Exception as e:
        if not pdf:
            raise
        print(f"[daily] PDF route failed ({e}); retrying with abstract", file=sys.stderr)
        pdf = None
        d = P.write_digest(rec, None, liked, FOCUS)
    ts = post_pick(slack, rec, d, full_text=pdf is not None)
    s["sent"][rec["paperId"]] = {"title": rec["title"], "date": today().isoformat()}
    s["recs"][ts] = {"paperId": rec["paperId"], "title": rec["title"], "url": rec["url"], "feedback": None}
    print(f"[daily] {rec['title']}")
    return True


def post_pick(slack: Slack, rec: dict, d: dict, full_text: bool) -> str:
    links = []
    if rec.get("arxiv"):
        links += [f"<https://arxiv.org/abs/{rec['arxiv']}|arXiv>", f"<https://arxiv.org/pdf/{rec['arxiv']}|PDF>"]
    elif rec.get("pdf_url"):
        links.append(f"<{rec['pdf_url']}|PDF>")
    links.append(f"<https://www.semanticscholar.org/paper/{rec['paperId']}|Semantic Scholar>")
    authors = ", ".join(rec["authors"][:6]) + (" et al." if len(rec["authors"]) > 6 else "")
    where = " · ".join(x for x in [rec.get("venue"), rec.get("publicationDate") or str(rec.get("year") or "")] if x)
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📄 {rec['title']}"[:150], "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": " · ".join(x for x in [authors, where, " · ".join(links)] if x)[:2900]}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*TL;DR* {d['tldr']}"[:2900]}},
    ]
    if d["why"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Why this group* {d['why']}"[:2900]}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                   "🧵 Method digest in thread  ·  👍 more like this · 👎 less · ➕ add to voting sheet"}]})
    ts = slack.post(f"Paper of the day: {rec['title']}", blocks=blocks)

    body = d["digest"]
    if not full_text:
        body = "_⚠️ Couldn't get the full text, so this is based on the abstract only._\n\n" + body
    parts = P.chunks(body)
    for i in range(0, len(parts), 20):
        slack.post("Method digest", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": c}}
                                            for c in parts[i:i + 20]], thread_ts=ts)
    return ts


# ------------------------------------------------------------------ main

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "sync"
    s = load_state()
    slack = Slack()
    sheets = Sheets(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], SPREADSHEET_ID, VOTING_GID, READ_GID)
    try:
        seeds(s)
        sync_messages(slack, sheets, s)
        sync_reactions(slack, sheets, s)
        save_state(s)
        due = mode == "daily" and s["last_daily"] != today().isoformat()
        if due or s["force_daily"] or "--force" in sys.argv:
            if daily(slack, sheets, s):
                s["force_daily"] = False
                if due:
                    s["last_daily"] = today().isoformat()
            else:
                print("[daily] Semantic Scholar unavailable; will try again next run", file=sys.stderr)
                s["force_daily"] = True
            save_state(s)
        s["collapse_sig"] = sheets.collapse_old(COLLAPSE_AFTER_DAYS, today(), COLLAPSE_UNDATED, s["collapse_sig"])
    finally:
        save_state(s)


if __name__ == "__main__":
    main()
