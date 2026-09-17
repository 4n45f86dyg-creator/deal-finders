#!/usr/bin/env python3
"""Free finder: giveaways near Riga worth picking up and reselling -> Telegram.
Sources: ss.lv listings that say free (atdodu, par velti, отдам…) and new items on atdodmantas.lv.
  python3 free.py scan [--dry]
Replies (once it has its own bot): picked 3 · sold 3 40 · skip 3 · stock
"""
import re, sys, time
from common import (BUY_RX, FREE_RX, JUNK_RX, Bot, State, age_hours, clean, fetch, is_near, listing_location, now,
                    parse_feed, roadblock, safe_say, secret)

NAME = "Free finder"
FEEDS = ["home-stuff", "for-children", "electronics", "construction", "entertainment"]
SITE = "https://www.atdodmantas.lv/"
RESELL_RX = re.compile(r"velosip|ķiver|ratiņ|autosēd|krēsl|galds|galdiņ|skap|kumod|dīvān|televiz|\btv\b|monitor|dator|"
                       r"telefon|planšet|kafij|putekļsūc|mikroviļņ|blender|kombain|instrument|urbj|slīp|zāģ|pļāvēj|trimer|"
                       r"slēpes|slidas|snovbord|trenažier|lampa|spogul|gult|šūpuļ|playstation|xbox|nintendo|skaļrun|austiņ|"
                       r"kamera|fotoaparāt|printer|elektr|велосипед|стол|стул|шкаф|комод|диван|телевизор|монитор|компьютер|"
                       r"ноутбук|кофе|пылесос|микроволн|инструмент|дрель|пила|коляск|кроват|лампа|зеркал", re.I)
SKIP_RX = re.compile(r"grāmat|augi|stād|dzīvžog|drēb|apģērb|apav|pārtik|ēdien|kaķ|suņ|kucēn|paklāj|trauk|zeme|malk|gruž|"
                     r"книг|растен|одежд|обув|котён|щен|земл|дров|мусор", re.I)
MAX_ALERTS, DAILY_CAP, MAX_AGE_MIN = 5, 20, 120
HELP = "Reply: picked 3 · sold 3 40 · skip 3 · stock"


def bot():
    token = secret("FREE_TG_TOKEN", "telegram-free-bot")
    if token:
        return Bot(token, secret("FREE_TG_CHAT", "telegram-free-chat"))
    # until it has its own bot: alerts go to Flip finder's chat, replies stay with Flip finder
    return Bot(secret("FLIP_TG_TOKEN", "telegram-flip-bot"), secret("FLIP_TG_CHAT", "telegram-flip-chat"), replies=False)


def worth_it(text):
    return bool(RESELL_RX.search(text)) and not SKIP_RX.search(text) and not JUNK_RX.search(text) and not BUY_RX.search(text)


def site_items():
    s = fetch(SITE)
    return {slug: clean(title) for title, slug in re.findall(r"text=([^&\"]+)&url=https://www\.atdodmantas\.lv/thing/([a-z0-9-]+)", s)}


def site_location(slug, title):
    t = clean(re.sub(r"<script.*?</script>|<style.*?</style>", " ", fetch(SITE + "thing/" + slug), flags=re.S))
    if "Jau atdots" in t:
        return "already given away"
    m = re.search(r"Apmeklējuma skaits\s*:\s*\d+\s*(.*?)\s*Iegūt kontaktus", t)
    if not m:
        return "?"
    seg = m.group(1)
    return seg[len(title):].strip(" ,") if seg.startswith(title) else seg


def alert_text(d):
    night = "🌙 posted overnight — may be gone\n" if d.get("overnight") else ""
    return f"F{d['no']} 🆓 FREE · {d['loc']}\n{night}{d['title']}\n{d['link']}"


def status_text(deals):
    picked = [d for d in deals if d.get("state") == "picked"]
    sold = [d for d in deals if d.get("state") == "sold"]
    return (f"Picked up, not sold: {len(picked)} · sold {len(sold)} · profit €{sum(d['sold'] for d in sold):.0f}\n"
            f"Giveaways sent so far: {sum(1 for d in deals if d.get('sent'))}")


def handle_replies(b, meta, deals):
    offset, texts = b.incoming(meta.get("tg_offset", 0))
    meta["tg_offset"] = offset
    for text in texts:
        w = text.lower().replace("#", "").split()
        if len(w) > 1:
            w[1] = w[1].lstrip("f")
        cmd = w[0] if w else ""
        if cmd in ("stock", "status"):
            b.say(status_text(deals))
            continue
        if cmd in ("help", "/start", "hi"):
            b.say(f"Giveaways worth reselling arrive here by themselves.\n{HELP}")
            continue
        if cmd not in ("picked", "sold", "skip") or len(w) < 2 or not w[1].isdigit():
            b.say(f"Didn't get that. {HELP}")
            continue
        d = next((x for x in deals if x["no"] == int(w[1])), None)
        if not d:
            b.say(f"No item F{w[1]}.")
            continue
        if cmd in ("picked", "skip"):
            d["state"] = "picked" if cmd == "picked" else "skipped"
            b.say(f"F{d['no']} {d['state']}.")
            continue
        if len(w) < 3 or not re.fullmatch(r"€?\d+([.,]\d+)?", w[2]):
            b.say(f"Add the price: sold {d['no']} 40")
            continue
        d.update(state="sold", sold=float(w[2].lstrip("€").replace(",", ".")), sold_at=f"{now():%Y-%m-%d}")
        b.say(f"F{d['no']} sold €{d['sold']:.0f}.\n{status_text(deals)}")


def scan(dry=False):
    st = State("free")
    meta, deals = st.load("meta.json", {}), st.load("deals.json", [])
    seen = dict.fromkeys(st.load("seen.json", []))
    t = now()
    today, quiet = t.strftime("%Y-%m-%d"), (t.hour >= 23 or t.hour < 7)
    first_feed = not any(not k.startswith("am:") for k in seen) and not dry
    first_site = not any(k.startswith("am:") for k in seen) and not dry
    reached = giveaways = found = errors = 0
    new = []
    for feed in FEEDS:
        try:
            items = parse_feed(feed)
            reached += 1
        except Exception as e:
            st.log(f"feed error {feed}: {e}")
            errors += 1
            continue
        for it in items:
            if it["id"] in seen and not dry:
                continue
            seen[it["id"]] = None
            if first_feed or it["age_min"] > MAX_AGE_MIN or not FREE_RX.search(it["title"]):
                continue
            if it["price"] not in (None, 0.0) and it["price"] > 1:
                continue
            giveaways += 1
            if not worth_it(it["title"]):
                continue
            loc = listing_location(it["link"])
            if dry:
                print(f"  ss.lv giveaway: {it['title'][:60]} | {loc} | near {is_near(loc)}")
            if is_near(loc):
                new.append({"title": it["title"][:90], "link": it["link"], "loc": loc, "src": "ss.lv"})
    try:
        for slug, title in site_items().items():
            key = "am:" + slug
            if key in seen and not dry:
                continue
            seen[key] = None
            if first_site:
                continue
            giveaways += 1
            if not worth_it(title):
                continue
            time.sleep(1)
            loc = site_location(slug, title)
            if dry:
                print(f"  atdodmantas: {title[:60]} | {loc} | near {is_near(loc)}")
            if is_near(loc):
                new.append({"title": title[:90], "link": SITE + "thing/" + slug, "loc": loc, "src": "atdodmantas"})
    except Exception as e:
        st.log(f"site error: {e}")
        errors += 1
    if dry:
        print(f"giveaways seen {giveaways}, worth it near Riga {len(new)}, errors {errors}")
        return
    for n in new:
        found += 1
        deals.append({"no": (deals[-1]["no"] + 1) if deals else 1, "at": t.strftime("%Y-%m-%d %H:%M"), "state": "new",
                      "sent": False, "overnight": quiet, **n})
    b, sent = bot(), 0
    if first_feed or first_site:
        meta["summary_date"] = today
        where = "here" if b.replies else "in Flip finder's chat until it gets its own bot"
        safe_say(b, f"✅ Free finder now runs on GitHub, 24/7. Giveaways near Riga worth reselling arrive {where}.", st)
    if not quiet:
        sent_today = sum(1 for d in deals if d.get("sent_at", "").startswith(today))
        for d in deals:
            if d["sent"] or d["state"] != "new":
                continue
            if age_hours(d["at"], t) > 12:
                d["state"] = "expired"
                continue
            if sent >= MAX_ALERTS or sent_today + sent >= DAILY_CAP:
                break
            if safe_say(b, alert_text(d), st):
                d["sent"], d["sent_at"] = True, t.strftime("%Y-%m-%d %H:%M")
                sent += 1
    if b.replies:
        try:
            handle_replies(b, meta, deals)
        except Exception as e:
            st.log(f"telegram error: {e}")
            errors += 1
    stats = meta.setdefault("stats", {})
    for k, v in (("runs", 1), ("giveaways", giveaways), ("worth_it", found), ("errors", errors)):
        stats[k] = stats.get(k, 0) + v
    roadblock(meta, reached > 0, b, NAME, st)
    summary = (f"☀️ Free finder — since the last summary\nRuns {stats.get('runs', 0)} · giveaways seen {stats.get('giveaways', 0)} · "
               f"worth picking up near Riga {stats.get('worth_it', 0)} · errors {stats.get('errors', 0)}\n{status_text(deals)}")
    if t.hour >= 7 and meta.get("summary_date") != today and safe_say(b, summary, st):
        meta["summary_date"] = today
        meta["days"] = (meta.get("days", []) + [{"date": today, **stats}])[-60:]
        meta["stats"] = {}
    meta["last_run"] = t.strftime("%Y-%m-%d %H:%M")
    st.save("seen.json", list(seen)[-6000:])
    st.save("deals.json", deals[-500:])
    st.save("meta.json", meta)
    line = f"{'seeded ' if first_feed or first_site else ''}giveaways {giveaways}, worth it {found}, sent {sent}, errors {errors}"
    st.log(line)
    print(line)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "scan":
        scan(dry="--dry" in sys.argv)
    else:
        print(__doc__)
