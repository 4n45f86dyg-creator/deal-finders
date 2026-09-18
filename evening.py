#!/usr/bin/env python3
"""20:00 evening report: every employee's numbers for the day + what's worth a look. One Telegram message.
  python3 evening.py            sends once per day after 20:00 Riga
  python3 evening.py --force    sends now as a test (doesn't count as today's report)
  python3 evening.py --print    prints only
"""
import sys
from commands import HOLD_DAYS, item_name
from common import Bot, State, age_hours, days_since, now, safe_say, secret


def build(t):
    today = t.strftime("%Y-%m-%d")
    flip_meta, flip_deals = State("flip").load("meta.json", {}), State("flip").load("deals.json", [])
    free_meta, free_deals = State("free").load("meta.json", {}), State("free").load("deals.json", [])
    jobs_meta, job_leads = State("jobs").load("meta.json", {}), State("jobs").load("leads.json", [])
    f, g = flip_meta.get("days", {}).get(today, {}), free_meta.get("days", {}).get(today, {})
    j = jobs_meta.get("days", {}).get(today, {})
    recent = [d for d in flip_deals if d.get("state") == "worth_a_look" and age_hours(d["at"], t) <= 24]
    looks = sorted(recent, key=lambda d: -d.get("score", 0))[:5]
    free_list = [d for d in free_deals if d.get("state") == "listed" and age_hours(d["at"], t) <= 24][:5]
    held = [d for d in flip_deals if d.get("state") == "bought"]
    sold = [d for d in flip_deals if d.get("state") == "sold"] + [d for d in free_deals if d.get("state") == "sold"]
    profit = sum(d.get("sold", 0) - d.get("bought", 0) for d in sold)
    lines = [f"🌙 Evening report · {t:%d.%m}",
             f"Flip finder: {f.get('runs', 0)} runs · {f.get('listings', 0)} new listings · {f.get('price_checks', 0)} price checks · "
             f"easy money {f.get('top', 0)} · worth a look {f.get('worth_a_look', 0)} · below bar {f.get('below_bar', 0)} · errors {f.get('errors', 0)}",
             f"Free finder: {g.get('runs', 0)} runs · {g.get('giveaways', 0)} giveaways · worth picking up {g.get('worth_it', 0)} "
             f"(instant {g.get('instant', 0)}) · errors {g.get('errors', 0)}",
             f"Job watcher: {j.get('new', 0)} new leads today · "
             f"{sum(1 for x in job_leads if x.get('state') == 'new')} businesses still to call",
             f"Money: stock {len(held)} items €{sum(d.get('bought', 0) for d in held):.0f} · sold {len(sold)} · profit €{profit:.0f}"]
    slow = [(d, days_since(d.get("bought_at", ""), t)) for d in held]
    slow = sorted([(d, n) for d, n in slow if n > HOLD_DAYS], key=lambda x: -x[1])
    if slow:
        lines.append(f"\n⏳ Held over {HOLD_DAYS} days — drop the price or sell at cost:")
        for d, n in slow[:5]:
            lines.append(f"#{d['no']} {n} days · paid €{d.get('bought', 0):.0f} · {item_name(d)}")
    last = [m.get("last_run", "") for m in (flip_meta, free_meta)]
    stale = [n for n, l in zip(("Flip", "Free"), last) if not l or age_hours(l, t) > 1]
    if stale:
        lines.append(f"🚧 {' and '.join(stale)} finder hasn't run for over an hour — HQ checks GitHub.")
    if looks or free_list:
        lines.append("\nWorth a look (no alert was sent):")
        for d in looks:
            lines.append(f"#{d['no']} {d['score']}/100 · buy €{d['price']:.0f} → relist ~€{d.get('relist', d['going']):.0f} · "
                         f"{d['loc']} · {d['title'][:40]}\n{d['link']}")
        for d in free_list:
            lines.append(f"F{d['no']} free · {d['loc']} · {d['title'][:50]}\n{d['link']}")
        lines.append("\nTell HQ which ones go into tomorrow's brief.")
    else:
        lines.append("\nNothing else worth your attention today.")
    return "\n".join(lines)


def main():
    t = now()
    today = t.strftime("%Y-%m-%d")
    rep = State("report")
    meta = rep.load("meta.json", {})
    text = build(t)
    if "--print" in sys.argv:
        print(text)
        return
    force = "--force" in sys.argv
    if not force and (t.hour < 20 or meta.get("sent_date") == today):
        print("not 20:00 yet" if t.hour < 20 else "already sent today")
        return
    b = Bot(secret("FLIP_TG_TOKEN", "telegram-flip-bot"), secret("FLIP_TG_CHAT", "telegram-flip-chat"), replies=False)
    if safe_say(b, ("🧪 TEST — " if force else "") + text, rep):
        if not force:
            meta["sent_date"] = today
            rep.save("meta.json", meta)
        print("sent")
    else:
        print("telegram failed")


if __name__ == "__main__":
    main()
