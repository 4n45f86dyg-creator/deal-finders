#!/usr/bin/env python3
"""Everything Boss can reply to the flip bot from his phone. flip.py polls Telegram and calls handle()."""
import re
from common import ROOT, State, now, report_day

STOCK_CAP = 300                                    # max unsold stock in euros before deal alerts pause
MILESTONES = (100, 250, 500, 1000, 2500)
HELP = ("What you can send me:\n"
        "bought 3 40 — you bought deal #3 for €40\n"
        "sold 3 90 — you sold deal #3 for €90\n"
        "skip 3 — not interested in #3\n"
        "stock — what you hold now and profit so far\n"
        "status — today's numbers from both finders\n"
        "log rang Salons X, call back Thu — save a line while you dial\n"
        "notes — your last 10 lines\n"
        "leads — the next 5 businesses to call\n"
        "lead done 2 — cross lead #2 off the list\n"
        "help — this list")
SHORT = ("Didn't get that. Reply: bought 3 40 · sold 3 90 · skip 3 · stock · status · "
         "log <what happened> · notes · leads · lead done 2 · help")


class Ctx:
    """What a command may touch: the flip state on disk, plus the deals and meta of the run that called it."""
    def __init__(self, st, deals, meta):
        self.st, self.deals, self.meta = st, deals, meta


def stock_of(deals):
    return sum(d.get("bought", 0) for d in deals if d.get("state") == "bought")


def profit_of(deals):
    return sum(d["sold"] - d.get("bought", 0) for d in deals if d.get("state") == "sold")


def stock_text(deals):
    held = [d for d in deals if d.get("state") == "bought"]
    sold = [d for d in deals if d.get("state") == "sold"]
    return (f"Stock: {len(held)} items, €{stock_of(deals):.0f} (cap €{STOCK_CAP})\n"
            f"Sold: {len(sold)} · profit €{profit_of(deals):.0f}\nDeals sent so far: {sum(1 for d in deals if d.get('sent'))}")


def deal_reply(cmd, w, ctx):
    """bought N price · sold N price · skip N — the numbers under every deal alert."""
    deals = ctx.deals
    d = next((x for x in deals if x["no"] == int(w[1])), None)
    if not d:
        return f"No deal #{w[1]}."
    if cmd == "skip":
        d["state"] = "skipped"
        return f"#{d['no']} skipped."
    amount = float(w[2].lstrip("€").replace(",", ".")) if len(w) > 2 and re.fullmatch(r"€?\d+([.,]\d+)?", w[2]) else None
    if amount is None:
        return f"Add the price: {cmd} {d['no']} 40"
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
    return msg


def log_note(rest, ctx):
    """One line about a call, typed while dialling. Kept in the flip state so it survives every run."""
    if not rest:
        return "Write what happened: log rang Salons X, no answer, call back Thu"
    t = now()
    notes = ctx.st.load("notes.json", [])
    notes.append({"at": t.strftime("%Y-%m-%d %H:%M"), "text": rest})
    notes = notes[-500:]
    ctx.st.save("notes.json", notes)
    today = sum(1 for n in notes if n.get("at", "").startswith(t.strftime("%Y-%m-%d")))
    return f"Logged ({today} today)."


def when(stamp):
    return f"{stamp[8:10]}.{stamp[5:7]} {stamp[11:16]}" if len(stamp) >= 16 else stamp


def notes_text(ctx):
    notes = ctx.st.load("notes.json", [])
    if not notes:
        return "No notes yet. Send: log rang Salons X, no answer"
    lines = [f"{when(n.get('at', ''))} {n.get('text', '')}" for n in reversed(notes[-10:])]
    return f"Last {len(lines)} notes, newest first:\n" + "\n".join(lines)


def leads_text(ctx):
    leads = ctx.st.load("leads.json", [])
    if not leads:
        return "No lead list loaded yet — HQ puts it here."
    left = [x for x in leads if x.get("state") != "done"]
    if not left:
        return f"All {len(leads)} leads worked. Tell HQ to load the next list."
    lines = [f"#{x['no']} {x.get('where', '')} · {(x.get('name') or x.get('what') or '')}\n{x.get('link', '')}" for x in left[:5]]
    return f"Next {len(lines)} to call ({len(left)} left):\n" + "\n".join(lines) + "\nDone with one: lead done 1"


def lead_done(w, ctx):
    if len(w) < 3 or w[1].lower() != "done" or not w[2].isdigit():
        return "Cross one off like this: lead done 2"
    leads = ctx.st.load("leads.json", [])
    x = next((y for y in leads if y.get("no") == int(w[2])), None)
    if not x:
        return f"No lead #{w[2]}."
    x["state"] = "done"
    ctx.st.save("leads.json", leads)
    return f"Lead #{x['no']} done. {sum(1 for y in leads if y.get('state') != 'done')} left."


def day_counts(meta, day):
    days = (meta or {}).get("days")
    return days.get(day, {}) if isinstance(days, dict) else {}


def free_meta():
    """flip.py mounts only the flip state, so the free finder's numbers are here only when something restored them too."""
    return State("free").load("meta.json", {}) if (ROOT / "state" / "free" / "meta.json").exists() else None


def status_text(ctx):
    """One line per employee for the current stats day, the way the 20:00 report counts them."""
    t = now()
    day = report_day(t)
    f = day_counts(ctx.meta, day)
    lines = [f"📊 {'Today so far' if day == t.strftime('%Y-%m-%d') else 'Since 20:00'}",
             f"Flip: {f.get('runs', 0)} runs · {f.get('listings', 0)} new listings · {f.get('price_checks', 0)} price checks · "
             f"easy money {f.get('top', 0)} · worth a look {f.get('worth_a_look', 0)} · errors {f.get('errors', 0)}"]
    g = free_meta()
    if g is None:
        lines.append("Free: not in this run — its numbers come in the 20:00 report.")
    else:
        gd = day_counts(g, day)
        lines.append(f"Free: {gd.get('runs', 0)} runs · {gd.get('giveaways', 0)} giveaways · worth picking up {gd.get('worth_it', 0)}")
    held = [d for d in ctx.deals if d.get("state") == "bought"]
    sold = [d for d in ctx.deals if d.get("state") == "sold"]
    lines.append(f"Money: stock {len(held)} items €{stock_of(ctx.deals):.0f} of €{STOCK_CAP} · sold {len(sold)} · profit €{profit_of(ctx.deals):.0f}")
    return "\n".join(lines)


def handle(text, ctx):
    """One Telegram message in, the reply out. Never empty — silence looks broken on the phone."""
    raw = (text or "").strip()
    parts = raw.split(None, 1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    w = raw.replace("#", "").split()
    cmd = w[0].lower() if w else ""
    if cmd == "log":
        return log_note(rest, ctx)
    if cmd in ("notes", "/notes"):
        return notes_text(ctx)
    if cmd in ("leads", "/leads"):
        return leads_text(ctx)
    if cmd in ("lead", "/lead"):
        return lead_done(w, ctx)
    if cmd in ("stock", "/stock"):
        return stock_text(ctx.deals)
    if cmd in ("status", "/status"):
        return status_text(ctx)
    if cmd in ("help", "/start", "/help", "hi"):
        return f"Deals arrive here by themselves.\n{HELP}"
    if cmd not in ("bought", "sold", "skip") or len(w) < 2 or not w[1].isdigit():
        return SHORT
    return deal_reply(cmd, w, ctx)
