

"""BTC-Alarm: 4h-RSI unter 30, Wochen-EMA, Fear & Greed.

Laeuft dauerhaft auf GitHub Actions und prueft alle 2 Minuten und schickt Push-Nachrichten ueber ntfy."""

import json, os, sys, time, urllib.request

from datetime import datetime, timezone


TOPIC = os.environ.get("NTFY_TOPIC", "")

H4 = 4 * 3600 * 1000


def get(url):

    req = urllib.request.Request(url, headers={"User-Agent": "btc-alarm"})

    with urllib.request.urlopen(req, timeout=30) as r:

        return json.loads(r.read().decode())


def src_binance(base):

    def f(interval, limit):

        k = get(f"{base}/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}")

        return [(int(x[0]), float(x[4]), int(x[6])) for x in k]

    return f


def src_kraken(interval, limit):

    mins = {"4h": 240, "1w": 10080}[interval]

    r = get(f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={mins}")

    if r.get("error"): raise RuntimeError(r["error"])

    rows = [v for k, v in r["result"].items() if k != "last"][0]

    return [(int(x[0]) * 1000, float(x[4]), int(x[0]) * 1000 + mins * 60000 - 1) for x in rows][-limit:]


def src_bitstamp(interval, limit):

    if interval == "4h":

        r = get("https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=14400&limit=1000")

        rows = r["data"]["ohlc"]

        return [(int(x["timestamp"]) * 1000, float(x["close"]), int(x["timestamp"]) * 1000 + H4 - 1) for x in rows][-limit:]

    # Wochenkerzen aus Tageskerzen bauen (Woche beginnt Montag 00:00 UTC)

    rows = get("https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=1000")["data"]["ohlc"]

    weeks = {}

    for x in rows:

        t = int(x["timestamp"]); wk = t - ((t // 86400 + 3) % 7) * 86400

        weeks[wk] = float(x["close"])

    W = 7 * 86400 * 1000

    return [(wk * 1000, c, wk * 1000 + W - 1) for wk, c in sorted(weeks.items())][-limit:]


SOURCES = [("Binance (data-api)", src_binance("https://data-api.binance.vision")),

           ("Binance (api)", src_binance("https://api.binance.com")),

           ("Kraken", src_kraken), ("Bitstamp", src_bitstamp)]


def klines(interval, limit):

    for name, f in SOURCES:

        try:

            k = f(interval, limit)

            age = time.time() * 1000 - k[-1][0]

            maxage = {"4h": 8 * 3600e3, "1w": 15 * 86400e3}[interval]

            if age > maxage:

                raise RuntimeError(f"Daten veraltet (letzte Kerze vor {age / 3600e3:.0f} h)")

            if len(k) > 60:

                print(f"Kursdaten {interval} von {name}"); return k

        except Exception as e:

            print(f"{name} nicht erreichbar: {e}")

    raise RuntimeError("Keine Kursquelle erreichbar")


def de(x, nk=0):

    """Zahl im deutschen Format: 62.907 bzw. 27,7"""

    t = f"{x:,.{nk}f}"

    return t.replace(",", "X").replace(".", ",").replace("X", ".")


def rsi(closes, n=14):

    # RSI nach Wilder (wie TradingView)

    gains = losses = 0.0

    out = [None] * len(closes)

    for i in range(1, len(closes)):

        d = closes[i] - closes[i - 1]

        g, l = max(d, 0), max(-d, 0)

        if i <= n:

            gains += g; losses += l

            if i == n:

                gains /= n; losses /= n

                out[i] = 100 - 100 / (1 + gains / losses) if losses else 100

        else:

            gains = (gains * (n - 1) + g) / n

            losses = (losses * (n - 1) + l) / n

            out[i] = 100 - 100 / (1 + gains / losses) if losses else 100

    return out


def ema(values, n=50):

    k = 2 / (n + 1); e = values[0]; out = []

    for v in values:

        e = v * k + e * (1 - k); out.append(e)

    return out


def sent_keys():

    # ntfy merkt sich Nachrichten 12 Stunden -> verhindert doppelte Alarme

    if not TOPIC: return set()

    try:

        req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}/json?poll=1&since=12h", headers={"User-Agent": "btc-alarm"})

        with urllib.request.urlopen(req, timeout=30) as r:

            lines = r.read().decode().splitlines()

        keys = set()

        for ln in lines:

            m = json.loads(ln)

            for t in m.get("tags", []):

                if t.startswith("k-"): keys.add(t)

        return keys

    except Exception as e:

        print("ntfy-Verlauf nicht lesbar:", e); return set()


SENT = None

def push(title, msg, prio="high", key=None, emoji="chart_with_downwards_trend"):

    global SENT

    if key:

        if SENT is None: SENT = sent_keys()

        if "k-" + key in SENT:

            print("schon gemeldet:", key); return

    print(title, "\n", msg)

    if not TOPIC:

        print("(kein NTFY_TOPIC gesetzt - nur Testausgabe)"); return

    prio_n = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}[prio]

    tags = [emoji] + ([f"k-{key}"] if key else [])

    body = json.dumps({"topic": TOPIC, "title": title, "message": msg, "priority": prio_n, "tags": tags}).encode()

    req = urllib.request.Request("https://ntfy.sh/", data=body, headers={"Content-Type": "application/json"})

    urllib.request.urlopen(req, timeout=30)

    if key and SENT is not None: SENT.add("k-" + key)


def main():

    now = int(time.time() * 1000)

    k4all = klines("4h", 500)

    k4 = [x for x in k4all if x[2] < now]          # abgeschlossene Kerzen

    live = k4all[-1] if k4all[-1][2] >= now else None  # laufende Kerze

    closes = [x[1] for x in k4]

    r = rsi(closes)

    rsi_closed, rsi_before = r[-1], r[-2]

    price = live[1] if live else closes[-1]

    rsi_live = rsi(closes + [price])[-1] if live else rsi_closed


    kw = [x for x in klines("1w", 400) if x[2] < now]

    wc = [x[1] for x in kw]; e = ema(wc)

    w_ema, w_close = e[-1], wc[-1]

    long_mode = w_close > w_ema

    try:

        fg = get("https://api.alternative.me/fng/?limit=2")["data"]

        fng, fng_prev = int(fg[0]["value"]), int(fg[1]["value"])

    except Exception as ex:

        print("Fear & Greed nicht erreichbar:", ex); fng = fng_prev = None

        push("STÖRUNG: Fear & Greed nicht abrufbar", f"Der RSI-Alarm läuft weiter, aber ohne Fear-&-Greed-Prüfung.\nFehler: {str(ex)[:120]}",

             "default", key=f"err-fng-{datetime.now(timezone.utc):%Y%m%d}")


    stop, liq_max = 0.90 * w_ema, 0.85 * w_ema

    abst = (price / w_ema - 1) * 100

    info = (f"BTC {de(price)} $ | 4h-RSI {de(rsi_live, 1)}\n"

            f"Wochen-EMA50 {de(w_ema)} $ (Kurs {'+' if abst >= 0 else ''}{de(abst, 1)} %)\n"

            f"Fear & Greed: {fng if fng is not None else '?'}\n"

            f"Stop (10 % unter EMA): {de(stop)} $\n"

            f"Liquidation muss unter {de(liq_max)} $ liegen")

    print(datetime.now(timezone.utc), info.replace("\n", " | "))


    def verdict():

        if not long_mode: return "KEIN Kauf: Wochenschluss unter EMA50 (kein Long-Modus)."

        if fng is not None and fng > 65: return f"KEIN Kauf: Fear & Greed {fng} über 65."

        return "Kaufregeln erfüllt."


    # 1) LIVE: RSI faellt waehrend der laufenden Kerze unter 30 (einmal pro Kerze)

    if live and rsi_closed >= 30 and rsi_live < 30:

        push("LIVE: 4h-RSI unter 30", verdict() + "\n\n" + info,

             "high", key=f"live-{live[0]}")


    # Sicherheitsnetz: fiel der RSI kurz vor Kerzenschluss unter 30 und kein Lauf hat es mitbekommen

    if now - k4[-1][2] < 3600 * 1000 and rsi_before >= 30 > rsi_closed:

        push("4h-RSI unter 30", verdict() + "\n\n" + info, "high", key=f"live-{k4[-1][0]}")


    # 3) Wochenschluss unter / ueber EMA50

    if now - kw[-1][2] < 3 * 24 * 3600 * 1000:

        if w_close < w_ema and wc[-2] >= e[-2]:

            push("AUSSTIEG: Wochenschluss unter EMA50", "Alle Futures-Trades schließen!\n\n" + info, "urgent", key=f"wdown-{kw[-1][0]}")

        elif w_close > w_ema and wc[-2] <= e[-2]:

            push("Long-Modus: Wochenschluss über EMA50", info, "high", key=f"wup-{kw[-1][0]}")


    # 4) Fear & Greed auf 85 oder hoeher

    if fng is not None and fng >= 85 > fng_prev:

        push("Fear & Greed 85+: Hälfte realisieren", info, "high", key=f"fg85-{datetime.now(timezone.utc):%Y%m%d}")



    # 5) Sonntags-Statusmeldung (10 Uhr oesterreichischer Zeit)

    try:

        from zoneinfo import ZoneInfo

        wien = datetime.now(ZoneInfo("Europe/Vienna"))

    except Exception:

        wien = datetime.now(timezone.utc)

    if wien.weekday() == 6 and wien.hour == 10:

        push("BTC-RSI Alarm läuft weiterhin fehlerfrei", info, "default", key=f"hb-{wien:%Y%m%d}", emoji="white_check_mark")


def lebenszeichen():

    """Meldet sich bei healthchecks.io. Bleibt das aus, schickt healthchecks.io eine Warnung."""

    url = os.environ.get("HC_URL", "").strip()

    if not url:

        return

    try:

        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "btc-alarm"}), timeout=10)

    except Exception as e:

        print("Lebenszeichen fehlgeschlagen:", e)


if __name__ == "__main__":

    if len(sys.argv) > 1 and sys.argv[1] == "test":

        lines = []

        for name, f in SOURCES:

            for iv in ("4h", "1w"):

                try:

                    k = f(iv, 300); lines.append(f"OK   {name} {iv}: {len(k)} Kerzen, letzter Kurs {de(k[-1][1])} $")

                except Exception as e:

                    lines.append(f"FEHLER {name} {iv}: {str(e)[:60]}")

        try:

            v = get("https://api.alternative.me/fng/?limit=1")["data"][0]["value"]; lines.append(f"OK   Fear & Greed: {v}")

        except Exception as e:

            lines.append(f"FEHLER Fear & Greed: {str(e)[:60]}")

        push("Test: BTC-Alarm funktioniert", "\n".join(lines), "default")

    else:

        # Dauerbetrieb: prueft alle PAUSE Sekunden, bis LAUFZEIT abgelaufen ist

        laufzeit = int(os.environ.get("LAUFZEIT_MIN", "0")) * 60

        pause = int(os.environ.get("PAUSE_SEK", "120"))

        ende = time.time() + laufzeit

        while True:

            try:

                main()

                lebenszeichen()

            except Exception as ex:

                # hoechstens alle 6 Stunden eine Stoerungsmeldung

                slot = datetime.now(timezone.utc).strftime("%Y%m%d") + str(datetime.now(timezone.utc).hour // 6)

                push("STÖRUNG: BTC-Alarm funktioniert nicht", f"Der Alarm konnte keine Kursdaten prüfen.\nFehler: {str(ex)[:200]}\n\nBitte das Skript prüfen lassen.",

                     "high", key=f"err-{slot}", emoji="warning")

                print("Fehler:", ex)

            if time.time() + pause > ende:

                break

            time.sleep(pause)

