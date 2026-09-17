#!/usr/bin/env python3
"""Flip finder (HQ spec 004): new used items on ss.lv priced well under similar listings -> Telegram.
  python3 flip.py scan [--dry]   one run (GitHub runs it every 5 min; --dry prints, sends nothing)
  python3 flip.py status         stock and profit
Replies in Telegram: bought 3 40 · sold 3 90 · skip 3 · stock
"""
import json, re, statistics, sys, time, urllib.parse
from common import (BUY_RX, CRACK_RX, JUNK_RX, SCRATCH_RX, Bot, State, age_hours, clean, fetch, is_near, listing_details, now, parse_feed,
                    price_of, roadblock, safe_say, secret)

NAME = "Flip finder"
DEAL_FEEDS = ["electronics/phones/mobile-phones", "construction/tools-and-technics", "electronics/home-appliances",
              "electronics/computers", "electronics/audio-video-dvd-sat", "for-children/carriages"]
RATIO, MIN_PROFIT, FAR_PROFIT, MAX_RESALE, MIN_COMPS, MAX_BUY = 0.65, 20, 40, 150, 3, 100
STOCK_CAP, MAX_LOOKUPS, MAX_ALERTS, DAILY_CAP, MAX_AGE_MIN, CACHE_H = 300, 8, 5, 15, 90, 6
BULKY = ("washing-machine", "refrigerator", "fridge", "freezer", "cooker", "stove", "oven", "dishwasher", "centrifuge")
MILESTONES = (100, 250, 500, 1000, 2500)
TOP, DIGEST = 70, 50          # score >= TOP: instant alert with sound · >= DIGEST: silent 19:00 list · below: logged only
HELP = "Reply: bought 3 40 · sold 3 90 · skip 3 · stock"


def bot():
    return Bot(secret("FLIP_TG_TOKEN", "telegram-flip-bot"), secret("FLIP_TG_CHAT", "telegram-flip-chat"))


def model_query(item, feed):
    f = item["fields"]
    if "phones" in feed:
        mk, md = f.get("Marka", ""), f.get("Modelis", "")
        if not mk or not md or md.lower() in ("cits", "другой"):
            return None, None
        return f"{mk} {md}", {"model": md.lower(), "storage": f.get("Apjoms, Gb", "")}
    if not (f.get("Stāv.", "") or "").startswith("liet"):
        return None, None                      # used items only; "jaun." is mostly shops
    brand, model = f.get("Marka", ""), f.get("Modelis", "") or f.get("Marka+", "")
    token = re.sub(r"[^a-z0-9]", "", model.lower())
    if not brand or len(token) < 3 or token == re.sub(r"[^a-z0-9]", "", brand.lower()) or not re.search(r"\d", token):
        return None, None                      # need a real model code, not just a brand
    return f"{brand} {model}", {"token": token, "query": model}


def comparables(st, item, match):
    """Asking prices of similar used listings.
    Phones: the model's own ss.lv category, 3 pages (many listings per model).
    Everything else: one ss.lv search for the model code in the item's section (same model is rare there)."""
    where = item["link"].split("/msg/lv/")[1].rsplit("/", 1)[0]
    cache = st.load("comps_cache.json", {})
    key = f"{where}|{json.dumps(match, sort_keys=True)}|{(item['fields'].get('Stāv.', '') or '')[:4]}"
    if key in cache and time.time() - cache[key][0] < CACHE_H * 3600:
        return cache[key][1]
    used = (item["fields"].get("Stāv.", "lietota") or "lietota").startswith("liet")
    if "model" in match:
        pages = [f"https://www.ss.lv/lv/{where}/" + ("" if n == 1 else f"page{n}.html") for n in (1, 2, 3)]
    else:
        section = "/".join(where.split("/")[:2])
        pages = [f"https://www.ss.lv/lv/{section}/search-result/?q={urllib.parse.quote(match['query'])}"]
    prices = []
    for n, url in enumerate(pages, 1):
        s = fetch(url)
        rows = re.findall(r'<tr id="tr_(\d+)"(.*?)</tr>', s, re.S)
        for _, row in rows:
            link = re.search(r'href="(/msg/[^"]+)"', row)
            if not link or item["id"] in link.group(1):
                continue
            cells = [c for c in (clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)) if c]
            if len(cells) < 2 or BUY_RX.search(" ".join(cells)) or JUNK_RX.search(cells[0]):
                continue
            p = price_of(cells[-1])
            if p is None or (used and "jaun." in cells):
                continue
            if "model" in match:
                nums = [c for c in cells[1:-1] if re.fullmatch(r"\d{1,4}", c)]
                if match["storage"] and nums and nums[0] != match["storage"]:
                    continue
            elif match["token"] not in re.sub(r"[^a-z0-9]", "", " ".join(cells[:-1]).lower()):
                continue
            prices.append(p)
        if len(pages) == 1 or len(rows) < 30 or f"page{n + 1}.html" not in s:
            break
        time.sleep(1.5)
    cache = {k: v for k, v in cache.items() if time.time() - v[0] < CACHE_H * 3600}
    cache[key] = [time.time(), prices]
    st.save("comps_cache.json", cache)
    return prices


def judge(price, comps):
    """The deal rule. Returns (going price, profit, is_deal)."""
    if len(comps) < MIN_COMPS:
        return None, None, False
    going = statistics.median(comps)
    profit = going - price
    return going, profit, (price <= RATIO * going and profit >= MIN_PROFIT and going <= MAX_RESALE)


def score(price, comps, det):
    """Deal quality 0-100+. Returns (score, reasons). The parameter matrix, v1 (2026-09-17)."""
    going = statistics.median(comps)
    off, profit, pts, why = 1 - price / going, going - price, 0, []
    for limit, p in ((0.55, 40), (0.45, 30), (0.35, 20)):
        if off >= limit:
            pts += p
            break
    why.append(f"{off:.0%} under")
    pts += 25 if profit >= 70 else 15 if profit >= 40 else 5 if profit >= 20 else 0
    why.append(f"+€{profit:.0f}")
    pts += 15 if len(comps) >= 10 else 10 if len(comps) >= 5 else 0
    why.append(f"{len(comps)} similar")
    spread = (max(comps) - min(comps)) / going
    pts += 5 if spread <= 0.5 else -10 if spread > 1.0 else 0
    if spread > 1.0:
        why.append("similar prices vary a lot")
    if is_near(det["loc"]):
        pts += 10
    why.append(det["loc"])
    pts += -15 if det["photos"] == 0 else 5 if det["photos"] >= 3 else 0
    why.append(f"{det['photos']} photos")
    if det["dealer"]:
        pts -= 5
        why.append("shop/pawnshop")
    if det["battery"] is not None:
        pts += -15 if det["battery"] < 80 else -5 if det["battery"] < 85 else 5 if det["battery"] >= 90 else 0
        why.append(f"battery {det['battery']}%")
    if CRACK_RX.search(det["desc"]):
        pts -= 50
        why.append("cracked/smashed mentioned")
    elif SCRATCH_RX.search(det["desc"]):
        pts -= 5
        why.append("scratches mentioned")
    return pts, why


def stock_of(deals):
    return sum(d.get("bought", 0) for d in deals if d.get("state") == "bought")


def profit_of(deals):
    return sum(d["sold"] - d.get("bought", 0) for d in deals if d.get("state") == "sold")


def alert_text(d):
    warn = "⚠️ very cheap — check it works, no prepayment\n" if d["price"] < 0.3 * d["going"] else ""
    night = "🌙 posted overnight — may be gone\n" if d.get("overnight") else ""
    return (f"🔥 {d.get('score', 0)}/100 · #{d['no']} €{d['price']:.0f} → sells ~€{d['going']:.0f}\n"
            f"{' · '.join(d.get('why', []))}\n{night}{warn}{d['title']}\n{d['link']}")


def digest_text(items):
    lines = [f"📋 Good but not top today ({len(items)}) — no alert was sent for these; probably gone by now:"]
    for d in items:
        lines.append(f"#{d['no']} {d['score']}/100 · €{d['price']:.0f} → ~€{d['going']:.0f} · {d['loc']} · {d['title'][:45]}\n{d['link']}")
    return "\n".join(lines)


def status_text(deals):
    held = [d for d in deals if d.get("state") == "bought"]
    sold = [d for d in deals if d.get("state") == "sold"]
    return (f"Stock: {len(held)} items, €{stock_of(deals):.0f} (cap €{STOCK_CAP})\n"
            f"Sold: {len(sold)} · profit €{profit_of(deals):.0f}\nDeals sent so far: {sum(1 for d in deals if d.get('sent'))}")


def summary_text(stats, deals):
    return (f"☀️ Flip finder — since the last summary\n"
            f"Runs {stats.get('runs', 0)} · listings {stats.get('listings', 0)} · price checks {stats.get('price_checks', 0)} · "
            f"passed base rule {stats.get('deals', 0)} → top {stats.get('top', 0)} · digest {stats.get('digest', 0)} · "
            f"below bar {stats.get('below_bar', 0)} · errors {stats.get('errors', 0)}\n{status_text(deals)}")


def handle_replies(b, meta, deals):
    offset, texts = b.incoming(meta.get("tg_offset", 0))
    meta["tg_offset"] = offset
    for text in texts:
        w = text.lower().replace("#", "").split()
        cmd = w[0] if w else ""
        if cmd in ("stock", "status", "/status"):
            b.say(status_text(deals))
            continue
        if cmd in ("help", "/start", "/help", "hi"):
            b.say(f"Deals arrive here by themselves.\n{HELP}")
            continue
        if cmd not in ("bought", "sold", "skip") or len(w) < 2 or not w[1].isdigit():
            b.say(f"Didn't get that. {HELP}")
            continue
        d = next((x for x in deals if x["no"] == int(w[1])), None)
        if not d:
            b.say(f"No deal #{w[1]}.")
            continue
        if cmd == "skip":
            d["state"] = "skipped"
            b.say(f"#{d['no']} skipped.")
            continue
        amount = float(w[2].lstrip("€").replace(",", ".")) if len(w) > 2 and re.fullmatch(r"€?\d+([.,]\d+)?", w[2]) else None
        if amount is None:
            b.say(f"Add the price: {cmd} {d['no']} 40")
            continue
        before = profit_of(deals)
        if cmd == "bought":
            d.update(state="bought", bought=amount, bought_at=f"{now():%Y-%m-%d}")
        else:
            d.update(state="sold", sold=amount, sold_at=f"{now():%Y-%m-%d}")
        msg = f"#{d['no']} {cmd} €{amount:.0f}. Stock now €{stock_of(deals):.0f}."
        if stock_of(deals) > STOCK_CAP:
            msg += f"\n⚠️ Over the €{STOCK_CAP} cap — new deals pause until you sell."
        if cmd == "sold":
            after = profit_of(deals)
            msg += f"\nProfit on this one €{amount - d.get('bought', 0):.0f} · total €{after:.0f}"
            if sum(1 for x in deals if x.get("state") == "sold") == 1:
                msg += "\n🎉 First flip done."
            msg += "".join(f"\n🏁 €{m} profit reached." for m in MILESTONES if before < m <= after)
        b.say(msg)


def scan(dry=False):
    st = State("flip")
    meta, deals = st.load("meta.json", {}), st.load("deals.json", [])
    seen = dict.fromkeys(st.load("seen.json", []))
    t = now()
    today, quiet, first = t.strftime("%Y-%m-%d"), (t.hour >= 23 or t.hour < 7), (not seen and not dry)
    reached = lookups = checked = found = errors = 0
    tiers = {"top": 0, "digest": 0, "below": 0}
    for feed in DEAL_FEEDS:
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
            checked += 1
            if first or it["age_min"] > MAX_AGE_MIN or BUY_RX.search(it["title"]) or JUNK_RX.search(it["title"]):
                continue
            if not it["price"] or not 3 <= it["price"] <= MAX_BUY or any(x in it["link"] for x in BULKY):
                continue
            if stock_of(deals) >= STOCK_CAP or lookups >= MAX_LOOKUPS:
                continue
            query, match = model_query(it, feed)
            if not query:
                continue
            lookups += 1
            time.sleep(1.5)
            try:
                comps = comparables(st, it, match)
            except Exception as e:
                st.log(f"lookup error {query}: {e}")
                errors += 1
                continue
            going, profit, ok = judge(it["price"], comps)
            if dry and going is not None:
                print(f"  {query[:30]:30} €{it['price']:.0f} vs going €{going:.0f} ({len(comps)} similar) +€{profit:.0f}{'  <- DEAL' if ok else ''}")
            if not ok:
                continue
            det = listing_details(it["link"])
            if JUNK_RX.search(det["desc"]) or BUY_RX.search(det["desc"][:60]):
                if dry:
                    print("    skipped: warning words in the full description")
                continue
            if not (is_near(det["loc"]) or profit >= FAR_PROFIT):
                if dry:
                    print(f"    skipped: {det['loc']} is far and profit under €{FAR_PROFIT}")
                continue
            pts, why = score(it["price"], comps, det)
            tier = "top" if pts >= TOP else "digest" if pts >= DIGEST else "below"
            found += 1
            tiers[tier] += 1
            d = {"no": (deals[-1]["no"] + 1) if deals else 1, "at": t.strftime("%Y-%m-%d %H:%M"), "title": it["title"][:90],
                 "link": it["link"], "price": it["price"], "going": going, "comps": len(comps), "loc": det["loc"],
                 "score": pts, "why": why, "tier": tier, "state": "new" if tier == "top" else tier, "sent": False, "overnight": quiet}
            if dry:
                print(f"    SCORE {pts} → {tier}:", " · ".join(why))
            else:
                deals.append(d)
    if dry:
        print(f"checked {checked}, price checks {lookups}, passed base rule {found} {tiers}, errors {errors}")
        return
    b, sent = bot(), 0
    if first:
        meta["summary_date"] = today
        safe_say(b, "✅ Flip finder now runs on GitHub, 24/7 — laptop open or closed. Deals arrive here.", st)
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
    if t.hour >= 19 and meta.get("digest_date") != today:
        todays = sorted((d for d in deals if d.get("state") == "digest" and d["at"].startswith(today)), key=lambda d: -d["score"])[:8]
        if not todays or safe_say(b, digest_text(todays), st, silent=True):
            meta["digest_date"] = today
            for d in todays:
                d["state"] = "in_digest"
    try:
        handle_replies(b, meta, deals)
    except Exception as e:
        st.log(f"telegram error: {e}")
        errors += 1
    stats = meta.setdefault("stats", {})
    for k, v in (("runs", 1), ("listings", checked), ("price_checks", lookups), ("deals", found), ("top", tiers["top"]),
                 ("digest", tiers["digest"]), ("below_bar", tiers["below"]), ("errors", errors)):
        stats[k] = stats.get(k, 0) + v
    roadblock(meta, reached > 0, b, NAME, st)
    if t.hour >= 7 and meta.get("summary_date") != today and safe_say(b, summary_text(stats, deals), st):
        meta["summary_date"] = today
        meta["days"] = (meta.get("days", []) + [{"date": today, **stats}])[-60:]
        meta["stats"] = {}
    meta["last_run"] = t.strftime("%Y-%m-%d %H:%M")
    st.save("seen.json", list(seen)[-6000:])
    st.save("deals.json", deals[-500:])
    st.save("meta.json", meta)
    line = f"{'seeded ' if first else ''}checked {checked}, price checks {lookups}, base {found} {tiers}, sent {sent}, errors {errors}"
    st.log(line)
    print(line)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "scan":
        scan(dry="--dry" in sys.argv)
    elif cmd == "status":
        print(status_text(State("flip").load("deals.json", [])))
    else:
        print(__doc__)
