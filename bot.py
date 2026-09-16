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
# ---- STRATEGIE CONFIRMATION + RATIO 1:2,5 (vision d'Enzo) ----
# On n'anticipe plus le retournement : apres une chute (resp. hausse), on attend
# la 1re bougie 5m qui CLOTURE dans le sens inverse, puis une 2e bougie qui confirme.
# Entree seulement apres cette double confirmation, dans le sens du biais 1h — 24/7.
# Gestion : risque 2% par trade, objectif fixe a 2,5 x le risque (1 pour 2,5),
# breakeven a +1R, puis trailing 2xATR qui ne recule jamais.
TIMEFRAME = "5m"         # bougies 5 minutes (base de la confirmation)
TRAIL_ATR = 2.0          # apres +1R : le stop suit a 2 x ATR(5m) derriere l'extremum
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



def build_embed(heure, result, state):
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
    elif action.startswith("CLOSE_TRAIL"):
        emoji = "🔒"
    elif action.startswith("OPEN"):
        emoji = "🎯"
    else:
        emoji = "⏳"
    color = 0x2ECC71 if (action.startswith("CLOSE_TP") or action.startswith("OPEN")) else (
        0xE74C3C if action.startswith("CLOSE_SL") else (
            0x3498DB if action.startswith("CLOSE_TRAIL") else 0x9B59B6
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
        ],
        "footer": {"text": "Bot trading papier — CONFIRMATION 5m (2 clôtures) + biais 1h · risque 2% → objectif 2,5R · breakeven à +1R puis trailing 2xATR · 24/7"},
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
        elif reason == "TRAIL":
            title, color = "🔒 Sortie trailing (stop protégé) — position clôturée", 0x3498DB
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

def run_cycle(state, trades):
    """Un check complet : recuperation des bougies, indicateurs, decisions, sauvegarde."""
    candles = fetch_candles()
    if len(candles) < 25:
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
            if len(htf) < 25:
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

    result = {
        "last_candle_t": last["t"],
        "price": price,
        "rsi": round(rsi, 1),
        "trend_5m": trend_5m,
        "trend_1h": trend_1h,
        "atr": round(atr, 2),
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
        # ---- TRAILING STOP 2xATR : suit le prix, ne recule jamais.
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
                # convention bougie : 1) SL (prix du debut de bougie), 2) TP, 3) le trail
                # ne se serre qu'APRES la bougie favorable -> pas d'optimisme intrabare
                if cd["l"] <= state["open_sl"]:
                    exit_price, close_reason = state["open_sl"], "TRAIL"; break
                if cd["h"] >= state["open_tp"]:
                    exit_price, close_reason = state["open_tp"], "TP"; break
                peak = max(peak, cd["h"])                      # le trail suit les plus hauts
                state["trail_peak"] = peak
                # breakeven puis trailing, seulement apres +1R en faveur
                if r_unit > 0 and peak >= state["open_entry"] + BE_R_MULT * r_unit:
                    cand = max(state["open_entry"], peak - TRAIL_ATR * atr)
                    state["open_sl"] = max(state["open_sl"], round(cand, 2))
            if exit_price is None and live_price is not None:
                if live_price <= state["open_sl"]:
                    exit_price, close_reason = state["open_sl"], "TRAIL"
                elif live_price >= state["open_tp"]:
                    exit_price, close_reason = state["open_tp"], "TP"
        else:
            trough = min(state.get("trail_trough") or state["open_entry"], state["open_entry"])
            for cd in missed:
                if cd["h"] >= state["open_sl"]:
                    exit_price, close_reason = state["open_sl"], "TRAIL"; break
                if cd["l"] <= state["open_tp"]:
                    exit_price, close_reason = state["open_tp"], "TP"; break
                trough = min(trough, cd["l"])                  # le trail suit les plus bas
                state["trail_trough"] = trough
                if r_unit > 0 and trough <= state["open_entry"] - BE_R_MULT * r_unit:
                    cand = min(state["open_entry"], trough + TRAIL_ATR * atr)
                    state["open_sl"] = min(state["open_sl"], round(cand, 2))
            if exit_price is None and live_price is not None:
                if live_price >= state["open_sl"]:
                    exit_price, close_reason = state["open_sl"], "TRAIL"
                elif live_price <= state["open_tp"]:
                    exit_price, close_reason = state["open_tp"], "TP"
        # libelle honnete : SL initial jamais deplace = "SL", trail serre = "TRAIL"
        if close_reason == "TRAIL":
            if is_long and state["open_sl"] <= (state.get("open_sl_init") or state["open_sl"]):
                close_reason = "SL"
            elif not is_long and state["open_sl"] >= (state.get("open_sl_init") or state["open_sl"]):
                close_reason = "SL"

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
        else:
            result["action"] = "HOLD"
            result["detail"] = "Position ouverte maintenue"

    # ---- 2) pas de position -> chercher une entree (confirmation requise, 24/7)
    elif state["status"] == "RUNNING":
        now_ms = datetime.now().timestamp() * 1000
        if now_ms - last["t"] > 15 * 60 * 1000:   # PAXG a des periodes calmes : 15 min de tolerance
            result["detail"] = "Dernière bougie trop ancienne (marché fermé ?)"
        elif news_label:
            result["detail"] = f"Blackout news ({news_label}) — aucune nouvelle entrée"
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
                        "opened_at": datetime.now(ZoneInfo("Europe/Paris")).isoformat(),
                    })
                    state.update({
                        "open_side": side, "open_entry": price, "open_size": size_oz,
                        "open_sl": sl, "open_sl_init": sl, "open_tp": tp, "open_reason": reason,
                        "position_id": trade_id,
                        "trail_peak": price if side == "LONG" else None,
                        "trail_trough": price if side == "SHORT" else None,
                        "open_time": datetime.now(ZoneInfo("Europe/Paris")).isoformat(),
                    })
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
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    prev_blackout = state.get("news_blackout_label")

    # un seul check par run : run court (~30 s) que GitHub ne throttle pas
    state["run_count"] = state.get("run_count", 0) + 1
    today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")
    if state.get("dispatch_day") != today:
        state["dispatch_day"] = today
        state["dispatches_today"] = 0
    state["dispatches_today"] = state.get("dispatches_today", 0) + 1
    result = run_cycle(state, trades)
    heure = datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M:%S")
    # transition de blackout news -> message Discord dedie (debut ou fin de fenetre d'annonce)
    cur_blackout = state.get("news_blackout_label")
    if cur_blackout != prev_blackout and result.get("action") != "ERROR":
        if cur_blackout:
            post_discord(webhook, {
                "title": "🔕 Blackout news — entrées suspendues",
                "description": f"**{cur_blackout}**\nAucune nouvelle entrée jusqu'à la fin de la fenêtre. "
                               "Les positions ouvertes restent protégées (SL / trailing / TP actifs).",
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
        # compte-rendu complet calé sur le TEMPS REEL (>= 12 min depuis le precedent),
        # independant des runs manques/throttles
        now_ms = int(time.time() * 1000)
        if now_ms - state.get("last_report_ms", 0) >= 12 * 60 * 1000:
            ok2, info2 = post_discord(webhook, build_embed(heure, result, state))
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


if __name__ == "__main__":
    main()
