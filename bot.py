#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot de trading papier XAU/USD (or) — version autonome, sans dépendance Base44.
Conçu pour tourner via GitHub Actions toutes les 15 minutes.

Stratégie : croisement EMA9/EMA21 + filtre RSI14, gestion du risque par ATR
SCALPING 5 min : EMA9/21 + RSI14 + ATR14, SL 1x ATR, TP 1.5x ATR, risque 2% par trade.
Prix : futures or GC=F (proxy fidèle du spot XAUUSD) via Yahoo Finance.
Capital papier initial : 50 EUR.

Chaque cycle :
  1. Lit l'état depuis state.json (créé au premier lancement)
  2. Analyse le marché et gère la position ouverte (SL/TP/signal inverse)
  3. Ouvre un trade si les conditions sont réunies
  4. Sauvegarde state.json + trades.json
  5. Poste un compte-rendu sur le webhook Discord (variable DISCORD_WEBHOOK_URL)

Sortie : affiche un résumé JSON du cycle sur stdout (visible dans les logs GitHub Actions).
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

STATE_FILE = "state.json"
TRADES_FILE = "trades.json"
RISK_PCT = 0.02          # risque par trade : 2% du capital

# ---- MODE CHALLENGE PROP FIRM (simulation FTMO) ----
# Miroir : chaque trade papier est rejoue sur un compte fictif de 10 000 EUR
# avec un risque reduit, et les regles FTMO sont verifiees a chaque cloture.
CHALLENGE_FILE = "challenge.json"
CH_START = 10000.0     # solde initial du compte simule (challenge 10k)
CH_RISK = 0.01         # risque par trade en mode challenge (1% au lieu de 2%)
CH_DAILY_LOSS = 0.05   # FTMO : max 5% de perte journaliere (sur solde initial)
CH_MAX_DD = 0.10       # FTMO : max 10% de drawdown total (statique, sur solde initial)
CH_P1_TARGET = 1.10    # phase 1 : objectif +10%
CH_P2_TARGET = 1.05    # phase 2 : objectif +5%
CH_MIN_DAYS = 4        # FTMO : minimum 4 jours de trading par phase
# ---- STRATEGIE CONFIRMATION + RATIO 1:2,5 (vision d'Enzo) ----
# On n'anticipe plus le retournement : apres une chute (resp. hausse), on attend
# la 1re bougie 5m qui CLOTURE dans le sens inverse, puis une 2e bougie qui confirme.
# Entree seulement apres cette double confirmation, dans le sens du biais 1h — 24/7.
# Gestion : risque 2% par trade, objectif fixe a 2,5 x le risque (1 pour 2,5),
# breakeven a +1R (stop remis a l'entree) puis ON LAISSE COURIR jusqu'au TP
# ou au stop — aucun trailing, aucune cloture temporelle (demande Enzo 19/09).
TIMEFRAME = "5m"         # bougies 5 minutes (base de la confirmation)
# (plus de trailing stop depuis le 19/09 : le trade court jusqu'au TP ou au SL)

# ---- PAUSE WEEK-END (demande Enzo 19/09) : marche de l'or ferme ----
WEEKEND_START_H = 20   # vendredi 20h00 Paris : plus aucune prise de trade
WEEKEND_RESUME_H = 1   # lundi 01h00 Paris : reprise (l'or spot redemarre avec l'Asie)

# ---- COUCHE IA (portee du bot BTC, demande Enzo 19/09) ----
LEARN_MIN_TRADES = 10     # nb min de trades dans un contexte avant de le juger
LEARN_WINDOW = 15         # juge sur les 15 derniers trades du contexte
LEARN_BAD_R = -0.15       # esperance en R sous laquelle un contexte est "mauvais"
LEARN_RELEASE_R = 0.05    # esperance recente au-dessus de laquelle on reactive
LEARN_STRIKES = 2         # 2 revues consecutives mauvaises avant d'eviter un contexte
LEARN_COOLDOWN_MS = 12 * 3600 * 1000   # max 1 NOUVEL avoid par 12 h
LEARN_AVOID_TTL_MS = 14 * 24 * 3600 * 1000   # contexte evite reessaie apres 14 jours
LEARN_REVIEW_EVERY = 25   # revue globale publiee tous les X trades fermes
VOL_CALME_PCT = 0.08      # ATR 5m < 0.08% du prix = contexte "calme", sinon "nerveux"
LEARNING_FILE = "gold_learning.json"
JOURNAL_FILE = "gold_journal.json"
BE_R_MULT = 1.0          # breakeven des que le prix fait +1 x le risque (trade "couvert")
TP_R_MULT = 2.5          # objectif = 2,5 x le risque : trades "1 pour 2,5" (vision d'Enzo)
SL_BUFFER_ATR = 0.3      # SL initial = creux (ou sommet) du retournement +/- 0.3 x ATR
CONF_MAX_RUN_ATR = 2.5   # si le prix a deja couru > 2.5 x ATR au-dela du creux -> on ne chase pas
DROP_MIN_ATR = 1.2       # la chute (resp. hausse) initiale doit faire >= 1.2 x ATR
# trades 24/7 y compris la nuit, s'ils respectent la confirmation + le biais 1h

# ---- BLACKOUT NEWS AUTO : le bot telecharge le calendrier economique (ForexFactory,
# flux JSON public sans cle) et bloque toute NOUVELLE entree autour des annonces a
# fort impact USD / GBP / EUR. Les positions ouvertes restent gerees normalement.
NEWS_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CURRENCIES = ("USD", "GBP", "EUR")
NEWS_IMPACT = ("High",)
NEWS_MIN_BEFORE = 30      # pas d'entree a partir de 30 min avant l'annonce
NEWS_MIN_AFTER = 60       # ... jusqu'a 60 min apres (volatilite post-annonce)
NEWS_CACHE_TTL_MS = 60 * 60 * 1000   # calendrier rafraichi toutes les heures (cache dans state)


def fetch_news_events():
    """Telecharge le calendrier ForexFactory -> fenetres de blackout [(debut, fin, label)] en UTC."""
    r = requests.get(NEWS_FEED_URL, timeout=15, headers={"User-Agent": UA})
    r.raise_for_status()
    events = []
    for e in r.json():
        try:
            if e.get("impact") not in NEWS_IMPACT or e.get("country") not in NEWS_CURRENCIES:
                continue
            # les dates du flux sont en heure de New York, sans fuseau explicite
            when = datetime.fromisoformat(e["date"]).replace(tzinfo=ZoneInfo("America/New_York"))
            utc = when.astimezone(timezone.utc)
            paris_h = when.astimezone(ZoneInfo("Europe/Paris")).strftime("%Hh%M")
            events.append([
                (utc - timedelta(minutes=NEWS_MIN_BEFORE)).isoformat(),
                (utc + timedelta(minutes=NEWS_MIN_AFTER)).isoformat(),
                f"{e['country']} — {e.get('title', 'annonce')} ({paris_h} Paris)",
            ])
        except Exception:
            continue
    return events


def active_news_blackout(state):
    """Label de l'annonce en blackout en cours, sinon None. Calendrier mis en cache dans state."""
    cache = state.get("news_cache") or {}
    now_ms = int(time.time() * 1000)
    if now_ms - cache.get("fetched_ms", 0) > NEWS_CACHE_TTL_MS:
        try:
            events = fetch_news_events()
        except Exception:
            events = cache.get("events", [])   # flux indisponible -> on garde l'ancien calendrier
        cache = {"fetched_ms": now_ms, "events": events}
        state["news_cache"] = cache
    now = datetime.now(timezone.utc)
    for start_s, end_s, label in cache.get("events", []):
        try:
            if datetime.fromisoformat(start_s) <= now <= datetime.fromisoformat(end_s):
                return label
        except Exception:
            continue
    return None


STARTING_EQUITY = 50.0   # capital papier initial en EUR
PAUSE_THRESHOLD = 5.0    # le bot se met en pause si le capital tombe sous 5 EUR
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"
RELAY_URL = "https://superagent-583f72c3.base44.app/functions/discordTradingSend"  # relais securise Discord
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))   # verification toutes les 60 s
CHECKS_PER_RUN = int(os.environ.get("CHECKS_PER_RUN", "13"))           # ~13 min de surveillance par run


# ---------------------------------------------------------------- utilitaires

def weekend_block(now_paris):
    """True si le marche de l'or est ferme : du vendredi 20h au lundi 01h (Paris).
    (Demande Enzo 19/09 : le week-end le gold est ferme, pas de prise de trade.)"""
    wd, h = now_paris.weekday(), now_paris.hour
    return (wd == 4 and h >= WEEKEND_START_H) or wd in (5, 6) or (wd == 0 and h < WEEKEND_RESUME_H)


# ---------------------------------------------------------------- couche IA
def bucket_of(trade):
    """Cle de contexte d'un trade : tranche 4h Paris x regime de volatilite."""
    h = trade.get("hour_paris")
    if h is None:
        return None
    vol = "calme" if trade.get("vol_pct", 0) < VOL_CALME_PCT else "nerveux"
    return f"{h // 4 * 4:02d}-{h // 4 * 4 + 4:02d}h {vol}"


def bucket_stats(trades, key):
    """(n, wr, exp_r) sur les LEARN_WINDOW derniers trades du contexte."""
    b = [t for t in trades if bucket_of(t) == key and t.get("r") is not None][-LEARN_WINDOW:]
    if not b:
        return None
    wins = [t for t in b if (t.get("pnl_eur") or 0) > 0]
    wr = round(len(wins) / len(b) * 100)
    exp_r = round(sum(t["r"] for t in b) / len(b), 2)
    return {"n": len(b), "wr": wr, "exp_r": exp_r}


def review_learning(state, trades, learning, journal):
    """Apres un trade ferme : recalcule chaque contexte et decide des filtres.
    Retourne les nouvelles entrees de journal (publiees par main sur Discord)."""
    new_entries = []
    now_ms = int(time.time() * 1000)
    keys = sorted({k for t in trades if (k := bucket_of(t))})
    for key in keys:
        st = bucket_stats(trades, key)
        if st is None or st["n"] < LEARN_MIN_TRADES:
            continue
        avoided = key in learning.get("avoid", {})
        if not avoided:
            if st["exp_r"] < LEARN_BAD_R:
                learning.setdefault("strikes", {})[key] = learning.get("strikes", {}).get(key, 0) + 1
                if learning["strikes"][key] >= LEARN_STRIKES:
                    if now_ms - learning.get("last_avoid_ms", 0) >= LEARN_COOLDOWN_MS:
                        learning.setdefault("avoid", {})[key] = {"since": now_ms, "stats": st}
                        learning["last_avoid_ms"] = now_ms
                        learning["strikes"][key] = 0
                        new_entries.append({
                            "t": now_ms, "type": "AVOID", "bucket": key, "stats": st,
                            "title": "Contexte perdant identifié",
                            "reason": (f"{key} : {st['n']} trades, {st['wr']} % victoires, "
                                       f"esperance {st['exp_r']} R — j'arrete d'entrer dans ce creneau")})
            else:
                learning["strikes"][key] = 0
        else:
            since = learning["avoid"][key].get("since", 0)
            if now_ms - since >= LEARN_AVOID_TTL_MS:
                del learning["avoid"][key]
                learning["strikes"][key] = LEARN_STRIKES - 1
                new_entries.append({
                    "t": now_ms, "type": "RELEASE", "bucket": key, "stats": st,
                    "title": "Contexte remis à l'essai",
                    "reason": (f"{key} : 14 jours d'évitement écoulés — je reteste ce contexte "
                               f"en période d'essai")})
                continue
            after = [t for t in trades if bucket_of(t) == key and t.get("t_out_ms", 0) > since]
            if len(after) >= 3 and sum(t["r"] for t in after) / len(after) > LEARN_RELEASE_R:
                del learning["avoid"][key]
                st2 = {"n": len(after),
                       "wr": round(len([t for t in after if (t.get("pnl_eur") or 0) > 0]) / len(after) * 100),
                       "exp_r": round(sum(t["r"] for t in after) / len(after), 2)}
                new_entries.append({
                    "t": now_ms, "type": "RELEASE", "bucket": key, "stats": st2,
                    "title": "Contexte réactivé",
                    "reason": f"{key} : {st2['n']} trades depuis l'évitement, esperance {st2['exp_r']} R — je reteste"})
    if state.get("trade_count", 0) % LEARN_REVIEW_EVERY == 0 and state.get("trade_count", 0) > 0:
        summary = []
        for key in keys:
            st = bucket_stats(trades, key)
            if st and st["n"] >= LEARN_MIN_TRADES:
                summary.append(f"{key} : {st['wr']} % WR, {st['exp_r']} R")
        if summary:
            new_entries.append({
                "t": now_ms, "type": "REVIEW", "bucket": "global", "stats": {},
                "title": "Revue périodique", "reason": " | ".join(summary)[:900]})
    learning["last_review_ms"] = now_ms
    if new_entries:
        journal.extend(new_entries)
        journal[:] = journal[-200:]
    return new_entries


def entry_blocked_by_learning(learning, hour_paris, vol_pct):
    """None si l'entree est autorisee, sinon le contexte evite."""
    vol = "calme" if (vol_pct or 0) < VOL_CALME_PCT else "nerveux"
    key = f"{hour_paris // 4 * 4:02d}-{hour_paris // 4 * 4 + 4:02d}h {vol}"
    if key in learning.get("avoid", {}):
        return key
    return None


def build_ia_embed(entries, learning, state):
    """Revue IA : ce que le bot a appris de ses derniers trades."""
    fields = []
    for e in entries[-3:]:
        if e["type"] == "AVOID":
            v = ("\U0001F6AB N'entre plus dans **" + e["bucket"] + "**\n"
                 + str(e["stats"]["n"]) + " trades · " + str(e["stats"]["wr"]) + " % victoires · "
                 + "esperance " + str(e["stats"]["exp_r"]) + " R — j'évite ce contexte")
        elif e["type"] == "RELEASE":
            v = ("\u2705 Réactive **" + e["bucket"] + "**\n"
                 + "esperance recente redevenue positive — je retente ce contexte")
        else:
            v = str(e.get("reason", ""))[:300]
        fields.append({"name": e.get("title", e["type"]), "value": v, "inline": False})
    if not fields:
        fields.append({"name": "Bilan", "value": "Rien de nouveau à signaler.", "inline": False})
    return {
        "title": "\U0001F9E0 REVUE IA OR — j'apprends de mes trades",
        "color": 0x9B59B6,
        "fields": fields + [
            {"name": "\U0001F5F3 Contextes évités actuellement", "inline": False,
             "value": "\n".join("· " + k for k in learning.get("avoid", {})) or "aucun"},
            {"name": "\U0001F522 Recul", "inline": True,
             "value": str(state.get("trade_count", 0)) + " trades analysés"},
        ],
        "footer": {"text": "L'IA n'ajuste QUE les filtres d'entrée — risque 2%, SL, TP 2,5R et breakeven restent intangibles"},
    }


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def ema_series(values, period):
    k = 2 / (period + 1)
    out = []
    e = values[0]
    for i, v in enumerate(values):
        e = v if i == 0 else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi14(closes):
    if len(closes) < 15:
        return 50.0
    gains = losses = 0.0
    for i in range(len(closes) - 14, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / 14) / (losses / 14)
    return 100 - 100 / (1 + rs)


def atr14(highs, lows, closes):
    if len(highs) < 15:
        return 3.0
    total = 0.0
    for i in range(len(highs) - 14, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        total += tr
    return total / 14


# ------------------------------------------------------------------- données

def fetch_candles(symbol="PAXG-USD", gran=None):
    """Bougies PAXG-USD (or spot, proxy du XAU/USD a +/-0.3%) via Coinbase.
    Donnees temps reel 24/7 — contrairement a Yahoo (retard ~10 min).
    gran = 60 (1m), 300 (5m) ou 3600 (1h)."""
    if gran is None:
        gran = {"1m": 60, "5m": 300, "15m": 900}[TIMEFRAME]
    r = requests.get(
        f"https://api.exchange.coinbase.com/products/{symbol}/candles?granularity={gran}",
        timeout=20,
    )
    r.raise_for_status()
    rows = r.json()  # decroissant : [time_sec, low, high, open, close, volume]
    candles = [
        {"t": row[0] * 1000, "o": row[3], "h": row[2], "l": row[1], "c": row[4]}
        for row in sorted(rows)
    ]
    return candles


def fetch_ticker(symbol="PAXG-USD"):
    """Dernier prix traite en temps reel (Coinbase)."""
    try:
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/ticker", timeout=10
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def fetch_eurusd():
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/EURUSD=X"
        "?interval=1d&range=1d"
    )
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        p = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        if p and 0.5 < p < 2:
            return float(p)
    except Exception:
        pass
    return 1.08  # repli approximatif


# ------------------------------------------------------------------ Discord

def post_discord(webhook_url, embed, username="Bot Or \U0001F916"):
    """Envoie l'embed sur Discord.

    1) Webhook direct si le secret DISCORD_WEBHOOK_URL est configure.
    2) Sinon, relais securise Base44 (fonction discordTradingSend) qui
       conserve le webhook hors du repo public.
    Renvoie (succes, info) - info decrit la cause exacte en cas d'echec.
    """
    payload = {"username": username, "embeds": [embed]}
    if webhook_url:
        try:
            r = requests.post(webhook_url, json=payload, timeout=15)
            if r.ok:
                return True, "ok (webhook direct)"
            info = f"Webhook direct HTTP {r.status_code}"
        except Exception as e:
            info = f"Webhook direct erreur reseau : {e}"
    else:
        info = "WEBHOOK ABSENT - le secret DISCORD_WEBHOOK_URL n'est pas configure"
    # repli sur le relais securise
    try:
        r = requests.post(RELAY_URL, json=payload, timeout=20)
        d = r.json()
        if r.ok and d.get("ok"):
            return True, "ok (relais securise)"
        return False, f"Relais a repondu : {d.get('error', r.status_code)} [apres: {info}]"
    except Exception as e:
        return False, f"Erreur reseau relais : {e} [apres: {info}]"



# ================== SIMULATION CHALLENGE FTMO (miroir du papier) ==================

def challenge_defaults(attempt=1, phase=1, history=None):
    return {
        "attempt": attempt,          # numero de la tentative en cours
        "phase": phase,              # 1 = challenge, 2 = verification
        "equity": CH_START,
        "day": datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d"),
        "day_start": CH_START,       # solde au debut du jour (regle 5% journalier)
        "trades": 0,                 # trades clotures de la tentative
        "days": [],                  # jours de trading (>=1 trade ouverts ce jour-la)
        "open": None,                # miroir de la position papier en cours
        "history": history or [],    # resultats des tentatives precedentes (conserves)
    }

def challenge_load():
    ch = load_json(CHALLENGE_FILE, None)
    if ch is None:
        ch = challenge_defaults()
    return ch

def challenge_rollover_day(ch):
    today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")
    if ch["day"] != today:
        ch["day"] = today
        ch["day_start"] = ch["equity"]

def challenge_on_open(ch, side, entry, sl_dist):
    """Le papier ouvre un trade -> le compte challenge en prend un aussi (risque 1%)."""
    ch["open"] = {"side": side, "entry": entry, "sl_dist": sl_dist,
                  "risk": round(ch["equity"] * CH_RISK, 2)}
    today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")
    if today not in ch["days"]:
        ch["days"].append(today)

def challenge_on_close(ch, exit_price):
    """Cloture miroir : meme prix de sortie, R identique, taille basee sur 10k a 1%.
    Retourne la liste des evenements FTMO (echec / phase validee)."""
    events = []
    o = ch.get("open")
    if not o:
        return ch, events
    d = 1 if o["side"] == "LONG" else -1
    r = d * (exit_price - o["entry"]) / o["sl_dist"] if o["sl_dist"] else 0.0
    pnl = round(o["risk"] * r, 2)
    ch["equity"] = round(ch["equity"] + pnl, 2)
    ch["trades"] += 1
    ch["open"] = None

    target = CH_P1_TARGET if ch["phase"] == 1 else CH_P2_TARGET
    if ch["equity"] <= CH_START * (1 - CH_MAX_DD):
        events.append(("FAIL_DD", ch["equity"]))
        ch["history"].append({"attempt": ch["attempt"], "phase": ch["phase"],
                              "result": "FAIL_DD", "trades": ch["trades"],
                              "days": len(ch["days"]), "equity": ch["equity"]})
        ch.update(challenge_defaults(attempt=ch["attempt"] + 1, history=ch["history"]))
    elif ch["equity"] - ch["day_start"] <= -CH_START * CH_DAILY_LOSS:
        events.append(("FAIL_DAILY", ch["equity"]))
        ch["history"].append({"attempt": ch["attempt"], "phase": ch["phase"],
                              "result": "FAIL_DAILY", "trades": ch["trades"],
                              "days": len(ch["days"]), "equity": ch["equity"]})
        ch.update(challenge_defaults(attempt=ch["attempt"] + 1, history=ch["history"]))
    elif ch["equity"] >= CH_START * target and len(ch["days"]) >= CH_MIN_DAYS:
        if ch["phase"] == 1:
            events.append(("PASS_P1", ch["equity"]))
            ch["history"].append({"attempt": ch["attempt"], "phase": 1,
                                  "result": "PASS_P1", "trades": ch["trades"],
                                  "days": len(ch["days"]), "equity": ch["equity"]})
            ch.update(challenge_defaults(attempt=ch["attempt"], phase=2, history=ch["history"]))
        else:
            events.append(("PASS_P2", ch["equity"]))
            ch["history"].append({"attempt": ch["attempt"], "phase": 2,
                                  "result": "PASS_P2", "trades": ch["trades"],
                                  "days": len(ch["days"]), "equity": ch["equity"]})
            ch.update(challenge_defaults(attempt=ch["attempt"] + 1, history=ch["history"]))
    return ch, events

def challenge_summary(ch):
    prog_pct = ch["equity"] / CH_START * 100 - 100
    tgt_pct = round((CH_P2_TARGET - 1) * 100) if ch["phase"] == 2 else round((CH_P1_TARGET - 1) * 100)
    return (f"**{ch['equity']:,.0f} EUR / 10 000** · tentative n°{ch['attempt']} "
            f"· phase {ch['phase']}/2 ({prog_pct:+.2f}% / objectif +{tgt_pct}%) · "
            f"jour {len(ch['days'])}/{CH_MIN_DAYS}+ · {ch['trades']} trade(s) · risque 1%").replace(",", " ")

def build_challenge_embed(event, equity):
    ev = event[0] if isinstance(event, tuple) else event
    if ev == "PASS_P2":
        return {"title": "🏆 SIMULATION FTMO RÉUSSIE — compte financé (simulé)",
                "description": "La stratégie a validé la phase 1 (+10%) **et** la phase 2 (+5%) "
                               "dans la simulation 10k à risque 1%. Prête pour un vrai challenge ! "
                               "Une nouvelle tentative démarre pour confirmer.",
                "color": 0xF1C40F}
    if ev == "PASS_P1":
        return {"title": "🎉 Simu FTMO — Phase 1 validée (+10%)",
                "description": "Objectif de la phase 1 atteint (min. 4 jours de trading respecté). "
                               "Phase 2 (vérification, +5%) démarre sur un nouveau compte 10k.",
                "color": 0x2ECC71}
    if ev == "FAIL_DAILY":
        return {"title": "❌ Simu FTMO échouée — perte journalière ≥ 5%",
                "description": f"La tentative aurait enfreint la règle de perte journalière "
                               f"(solde : {equity:,.0f} EUR). Nouvelle tentative relancée.".replace(",", " "),
                "color": 0xE74C3C}
    if ev == "FAIL_DD":
        return {"title": "❌ Simu FTMO échouée — drawdown ≥ 10%",
                "description": f"La tentative aurait enfreint la règle de drawdown total "
                               f"(solde : {equity:,.0f} EUR). Nouvelle tentative relancée.".replace(",", " "),
                "color": 0xE74C3C}
    return None

def build_embed(heure, result, state, ch):
    price = result["price"]
    pos = result.get("position")
    perf = result["equity"] - state["starting_equity"]
    perf_pct = round(perf / state["starting_equity"] * 100, 1)
    if pos:
        arrow = "📈 LONG" if pos["side"] == "LONG" else "📉 SHORT"
        pos_text = (
            f"{arrow} {pos['size']} oz @ {round(pos['entry'], 2)}\n"
            f"SL {pos['sl']} · TP {pos['tp']}\n"
            f"P&L flottant : {'+' if pos['floating_eur'] >= 0 else ''}{pos['floating_eur']} EUR"
        )
    else:
        pos_text = "Aucune position ouverte"
    action = result["action"]
    if action.startswith("CLOSE_TP"):
        emoji = "✅"
    elif action.startswith("CLOSE_SL"):
        emoji = "🛑"
    elif action.startswith("CLOSE_BE"):
        emoji = "🛡️"
    elif action.startswith("OPEN"):
        emoji = "🎯"
    else:
        emoji = "⏳"
    color = 0x2ECC71 if (action.startswith("CLOSE_TP") or action.startswith("OPEN")) else (
        0xE74C3C if action.startswith("CLOSE_SL") else (
            0x3498DB if action.startswith("CLOSE_BE") else 0x9B59B6
        )
    )
    sign = "+" if perf >= 0 else ""
    return {
        "title": f"{emoji} Cycle de {heure} — {result['detail']}",
        "color": color,
        "fields": [
            {
                "name": "💰 Capital",
                "value": f"**{round(result['equity'], 2)} EUR** "
                         f"({sign}{round(perf, 2)} EUR / {perf_pct}% depuis le début)",
                "inline": True,
            },
            {"name": "🥇 Or spot (PAXG)", "value": f"{round(price, 2)} $", "inline": True},
            {
                "name": "📊 Indicateurs",
                "value": f"RSI {result['rsi']} · EMA9 {result['ema9']} · "
                         f"EMA21 {result['ema21']} · ATR {result['atr']}",
                "inline": False,
            },
            {"name": "🧭 Tendances",
                "value": f"1h {'📈 haussière' if result.get('trend_1h', 0) > 0 else ('📉 baissière' if result.get('trend_1h', 0) < 0 else '~ neutre')} · "
                         f"5m {'📈 haussière' if result.get('trend_5m', 0) > 0 else ('📉 baissière' if result.get('trend_5m', 0) < 0 else '~ neutre')} · "
                         "entrée 1m seulement si alignées",
                "inline": False,
            },
            {"name": "📌 Position", "value": pos_text, "inline": False},
            {"name": "🔢 Trades", "value": f"{state['trade_count']} trade(s) clôturé(s)", "inline": True},
            {"name": "🏆 Simu FTMO 10k", "value": challenge_summary(ch), "inline": False},
        ],
        "footer": {"text": "Bot trading papier — CONFIRMATION 5m (2 clôtures) + biais 1h · risque 2% → objectif 2,5R · breakeven à +1R puis on laisse courir jusqu'au TP ou au stop · lun 01h–ven 20h (pause week-end) · IA apprend des trades"},
    }


def build_event_embed(result, state):
    """Message Discord dedie a chaque evenement de position (ouverture, SL, TP...)."""
    action = result["action"]
    heure = datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M")
    pos = result.get("position") or {}
    if action == "OPEN_LONG" or action == "OPEN_SHORT":
        long_ = action == "OPEN_LONG"
        return {
            "title": ("🟢 ACHAT (LONG)" if long_ else "🔴 VENTE (SHORT)")
                     + " — position ouverte",
            "color": 0x2ECC71 if long_ else 0xE74C3C,
            "fields": [
                {"name": "Entrée", "value": f"**{round(pos.get('entry', 0), 2)} $**", "inline": True},
                {"name": "Taille", "value": f"{pos.get('size', 0)} oz", "inline": True},
                {"name": "Risque", "value": f"{round(RISK_PCT * 100)} % du capital", "inline": True},
                {"name": "🛑 Stop Loss", "value": f"{pos.get('sl')} $", "inline": True},
                {"name": "🎯 Objectif (2,5R)", "value": f"{pos.get('tp')} $", "inline": True},
                {"name": "Signal", "value": state.get("open_reason") or "—", "inline": False},
            ],
            "footer": {"text": f"Ouvert à {heure} — suivi sur le panneau GitHub Pages"},
        }
    if action.startswith("CLOSE_"):
        reason = action[6:]
        pnl_txt = result["detail"]
        if reason == "TP":
            title, color = "🎯 TAKE PROFIT ATTEINT — trade clôturé", 0x2ECC71
        elif reason == "SL":
            title, color = "🛑 STOP LOSS TOUCHÉ — trade clôturé", 0xE74C3C
        elif reason == "WEEKEND":
            title, color = "🏠 Clôture week-end — marché de l'or fermé, position sécurisée", 0xE67E22
        elif reason == "BE":
            title, color = "🛡️ Break even touché — trade protégé, clôturé à l'équilibre", 0x3498DB
        else:
            title, color = "↩️ Position clôturée", 0x9B59B6
        return {
            "title": title,
            "color": color,
            "fields": [
                {"name": "Résultat", "value": f"**{pnl_txt}**", "inline": False},
                {"name": "💰 Capital", "value": f"**{round(result['equity'], 2)} EUR**", "inline": True},
                {"name": "🔢 Trades", "value": f"{state['trade_count']} clôturé(s)", "inline": True},
            ],
            "footer": {"text": f"Fermé à {heure} — le bot continue sa surveillance"},
        }
    return None


# ---------------------------------------------------------------------- main

def run_cycle(state, trades, learning, journal):
    """Un check complet : recuperation des bougies, indicateurs, decisions, sauvegarde."""
    try:
        candles = fetch_candles()
    except Exception as e:
        # panne reseau / API : erreur propre, pas de crash (le prochain run retentera)
        return {"action": "ERROR", "detail": f"Bougies indisponibles : {e}", "equity": state["equity"], "price": None}
    if not candles or len(candles) < 25:
        return {"action": "ERROR", "detail": "Pas assez de bougies", "equity": state["equity"], "price": None}
    eurusd = fetch_eurusd()
    live_price = fetch_ticker()          # prix temps reel pour entrees et SL/TP

    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    ema9 = ema_series(closes, 9)
    ema21 = ema_series(closes, 21)
    rsi = rsi14(closes)
    atr = atr14(highs, lows, closes)

    i = len(candles) - 1          # Coinbase ne renvoie que des bougies closes : la derniere est exploitable
    last = candles[i]
    price = live_price if live_price is not None else last["c"]

    # ---- tendances superieures (multi-fuseaux 1h + 5m) :
    # le biais 1h (EMA9/21) filtre les confirmations contre-tendance ; la 5m sert d'info.
    def htf_trend(gran):
        try:
            htf = fetch_candles(gran=gran)
            if not htf or len(htf) < 25:
                return 0
            cl = [x["c"] for x in htf]
            e9, e21 = ema_series(cl, 9), ema_series(cl, 21)
            return 1 if e9[-1] > e21[-1] else (-1 if e9[-1] < e21[-1] else 0)
        except Exception:
            return 0   # donnees indisponibles -> filtre neutralise ce cycle
    trend_5m = htf_trend(300)
    trend_1h = htf_trend(3600)

    # ---- detection de confirmation : le retournement doit etre PROUVE par
    # deux clôtures consecutives dans le nouveau sens avant d'entrer (vision d'Enzo)
    def detect_confirmation(cs):
        """(side, setup_t, swing) ou (None, 0, 0.0).
        LONG : chute >= 1.2xATR, puis 1re clôture haussiere, puis 2e qui confirme.
        SHORT : hausse >= 1.2xATR, puis 1re clôture baissiere, puis 2e qui confirme."""
        if len(cs) < 14:
            return None, 0, 0.0
        win = cs[-12:]
        price_now = price
        # ---------- setup LONG : sommet puis creux ----------
        hi_i = max(range(len(win)), key=lambda k: win[k]["h"])
        after_hi = win[hi_i + 1:]
        if len(after_hi) >= 2:
            lo_i = hi_i + 1 + min(range(len(after_hi)), key=lambda k: after_hi[k]["l"])
            hi, lo = win[hi_i]["h"], win[lo_i]["l"]
            if hi - lo >= DROP_MIN_ATR * atr:                      # vraie chute
                rest = win[lo_i:]        # la bougie du creux peut elle-meme etre la 1re confirmation
                if len(rest) >= 2:
                    first, cont = rest[-2], rest[-1]                # 1re conf + continuation (derniere close)
                    first_is_swing = rest[0] is first
                    if (
                        first["c"] > first["o"]                                 # clôture haussiere
                        and (first_is_swing or first["c"] > win[lo_i]["c"])      # redemarre au-dessus du creux
                        and cont["c"] > cont["o"] and cont["c"] >= first["c"]   # ca continue
                        and cont["l"] >= first["l"]                             # creux plus haut
                        and price_now - lo <= CONF_MAX_RUN_ATR * atr              # pas de chasse au prix
                    ):
                        return "LONG", cont["t"], lo
        # ---------- setup SHORT : creux puis sommet ----------
        lo2_i = min(range(len(win)), key=lambda k: win[k]["l"])
        after_lo = win[lo2_i + 1:]
        if len(after_lo) >= 2:
            hi2_i = lo2_i + 1 + max(range(len(after_lo)), key=lambda k: after_lo[k]["h"])
            lo2, hi2 = win[lo2_i]["l"], win[hi2_i]["h"]
            if hi2 - lo2 >= DROP_MIN_ATR * atr:                     # vraie hausse
                rest = win[hi2_i:]         # la bougie du sommet peut etre la 1re confirmation
                if len(rest) >= 2:
                    first, cont = rest[-2], rest[-1]
                    first_is_swing = rest[0] is first
                    if (
                        first["c"] < first["o"]                                  # clôture baissiere
                        and (first_is_swing or first["c"] < win[hi2_i]["c"])     # casse sous le sommet
                        and cont["c"] < cont["o"] and cont["c"] <= first["c"]   # ca continue
                        and cont["h"] <= first["h"]                             # sommet plus bas
                        and hi2 - price_now <= CONF_MAX_RUN_ATR * atr           # pas de chasse au prix
                    ):
                        return "SHORT", cont["t"], hi2
        return None, 0, 0.0
    conf_side, conf_t, conf_swing = detect_confirmation(candles)
    # calendrier eco auto (cache 1 h dans state) : label si une annonce bloque les entrees
    news_label = active_news_blackout(state)
    state["news_blackout_label"] = news_label
    # pause week-end (marche de l'or ferme) + contexte IA courant
    now_paris = datetime.now(ZoneInfo("Europe/Paris"))
    weekend_now = weekend_block(now_paris)
    ch = challenge_load()
    challenge_rollover_day(ch)
    state["weekend_active"] = weekend_now
    vol_pct = round(atr / price * 100, 3) if price else 0.0
    avoid_key = entry_blocked_by_learning(learning, now_paris.hour, vol_pct)

    result = {
        "last_candle_t": last["t"],
        "price": price,
        "rsi": round(rsi, 1),
        "trend_5m": trend_5m,
        "trend_1h": trend_1h,
        "atr": round(atr, 2),
        "weekend": weekend_now,
        "ema9": round(ema9[i], 2),
        "ema21": round(ema21[i], 2),
        "action": "NONE",
        "equity": state["equity"],
        "detail": "Aucun signal",
        "position": None,
    }

    if state["status"] != "RUNNING":
        result["detail"] = "Bot en pause"

    # ---- 1) gestion de la position ouverte
    if state["status"] == "RUNNING" and state["open_side"] in ("LONG", "SHORT"):
        d = 1 if state["open_side"] == "LONG" else -1
        floating_eur = (price - state["open_entry"]) * d * state["open_size"] / eurusd
        result["position"] = {
            "side": state["open_side"], "entry": state["open_entry"],
            "size": state["open_size"], "sl": state["open_sl"], "tp": state["open_tp"],
            "floating_eur": round(floating_eur, 2),
        }

        exit_price, close_reason = None, None
        is_long = state["open_side"] == "LONG"
        # ---- SORTIES SL / TP : le trade court jusqu'au TP ou au stop.
        # A +1R en faveur, le stop passe a l'entree (breakeven) pour proteger le trade,
        # puis ON NE LE TOUCHE PLUS — pas de trailing (demande Enzo 19/09).
        # Verifie sur TOUTES les bougies depuis le dernier check (remplissage retroactif).
        try:
            open_ms = datetime.fromisoformat(state["open_time"]).timestamp() * 1000
        except Exception:
            open_ms = 0
        since_ms = max(open_ms, state.get("last_check_ms", 0))
        missed = [x for x in candles if x["t"] > since_ms] or [candles[-1]]
        r_unit = abs(state["open_entry"] - (state.get("open_sl_init") or state["open_sl"]))
        if is_long:
            peak = max(state.get("trail_peak") or state["open_entry"], state["open_entry"])
            for cd in missed:
                # convention bougie : 1) SL (prix du debut de bougie), 2) TP
                if cd["l"] <= state["open_sl"]:
                    exit_price, close_reason = state["open_sl"], "SL"; break
                if cd["h"] >= state["open_tp"]:
                    exit_price, close_reason = state["open_tp"], "TP"; break
                peak = max(peak, cd["h"])
                state["trail_peak"] = peak
                # breakeven : a +1R en faveur, stop remis a l'entree — et c'est tout
                if r_unit > 0 and peak >= state["open_entry"] + BE_R_MULT * r_unit:
                    if state["open_sl"] < state["open_entry"]:
                        state["open_sl"] = state["open_entry"]
            if exit_price is None and live_price is not None:
                if live_price <= state["open_sl"]:
                    exit_price, close_reason = state["open_sl"], "SL"
                elif live_price >= state["open_tp"]:
                    exit_price, close_reason = state["open_tp"], "TP"
        else:
            trough = min(state.get("trail_trough") or state["open_entry"], state["open_entry"])
            for cd in missed:
                if cd["h"] >= state["open_sl"]:
                    exit_price, close_reason = state["open_sl"], "SL"; break
                if cd["l"] <= state["open_tp"]:
                    exit_price, close_reason = state["open_tp"], "TP"; break
                trough = min(trough, cd["l"])
                state["trail_trough"] = trough
                # breakeven : a +1R en faveur, stop remis a l'entree — et c'est tout
                if r_unit > 0 and trough <= state["open_entry"] - BE_R_MULT * r_unit:
                    if state["open_sl"] > state["open_entry"]:
                        state["open_sl"] = state["open_entry"]
            if exit_price is None and live_price is not None:
                if live_price >= state["open_sl"]:
                    exit_price, close_reason = state["open_sl"], "SL"
                elif live_price <= state["open_tp"]:
                    exit_price, close_reason = state["open_tp"], "TP"
        # week-end : marche de l'or ferme -> on securise la position avant la pause
        if exit_price is None and weekend_now:
            exit_price = round(live_price or price, 2)
            close_reason = "WEEKEND"

        # libelle honnete : SL initial jamais deplace = "SL", stop remis a l'entree = "BE"
        if close_reason == "SL":
            sl_init = state.get("open_sl_init")
            if sl_init is not None and state["open_sl"] != sl_init:
                close_reason = "BE"

        if exit_price is not None:
            pnl_usd = (exit_price - state["open_entry"]) * d * state["open_size"]
            pnl_eur = pnl_usd / eurusd
            new_equity = round(state["equity"] + pnl_eur, 2)
            for t in trades:
                if t["id"] == state["position_id"]:
                    t.update({
                        "status": "CLOSED",
                        "exit_price": round(exit_price, 2),
                        "close_reason": close_reason,
                        "closed_at": datetime.now(ZoneInfo("Europe/Paris")).isoformat(),
                        "pnl_eur": round(pnl_eur, 2),
                        "r": round(pnl_eur / t["risk_eur"], 2) if t.get("risk_eur") else None,
                        "t_out_ms": int(time.time() * 1000),
                    })
                    break
            state.update({
                "equity": new_equity,
                "trade_count": state["trade_count"] + 1,
                "open_side": "", "open_entry": None, "open_size": None,
                "open_sl": None, "open_tp": None, "position_id": None,
                "open_sl_init": None, "trail_peak": None, "trail_trough": None,
                "status": "RUNNING" if new_equity > PAUSE_THRESHOLD else "PAUSED",
            })
            result.update({
                "action": "CLOSE_" + close_reason,
                "equity": new_equity,
                "position": None,
                "detail": f"Trade fermé ({close_reason}) : "
                          f"{'+' if pnl_eur >= 0 else ''}{round(pnl_eur, 2)} EUR",
            })
            # simu FTMO : rejouer la cloture sur le compte challenge 10k
            ch, ch_events = challenge_on_close(ch, exit_price)
            if ch_events:
                result["challenge_events"] = ch_events
            # couche IA : analyser le trade ferme, ajuster les filtres d'entree
            ia_new = review_learning(state, trades, learning, journal)
            if ia_new:
                result["ia_entries"] = ia_new
        else:
            result["action"] = "HOLD"
            result["detail"] = "Position ouverte maintenue"

    # ---- 2) pas de position -> chercher une entree (confirmation requise, 24/7)
    elif state["status"] == "RUNNING":
        now_ms = datetime.now().timestamp() * 1000
        if weekend_now:
            result["detail"] = "🏠 Week-end — marché de l'or fermé, aucune prise de trade (reprise lundi 01h)"
        elif now_ms - last["t"] > 15 * 60 * 1000:   # PAXG a des periodes calmes : 15 min de tolerance
            result["detail"] = "Dernière bougie trop ancienne (marché fermé ?)"
        elif news_label:
            result["detail"] = f"Blackout news ({news_label}) — aucune nouvelle entrée"
        elif avoid_key:
            result["detail"] = f"🧠 IA : contexte évité ({avoid_key}) — pas d'entrée"
        else:
            side, reason, swing = None, "", 0.0
            if conf_t and conf_t <= state.get("last_flip_t", 0):
                side = None                       # setup deja joue (anti re-entree)
            elif conf_side == "LONG" and trend_1h >= 0:
                side, reason, swing = "LONG", (
                    f"Confirmation haussière : chute puis 2 clôtures 5m haussières, "
                    f"biais 1h {'↑' if trend_1h > 0 else '~'}"
                ), conf_swing
            elif conf_side == "SHORT" and trend_1h <= 0:
                side, reason, swing = "SHORT", (
                    f"Confirmation baissière : hausse puis 2 clôtures 5m baissières, "
                    f"biais 1h {'↓' if trend_1h < 0 else '~'}"
                ), conf_swing
            elif conf_side:
                result["detail"] = (
                    f"Confirmation {'haussière' if conf_side == 'LONG' else 'baissière'} "
                    "contre le biais 1h — filtrée"
                )

            if side:
                d = 1 if side == "LONG" else -1
                # SL structurel : sous le creux (LONG) / au-dessus du sommet (SHORT), + buffer
                sl = round(swing - d * SL_BUFFER_ATR * atr, 2)
                sl_dist = (price - sl) * d
                if sl_dist <= 0:
                    result["detail"] = "Structure du setup invalide (SL du mauvais côté) — ignorée"
                else:
                    tp = round(price + d * TP_R_MULT * sl_dist, 2)   # objectif 1 pour 2,5
                    risk_eur = state["equity"] * RISK_PCT
                    size_oz = max(0.01, (risk_eur * eurusd / sl_dist) // 0.01 / 100)
                    trade_id = uuid.uuid4().hex[:12]
                    state["last_flip_t"] = conf_t
                    trades.append({
                        "id": trade_id,
                        "side": side,
                        "entry_price": round(price, 2),
                        "size_oz": size_oz,
                        "sl": sl,
                        "tp": tp,
                        "status": "OPEN",
                        "reason_open": reason,
                        "opened_at": now_paris.isoformat(),
                        "hour_paris": now_paris.hour,
                        "vol_pct": vol_pct,
                        "rsi_in": round(rsi, 1),
                        "risk_eur": round(risk_eur, 2),
                    })
                    state.update({
                        "open_side": side, "open_entry": price, "open_size": size_oz,
                        "open_sl": sl, "open_sl_init": sl, "open_tp": tp, "open_reason": reason,
                        "position_id": trade_id,
                        "trail_peak": price if side == "LONG" else None,
                        "trail_trough": price if side == "SHORT" else None,
                        "open_time": datetime.now(ZoneInfo("Europe/Paris")).isoformat(),
                    })
                    challenge_on_open(ch, side, price, sl_dist)
                    result.update({
                        "action": "OPEN_" + side,
                        "detail": f"Trade ouvert : {side} {size_oz} oz @ {round(price,2)} | "
                                  f"SL {sl} / TP {tp} (risque {round(RISK_PCT*100)}% pour viser 2,5R) | {reason}",
                        "position": {"side": side, "entry": price, "size": size_oz,
                                     "sl": sl, "tp": tp, "floating_eur": 0.0},
                    })
            elif result["detail"] in ("Aucun signal",):
                result["detail"] = "Aucune confirmation — en surveillance"

    # ---- 3) sauvegarde de l'etat a chaque check
    state["last_price"] = round(price, 2)   # prix temps reel (ticker Coinbase)
    state["eurusd"] = round(eurusd, 4)
    state["last_cycle"] = datetime.now(ZoneInfo("Europe/Paris")).isoformat()
    save_json(STATE_FILE, state)
    save_json(TRADES_FILE, trades)
    save_json(CHALLENGE_FILE, ch)
    result["eurusd"] = round(eurusd, 4)
    return result


def main():
    state = load_json(STATE_FILE, None)
    if state is None:
        state = {
            "equity": STARTING_EQUITY,
            "starting_equity": STARTING_EQUITY,
            "status": "RUNNING",
            "trade_count": 0,
            "open_side": "",
            "open_entry": None,
            "open_size": None,
            "open_sl": None,
            "open_tp": None,
            "open_time": None,
            "open_reason": None,
            "position_id": None,
        }
    trades = load_json(TRADES_FILE, [])
    learning = load_json(LEARNING_FILE, {"avoid": {}, "strikes": {}})
    journal = load_json(JOURNAL_FILE, [])
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    prev_blackout = state.get("news_blackout_label")
    prev_weekend = state.get("weekend_active")

    # un seul check par run : run court (~30 s) que GitHub ne throttle pas
    state["run_count"] = state.get("run_count", 0) + 1
    today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")
    if state.get("dispatch_day") != today:
        state["dispatch_day"] = today
        state["dispatches_today"] = 0
    state["dispatches_today"] = state.get("dispatches_today", 0) + 1
    result = run_cycle(state, trades, learning, journal)
    # recharger la simu FTMO mise a jour par run_cycle
    challenge = challenge_load()
    heure = datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M:%S")
    # transitions pause/reprise week-end -> message Discord dedie
    cur_weekend = state.get("weekend_active")
    if cur_weekend and not prev_weekend:
        post_discord(webhook, {
            "title": "🏠 Pause week-end — marché de l'or fermé",
            "description": "Aucune nouvelle entrée jusqu'à lundi 01h (Paris). "
                           "Position éventuelle clôturée pour le week-end.",
            "color": 0xE67E22,
            "footer": {"text": "Le bot Bitcoin prend le relais 24/7"},
        }, username="Bot Or \U0001F9E1")
    elif prev_weekend and not cur_weekend:
        post_discord(webhook, {
            "title": "🔁 Reprise — le marché de l'or est ouvert",
            "description": "L'Asie a ouvert : le bot reprend les entrées confirmées.",
            "color": 0x2ECC71,
        }, username="Bot Or \U0001F9E1")
    # transition de blackout news -> message Discord dedie (debut ou fin de fenetre d'annonce)
    cur_blackout = state.get("news_blackout_label")
    if cur_blackout != prev_blackout and result.get("action") != "ERROR":
        if cur_blackout:
            post_discord(webhook, {
                "title": "🔕 Blackout news — entrées suspendues",
                "description": f"**{cur_blackout}**\nAucune nouvelle entrée jusqu'à la fin de la fenêtre. "
                               "Les positions ouvertes restent protégées (SL / break even / TP actifs).",
                "color": 0xE67E22,
                "footer": {"text": "Calendrier éco auto — ForexFactory"},
            }, username="Bot Or 📰")
        else:
            post_discord(webhook, {
                "title": "🔔 Fin du blackout news — entrées réactivées",
                "description": "Fenêtre d'annonce terminée — le bot reprend les entrées confirmées.",
                "color": 0x2ECC71,
                "footer": {"text": "Calendrier éco auto — ForexFactory"},
            }, username="Bot Or 📰")
    ok, info = False, "non envoyé"
    if result.get("action") == "ERROR":
        print(json.dumps({"run": state["run_count"], "error": result["detail"]}, ensure_ascii=False))
    else:
        # evenement d'ouverture/fermeture -> message Discord IMMEDIAT
        event = build_event_embed(result, state)
        if event:
            ok, info = post_discord(webhook, event)
        # simu FTMO : annoncer echec / phase validee
        for ev, eq in result.get("challenge_events", []):
            ch_emb = build_challenge_embed(ev, eq)
            if ch_emb:
                post_discord(webhook, ch_emb, username="Bot Or \U0001F3C6")
        # revue IA : le bot annonce ce qu'il a appris du trade ferme
        if result.get("ia_entries"):
            post_discord(webhook, build_ia_embed(result["ia_entries"], learning, state),
                         username="Bot Or \U0001F9E0")
        # compte-rendu complet calé sur le TEMPS REEL (>= 12 min depuis le precedent),
        # independant des runs manques/throttles
        now_ms = int(time.time() * 1000)
        if now_ms - state.get("last_report_ms", 0) >= 12 * 60 * 1000:
            ok2, info2 = post_discord(webhook, build_embed(heure, result, state, challenge))
            state["last_report_ms"] = now_ms
            ok, info = ok or ok2, info + " | cycle: " + ("OK" if ok2 else info2)
            if not ok2:
                print(f"ECHEC ENVOI DISCORD : {info2}")
        # memoriser le dernier check (SL/TP retroactif au prochain run)
        if result.get("last_candle_t"):
            state["last_check_ms"] = result["last_candle_t"]
        print(json.dumps({"run": state["run_count"], "heure": heure, **{
            k: result.get(k) for k in ("action", "detail", "equity", "price")}}, ensure_ascii=False))

    save_json(STATE_FILE, state)
    save_json(TRADES_FILE, trades)
    save_json(LEARNING_FILE, learning)
    save_json(JOURNAL_FILE, journal)
    save_json(CHALLENGE_FILE, challenge)


if __name__ == "__main__":
    main()
