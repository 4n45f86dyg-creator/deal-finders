#!/usr/bin/env python3
"""Job watcher (HQ spec 005): businesses hiring someone to answer calls and take bookings -> Telegram.
  python3 jobs.py scan [--dry]   one run (GitHub runs it every weekday morning; --dry prints, sends nothing)
  python3 jobs.py status         how many leads are still to call
Sources: ss.lv work section (small businesses) + cv.lv public search API (bigger ones).
Replies go through the flip bot (commands.py): leads · lead done 2 · lead skip 2
"""
import json, re, sys, time
from common import Bot, State, bump_day, clean, fetch, now, report_day, roadblock, safe_say, secret

NAME = "Job watcher"
SS_CATS = ["administrator", "secretary", "dispatcher", "operator"]
MAX_ADS, MAX_LISTED = 45, 5          # ss.lv ad pages opened per run · leads listed in the one Telegram message
CV_API = "https://www.cv.lv/api/v1/vacancy-search-service/search"
# work the front-desk kit covers, with its weight: ss.lv ads need 2 points, cv.lv 3 plus a matching job title
PIECES = {
    "phone": (2, r"zvan|telefon|звон|телефон|call"),
    "booking": (2, r"pierakst|reģistr|rezerv|kalendār|vizīt|запис|регистрат|бронир|график"),
    "messages": (1, r"e-past|epast|ziņ[au]|sociāl|instagram|whatsapp|facebook|письм|сообщен|соцсет|почт"),
    "dispatch": (2, r"dispečer|brigād|meistar|pasūtījum|uzdevum|izsaukum|диспетчер|заявк|бригад|мастер|заказ"),
    "data": (1, r"datu ievad|dokument|excel|rēķin|ввод данных|документ|счет"),
}
SKIP_RX = re.compile(r"sistēmu administrator|datortīkl|it adminis|system admin|network admin|iekrāvēj|frēzētāj|ražošanas iekārt|"
                     r"traktor|celtņ|autovadītāj|šoferis|krāvēj|noliktav|pavār|iekārtas operator|skaldīšan|zāģ|виловоч|погрузчик|"
                     r"водител|склад|"
                     # titles that pass on the word "administrators"/"asistents" but are not front-desk work
                     r"\bit\s+(infrastruktūr|sistēm|tīkl)|sistēmu un tīkl|būvinženier|būvdarbu vadītāj|būvlaukum|"
                     r"personāla atlas|grāmatved|grāmatvež|analītiķ", re.I)
RECRUITER_RX = re.compile(r"cv-online|workingday|visidarbi|recruit|personāla atlas|manpower|biuro|wow personnel", re.I)
TITLE_RX = re.compile(r"administrat|reģistrat|recepc|klientu apkalpo|zvanu centr|dispečer|koordinator|asistent|"
                      r"регистрат|администрат|диспетчер|оператор", re.I)
# Boss 18.09: a chain already staffs the phone and buying goes through IT/procurement — no cash now, skip it
CHAIN_RX = re.compile(r"veselības centru apvienīb|\bm\s?f\s?d\b|zvanu centr|call cent", re.I)
PAY_RX = re.compile(r"(?<![\d,.])(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:EUR|eur|€|eiro)\s*(/\s*(?:h|st|stund|mēn)\w*)?")


def bot():
    return Bot(secret("FLIP_TG_TOKEN", "telegram-flip-bot"), secret("FLIP_TG_CHAT", "telegram-flip-chat"), replies=False)


def pieces_for(text):
    hits, score = [], 0
    for name, (weight, rx) in PIECES.items():
        if re.search(rx, text, re.I):
            hits.append(name)
            score += weight
    return hits, score


def is_chain(text, skipped):
    """Chains and anything Boss has crossed off as not worth calling."""
    low = (text or "").lower()
    return bool(CHAIN_RX.search(low)) or any(s and s in low for s in skipped)


def pay_of(text):
    pays = PAY_RX.findall(text or "")
    return ", ".join(f"€{a}{(' ' + b.replace(' ', '')) if b else ''}" for a, b in pays[:2])


def ss_rows(st):
    """Ad rows from the four front-desk categories that already score on the list page. Returns (rows, pages reached)."""
    rows, reached = [], 0
    for cat in SS_CATS:
        for page in ("", "page2.html"):
            try:
                s = fetch(f"https://www.ss.lv/lv/work/are-required/{cat}/{page}", timeout=20)
                reached += 1
            except Exception as e:
                st.log(f"ss.lv error {cat} {page or 'page1'}: {e}")
                continue
            for _, row in re.findall(r'<tr id="tr_(\d+)"(.*?)</tr>', s, re.S):
                link = re.search(r'href="(/msg/[^"]+)"', row)
                text = clean(row)
                if not link or SKIP_RX.search(text):
                    continue
                if pieces_for(text)[1] >= 2:
                    rows.append({"src": "ss.lv", "link": "https://www.ss.lv" + link.group(1), "row": text})
            time.sleep(1)
    seen, out = set(), []
    for r in rows:
        if r["link"] not in seen:
            seen.add(r["link"])
            out.append(r)
    return out, reached


def ss_detail(row):
    """Read the ad's own page: full description, where it is, pay named in the text."""
    page = fetch(row["link"], timeout=20)
    d = re.search(r'<div id="msg_div_msg"[^>]*>(.*?)<table', page, re.S)
    desc = clean(d.group(1)) if d else row["row"]
    loc = re.search(r"(?:Pilsēta, rajons|Pilsēta|Rajons|Vieta):\s*</td>\s*<td[^>]*>(.*?)</td>", page, re.S)
    pieces, score = pieces_for(desc)
    return {"desc": desc, "where": clean(loc.group(1)) if loc else "?", "pay": pay_of(desc), "pieces": pieces, "score": score}


def towns(st):
    """cv.lv gives a town id only. Its vacancy pages carry the whole id->name table, so read it once and keep it."""
    cached = st.load("towns.json", {})
    if cached:
        return cached
    try:
        s = fetch("https://www.cv.lv/lv/vacancy/1", timeout=25)
        block = re.search(r'"towns":\[(.*?)\}\]', s, re.S)
        cached = {str(i): n for i, n in re.findall(r'"id":(\d+),[^{}]*?"name":"([^"]+)"', block.group(1))} if block else {}
    except Exception as e:
        st.log(f"cv.lv towns error: {e}")
        return {}
    if cached:
        st.save("towns.json", cached)
    return cached


def cv_vacancies(st):
    """Every open vacancy from the public search API, 100 at a time."""
    out, off = [], 0
    while True:
        d = json.loads(fetch(f"{CV_API}?limit=100&offset={off}", timeout=30))
        v = d.get("vacancies") or []
        out += v
        off += len(v)
        if not v or off >= d.get("total", 0) or off > 3000:
            break
        time.sleep(0.6)
    return out


def cv_rows(st):
    """cv.lv vacancies that are front-desk work at a company worth calling."""
    try:
        vacancies = cv_vacancies(st)
    except Exception as e:
        st.log(f"cv.lv error: {e}")
        return [], 0
    town = towns(st)
    out = []
    for v in vacancies:
        title, employer = v.get("positionTitle") or "", v.get("employerName") or "?"
        body = clean(v.get("positionContent") or "")[:1500]
        if SKIP_RX.search(title) or RECRUITER_RX.search(employer) or not TITLE_RX.search(title):
            continue
        pieces, score = pieces_for(f"{title} {body}")
        if score < 3:
            continue
        pay = ""
        if v.get("salaryFrom"):
            pay = f"€{v['salaryFrom']:.0f}" + (f"-{v['salaryTo']:.0f}" if v.get("salaryTo") else "")
            pay += "/h" if v.get("hourlySalary") else ""
        out.append({"src": "cv.lv", "link": f"https://www.cv.lv/lv/vacancy/{v['id']}", "name": f"{employer} — {title}",
                    "where": town.get(str(v.get("townId")), "?"), "pay": pay, "desc": body, "pieces": pieces, "score": score,
                    "row": f"{employer} {title} {body[:400]}"})
    return sorted(out, key=lambda x: -x["score"]), 1


def alert_text(fresh, left, first=False):
    """One message a morning: what turned up, the best five, nothing to scroll."""
    head = "First list" if first else "New this morning"      # the seeding run finds the whole backlog, not one morning's ads
    lines = [f"📇 {head}: {len(fresh)} business{'es' if len(fresh) != 1 else ''} hiring someone to answer calls"]
    for x in fresh[:MAX_LISTED]:
        bits = " · ".join(b for b in (x["name"], x["where"], x["pay"]) if b and b != "?")
        lines.append(f"#{x['no']} {bits}\n{x['link']}")
    if len(fresh) > MAX_LISTED:
        lines.append(f"…and {len(fresh) - MAX_LISTED} more.")
    lines.append(f"{left} still to call. Reply: leads · lead done 2 · lead skip 2")
    return "\n".join(lines)


def scan(dry=False):
    st = State("jobs")
    meta, leads = st.load("meta.json", {}), st.load("leads.json", [])
    seen = dict.fromkeys(st.load("seen.json", []))
    skipped = [s.lower() for s in st.load("skipped.json", [])]
    t = now()
    first = not seen and not leads
    day = report_day(t)
    known = {x.get("link") for x in leads}
    rows, reached = ss_rows(st)
    cv, cv_ok = cv_rows(st)
    kept, chains, opened = [], 0, 0
    for row in rows:
        if row["link"] in seen or row["link"] in known:
            continue
        if opened >= MAX_ADS:
            break
        seen[row["link"]] = None
        opened += 1
        try:
            row.update(ss_detail(row))
        except Exception as e:
            st.log(f"ad error {row['link']}: {e}")
            continue
        time.sleep(1)
        if row["score"] < 2:
            continue
        if is_chain(f"{row['row']} {row['desc']}", skipped):
            chains += 1
            continue
        row["name"] = re.sub(r"^[>\s]+", "", row["row"])[:70]
        kept.append(row)
    for row in cv:
        if row["link"] in seen or row["link"] in known:
            continue
        seen[row["link"]] = None
        if is_chain(f"{row['name']} {row['desc']}", skipped):
            chains += 1
            continue
        kept.append(row)
    no = max([x.get("no", 0) for x in leads] or [0])
    fresh = []
    for row in sorted(kept, key=lambda x: -x["score"]):
        no += 1
        fresh.append({"no": no, "name": row["name"][:70], "where": row["where"], "pay": row["pay"], "link": row["link"],
                      "src": row["src"], "state": "new", "at": t.strftime("%Y-%m-%d")})
    left = sum(1 for x in leads if x.get("state") == "new") + len(fresh)
    if dry:
        print(f"ss.lv rows {len(rows)} (pages reached {reached}) · cv.lv matches {len(cv)} · ad pages opened {opened} · "
              f"chains dropped {chains} · new leads {len(fresh)}")
        for x in fresh:
            print(f"  #{x['no']} [{x['src']}] {x['name']} · {x['where']} · {x['pay'] or '—'}\n     {x['link']}")
        print("\n--- would send ---\n" + (alert_text(fresh, left, first) if fresh else "(nothing new — no message)"))
        return
    b = bot()
    sent = 0
    if fresh and safe_say(b, alert_text(fresh, left, first), st, silent=True):
        sent = 1
    leads += fresh
    roadblock(meta, reached > 0 or cv_ok > 0, b, NAME, st)
    bump_day(meta, day, runs=1, ads=opened, new=len(fresh), chains=chains, sent=sent)
    meta["last_run"] = t.strftime("%Y-%m-%d %H:%M")
    st.save("seen.json", list(seen)[-4000:])
    st.save("leads.json", leads[-400:])
    st.save("meta.json", meta)
    line = f"ss.lv rows {len(rows)}, ads opened {opened}, cv.lv {len(cv)}, chains {chains}, new {len(fresh)}, sent {sent}"
    st.log(line)
    print(line)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "scan":
        scan(dry="--dry" in sys.argv)
    elif cmd == "status":
        leads = State("jobs").load("leads.json", [])
        print(f"{sum(1 for x in leads if x.get('state') == 'new')} still to call of {len(leads)} found")
    else:
        print(__doc__)
