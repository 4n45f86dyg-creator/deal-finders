"""Shared plumbing for the deal finders: fetching, secrets, Telegram, state, Riga time."""
import email.utils, getpass, html, json, os, re, subprocess, sys, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

RIGA = ZoneInfo("Europe/Riga")
ROOT = Path(__file__).resolve().parent
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NEAR = ["rīga", "riga", "jūrmala", "mārupe", "ķekava", "salaspils", "ādaži", "olaine", "babīte", "stopiņi", "carnikava",
        "garkalne", "ropaži", "imanta", "purvciems", "ķengarags", "pļavnieki", "jugla", "ziepniekkalns", "zolitūde",
        "mežciems", "bolderāja", "mīlgrāvis", "iļģuciems", "āgenskalns", "rīgas"]
FREE_RX = re.compile(r"atdodu|atdod par|atdodam|par velti|par brīvu|bez maksas|bezmaksas|отдам|отдаю|бесплатно|даром", re.I)
BUY_RX = re.compile(r"\b(pērku|pērkam|nopirkšu|покупаю|покупаем|куплю|скупаем|скупка)", re.I)
JUNK_RX = re.compile(r"nedarbojas|nedarbojās|salūz|(?<!ne)bojāt|nestrādā|(?<!ne)salauzt|rezerves daļ|uz detaļ|detaļām|icloud|"
                     r"bloķēt|locked|на запчаст|не работ|(?<!не )разбит|(?<!не )сломан|for parts|broken|priekšapmaks|"
                     r"pārskaitījum|tikai piegād|предоплат|только доставк", re.I)


def now():
    return datetime.now(RIGA)


def fetch(url, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def secret(env, keychain_service):
    """On GitHub secrets arrive as env vars; on the Mac they live in the Keychain."""
    value = os.environ.get(env, "").strip()
    if value or sys.platform != "darwin":
        return value
    out = subprocess.run(["security", "find-generic-password", "-a", getpass.getuser(), "-s", keychain_service, "-w"],
                         capture_output=True, text=True)
    return out.stdout.strip()


class Bot:
    def __init__(self, token, chat, replies=True):
        self.token, self.chat, self.replies = token, str(chat), replies

    def call(self, method, **params):
        body = urllib.parse.urlencode(params).encode()
        return json.loads(fetch(f"https://api.telegram.org/bot{self.token}/{method}", data=body))

    def say(self, text):
        return self.call("sendMessage", chat_id=self.chat, text=text, disable_web_page_preview="true")

    def incoming(self, offset):
        """Boss's messages since offset -> (new offset, texts). Messages from anyone else are ignored."""
        texts = []
        for u in self.call("getUpdates", offset=offset, timeout=0).get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) == self.chat and msg.get("text"):
                texts.append(msg["text"].strip())
        return offset, texts


def safe_say(bot, text, st=None):
    try:
        return bool(bot.say(text).get("ok"))
    except Exception as e:
        if st:
            st.log(f"telegram error: {e}")
        return False


class State:
    """JSON files in state/<name>/. Between GitHub runs they travel encrypted on branch state-<name> (state.sh)."""
    def __init__(self, name):
        self.dir = ROOT / "state" / name
        self.dir.mkdir(parents=True, exist_ok=True)

    def load(self, file, default):
        p = self.dir / file
        return json.loads(p.read_text()) if p.exists() else default

    def save(self, file, obj):
        (self.dir / file).write_text(json.dumps(obj, ensure_ascii=False, indent=1))

    def log(self, msg):
        p = self.dir / "log.txt"
        lines = p.read_text().splitlines()[-500:] if p.exists() else []
        lines.append(f"{now():%Y-%m-%d %H:%M} {msg}")
        p.write_text("\n".join(lines) + "\n")


def clean(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def price_of(txt):
    m = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)\s*€", txt or "")
    if not m or "/" in (txt or ""):          # rent-style prices (€/mēn.) are not sale prices
        return None
    return float(m.group(1).replace(" ", "").replace(",", "."))


def parse_feed(path):
    """ss.lv category RSS -> newest ~20 listings with fields, price and age in minutes."""
    x = fetch(f"https://www.ss.lv/lv/{path}/rss/")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", x, re.S):
        link = re.search(r"<link>([^<]+)</link>", it).group(1).strip()
        title = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", it, re.S)
        fields = {}
        for k, v in re.findall(r"([^<>:\n]{2,25}):\s*(<b>.*?)<br/>", it):
            segs = [clean(s) for s in re.split(r"<br>", v)]
            fields[k.strip()] = segs[0]
            if len(segs) > 1:
                fields[k.strip() + "+"] = segs[1]
        pd = re.search(r"<pubDate>([^<]+)</pubDate>", it)
        age = (datetime.now(timezone.utc) - email.utils.parsedate_to_datetime(pd.group(1))).total_seconds() / 60 if pd else 0
        out.append({"id": link.rstrip("/").split("/")[-1].replace(".html", ""), "link": link, "age_min": age,
                    "title": clean(title.group(1)) if title else "", "fields": fields,
                    "price": price_of(fields.get("Cena", "")), "cena": fields.get("Cena", "")})
    return out


def listing_location(link):
    try:
        s = fetch(link)
    except Exception:
        return "?"
    m = re.search(r"(?:Pilsēta, rajons|Pilsēta|Rajons|Vieta):\s*</td>\s*<td[^>]*>(.*?)</td>", s, re.S)
    return clean(m.group(1)) if m else "?"


def is_near(loc):
    return any(n in (loc or "").lower() for n in NEAR)


def age_hours(stamp, t):
    return (t - datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=RIGA)).total_seconds() / 3600


def roadblock(meta, reachable, bot, name, st):
    """Tell Boss once when ss.lv stops answering for 3 runs, and once when it's back."""
    if reachable:
        if meta.get("fail_runs", 0) >= 3:
            safe_say(bot, f"✅ {name}: ss.lv reachable again.", st)
        meta["fail_runs"] = 0
    else:
        meta["fail_runs"] = meta.get("fail_runs", 0) + 1
        if meta["fail_runs"] == 3:
            safe_say(bot, f"🚧 {name}: can't reach ss.lv for 3 runs in a row. It keeps retrying — nothing for you to do yet.", st)
