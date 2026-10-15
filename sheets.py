"""Google Sheets access for the 'Paper Voting' and 'Already read' tabs.

Columns are found by their header names, so reordering columns or adding
members (new checkbox columns after 'Total') keeps working.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import gspread
from gspread.utils import DateTimeOption, ValueInputOption, ValueRenderOption, rowcol_to_a1

from papers import ARXIV_BARE, norm_title

EPOCH = dt.date(1899, 12, 30)  # Google Sheets date serial origin
HYPERLINK = re.compile(r'^=HYPERLINK\(\s*"((?:[^"]|"")*)"\s*[,;]\s*"((?:[^"]|"")*)"\s*\)\s*$', re.I)


def parse_date(v) -> dt.date | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return EPOCH + dt.timedelta(days=int(v))
    s = str(v).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%b %d, %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_paper_cell(cell) -> tuple[str, str | None]:
    """'=HYPERLINK(url, label)' or plain text -> (label, url)."""
    s = str(cell or "").strip()
    m = HYPERLINK.match(s)
    if m:
        return m.group(2).replace('""', '"'), m.group(1).replace('""', '"')
    if re.match(r"https?://", s):
        return s, s
    return s, None


def q(s: str) -> str:
    return (s or "").replace('"', '""')


def paper_formula(rec: dict) -> str:
    label = f"[{rec['arxiv']}] {rec['title']}" if rec.get("arxiv") else rec["title"]
    return f'=HYPERLINK("{q(rec["url"])}", "{q(label)}")' if rec.get("url") else label


def col_letter(c: int) -> str:
    return re.sub(r"\d", "", rowcol_to_a1(1, c))


class Sheets:
    def __init__(self, service_account_json: str, spreadsheet_id: str, voting_gid: int, read_gid: int):
        gc = gspread.service_account_from_dict(json.loads(service_account_json))
        self.sh = gc.open_by_key(spreadsheet_id)
        self.voting = self.sh.get_worksheet_by_id(voting_gid)
        self.read = self.sh.get_worksheet_by_id(read_gid)

    # ---------------------------------------------------------------- reading

    @staticmethod
    def _values(ws) -> list[list]:
        return ws.get_all_values(value_render_option=ValueRenderOption.formula,
                                 date_time_render_option=DateTimeOption.serial_number)

    @staticmethod
    def _header(values: list[list], wanted: dict[str, tuple[str, ...]], required: tuple[str, ...], tab: str):
        for h, row in enumerate(values[:6]):
            names = [str(c).strip().lower() for c in row]
            cols = {}
            for key, prefixes in wanted.items():
                for i, n in enumerate(names):
                    if n and any(n.startswith(p) for p in prefixes):
                        cols[key] = i
                        break
            if all(k in cols for k in required):
                return h, cols, names
        raise RuntimeError(f"Couldn't find the header row in the '{tab}' tab "
                           f"(looking for columns: {', '.join(required)}).")

    def _voting_layout(self, values):
        h, cols, names = self._header(values, {
            "paper": ("paper",), "desc": ("description",), "proposer": ("who proposed", "proposed"),
            "presenter": ("presenter",), "date": ("date added", "date"), "total": ("total",),
        }, ("paper", "desc", "proposer", "date"), self.voting.title)
        start = cols.get("total", max(cols.values())) + 1
        voters = [i for i in range(start, len(names)) if names[i]]
        return h, cols, voters

    def voting_papers(self) -> list[dict]:
        values = self._values(self.voting)
        h, cols, _ = self._voting_layout(values)
        out = []
        for r, row in enumerate(values[h + 1:], start=h + 2):
            get = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else ""
            label, url = parse_paper_cell(get("paper"))
            if not label:
                continue
            m = ARXIV_BARE.search(label) or (ARXIV_BARE.search(url) if url else None)
            out.append({"row": r, "raw": get("paper"), "label": label, "url": url,
                        "title": re.sub(r"^\s*\[[^\]]+\]\s*", "", label),
                        "arxiv": m.group(1) if m else None,
                        "description": str(get("desc") or ""), "proposer": str(get("proposer") or ""),
                        "presenter": str(get("presenter") or ""), "date": parse_date(get("date"))})
        return out

    def read_papers(self) -> list[dict]:
        values = self._values(self.read)
        h, cols, _ = self._read_layout(values)
        out = []
        for row in values[h + 1:]:
            get = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else ""
            label, url = parse_paper_cell(get("title"))
            link = str(get("link") or "") or url or ""
            if not (label or link):
                continue
            m = ARXIV_BARE.search(label) or ARXIV_BARE.search(link)
            out.append({"title": re.sub(r"^\s*\[[^\]]+\]\s*", "", label), "url": link,
                        "arxiv": m.group(1) if m else None, "date": parse_date(get("date"))})
        return out

    def _read_layout(self, values):
        return self._header(values, {
            "title": ("paper title", "paper", "title"), "link": ("link", "url"),
            "desc": ("description",), "presenter": ("who presented", "presenter", "presented"),
            "date": ("date read", "date"),
        }, ("title", "date"), self.read.title)

    def find_existing(self, rec: dict) -> tuple[str, dict] | None:
        """('voting'|'read', row) if this paper is already in either tab."""
        t = norm_title(rec["title"])
        for tab, rows in (("voting", self.voting_papers()), ("read", self.read_papers())):
            for p in rows:
                if (rec.get("arxiv") and p.get("arxiv") == rec["arxiv"]) or (t and norm_title(p["title"]) == t) \
                        or (rec.get("url") and p.get("url") and p["url"].rstrip("/") == rec["url"].rstrip("/")):
                    return tab, p
        return None

    # ---------------------------------------------------------------- writing

    def add_to_voting(self, rec: dict, description: str, proposer: str, date_str: str) -> int:
        ws = self.voting
        values = self._values(ws)
        h, cols, voters = self._voting_layout(values)
        last = h + 1  # 1-based header row
        for r, row in enumerate(values, start=1):
            if r > h + 1 and cols["paper"] < len(row) and str(row[cols["paper"]]).strip():
                last = r
        target = last + 1
        ncols = max(len(values[h]), ws.col_count)
        if target > ws.row_count:
            ws.add_rows(target - ws.row_count + 5)

        requests_ = []
        if last > h + 1:  # copy the previous paper row: keeps checkboxes, Total formula, formatting
            requests_.append({"copyPaste": {
                "source": {"sheetId": ws.id, "startRowIndex": last - 1, "endRowIndex": last,
                           "startColumnIndex": 0, "endColumnIndex": ncols},
                "destination": {"sheetId": ws.id, "startRowIndex": target - 1, "endRowIndex": target,
                                "startColumnIndex": 0, "endColumnIndex": ncols},
                "pasteType": "PASTE_NORMAL"}})
        elif voters:
            requests_.append({"setDataValidation": {
                "range": {"sheetId": ws.id, "startRowIndex": target - 1, "endRowIndex": target,
                          "startColumnIndex": voters[0], "endColumnIndex": voters[-1] + 1},
                "rule": {"condition": {"type": "BOOLEAN"}}}})
        if requests_:
            self.sh.batch_update({"requests": requests_})

        updates = [
            (cols["paper"], paper_formula(rec)),
            (cols["desc"], description),
            (cols["proposer"], proposer),
            (cols["date"], date_str),
        ]
        if "presenter" in cols:
            updates.append((cols["presenter"], ""))
        updates += [(v, False) for v in voters]  # empty voting slots
        if last <= h + 1 and "total" in cols and voters:
            updates.append((cols["total"],
                            f"=COUNTIF({col_letter(voters[0] + 1)}{target}:{col_letter(voters[-1] + 1)}{target}, TRUE)"))
        ws.batch_update([{"range": rowcol_to_a1(target, c + 1), "values": [[v]]} for c, v in updates],
                        value_input_option=ValueInputOption.user_entered)
        return target

    def move_to_read(self, paper: dict, presenter: str, date_str: str) -> None:
        # 1) add to the top of 'Already read' (newest first, like the existing rows)
        rvalues = self._values(self.read)
        h, cols, _ = self._read_layout(rvalues)
        self.sh.batch_update({"requests": [{"insertDimension": {
            "range": {"sheetId": self.read.id, "dimension": "ROWS", "startIndex": h + 1, "endIndex": h + 2},
            "inheritFromBefore": False}}]})
        row = h + 2
        link = paper.get("url") or (f"https://arxiv.org/abs/{paper['arxiv']}" if paper.get("arxiv") else "")
        updates = [(cols["title"], paper["title"]), (cols["date"], date_str)]
        if "link" in cols:
            updates.append((cols["link"], link))
        if "desc" in cols:
            updates.append((cols["desc"], paper.get("description", "")))
        if "presenter" in cols:
            updates.append((cols["presenter"], presenter or paper.get("presenter", "")))
        self.read.batch_update([{"range": rowcol_to_a1(row, c + 1), "values": [[v]]} for c, v in updates],
                               value_input_option=ValueInputOption.user_entered)

        # 2) delete from 'Paper Voting' (re-find the row in case the sheet changed meanwhile)
        current = next((p for p in self.voting_papers() if p["raw"] == paper["raw"]), None)
        if current:
            self.sh.batch_update({"requests": [{"deleteDimension": {"range": {
                "sheetId": self.voting.id, "dimension": "ROWS",
                "startIndex": current["row"] - 1, "endIndex": current["row"]}}}]})

    # -------------------------------------------------------------- collapsing

    def collapse_old(self, days: int, today: dt.date, include_undated: bool, last_signature: str | None) -> str:
        """Group + collapse rows whose Date Added is older than `days`. Returns a signature;
        does nothing if the set of old rows hasn't changed since last time (so people can
        expand groups without the bot re-collapsing them every run)."""
        rows = self.voting_papers()
        old = sorted(p["row"] for p in rows
                     if (p["date"] and (today - p["date"]).days > days) or (include_undated and not p["date"]))
        runs: list[list[int]] = []
        for r in old:
            if runs and r == runs[-1][1] + 1:
                runs[-1][1] = r
            else:
                runs.append([r, r])
        signature = json.dumps(runs)
        if signature == last_signature:
            return signature

        meta = self.sh.fetch_sheet_metadata(params={"fields": "sheets(properties(sheetId),rowGroups)"})
        groups = next((s.get("rowGroups", []) for s in meta["sheets"]
                       if s["properties"]["sheetId"] == self.voting.id), [])
        reqs = [{"deleteDimensionGroup": {"range": g["range"]}}
                for g in sorted(groups, key=lambda g: -g.get("depth", 1))]
        for a, b in runs:
            rng = {"sheetId": self.voting.id, "dimension": "ROWS", "startIndex": a - 1, "endIndex": b}
            reqs.append({"addDimensionGroup": {"range": rng}})
            reqs.append({"updateDimensionGroup": {"dimensionGroup": {"range": rng, "depth": 1, "collapsed": True},
                                                  "fields": "collapsed"}})
        if reqs:
            self.sh.batch_update({"requests": reqs})
        return signature
