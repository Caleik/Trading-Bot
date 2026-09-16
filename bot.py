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
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

STATE_FILE = "state.json"
TRADES_FILE = "trades.json"
RISK_PCT = 0.02          # risque par trade : 2% du capital
# ---- MODE SCALPING : bougies 1 minute, signaux tres frequents ----
TIMEFRAME = "1m"         # bougies 1 minute (scalping)
SL_ATR = 1.0             # stop loss = 1 x ATR (resserre pour le scalping)
TP_ATR = 1.5             # take profit = 1.5 x ATR
MAX_HOLD_MIN = 120       # sortie forcee apres 2 h : du vrai scalping, jamais de position qui traine
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

def fetch_candles(symbol="PAXG-USD", interval=TIMEFRAME, range_="2d"):
    """Bougies PAXG-USD (or spot, proxy du XAU/USD a +/-0.3%) via Coinbase.
    Donnees temps reel 24/7 — contrairement a Yahoo (retard ~10 min)."""
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
    elif action.startswith("CLOSE_TIMEOUT"):
        emoji = "⌛"
    elif action.startswith("OPEN"):
        emoji = "🎯"
    else:
        emoji = "⏳"
    color = 0x2ECC71 if (action.startswith("CLOSE_TP") or action.startswith("OPEN")) else (
        0xE74C3C if action.startswith("CLOSE_SL") else (
            0xE67E22 if action.startswith("CLOSE_TIMEOUT") else 0x9B59B6
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
            {"name": "📌 Position", "value": pos_text, "inline": False},
            {"name": "🔢 Trades", "value": f"{state['trade_count']} trade(s) clôturé(s)", "inline": True},
        ],
        "footer": {"text": "Bot trading papier — SCALPING XAU/USD 1 min, SL 1xATR / TP 1.5xATR, sortie max 2 h, risque 2% (GitHub Actions)"},
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
                {"name": "🎯 Take Profit", "value": f"{pos.get('tp')} $", "inline": True},
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
        elif reason == "TIMEOUT":
            title, color = "⌛ Durée max atteinte (2 h) — position clôturée", 0xE67E22
        else:
            title, color = "↩️ Signal inversé — position clôturée", 0x9B59B6
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

    # croisement RECENT dans les FLIP_WINDOW dernieres bougies : les runs GitHub
    # espaces de ~5 min peuvent rater le flip exact -> on cherche le flip recemment survenu
    FLIP_WINDOW = 8
    cross_up = cross_down = False
    flip_t = 0
    for j in range(max(1, i - FLIP_WINDOW), i + 1):
        if ema9[j - 1] <= ema21[j - 1] and ema9[j] > ema21[j]:
            cross_up, flip_t = True, candles[j]["t"]
            break
    if not cross_up:
        for j in range(max(1, i - FLIP_WINDOW), i + 1):
            if ema9[j - 1] >= ema21[j - 1] and ema9[j] < ema21[j]:
                cross_down, flip_t = True, candles[j]["t"]
                break
    # le sens doit etre toujours valable sur la derniere bougie
    cross_up = cross_up and ema9[i] > ema21[i]
    cross_down = cross_down and ema9[i] < ema21[i]

    result = {
        "last_candle_t": last["t"],
        "price": price,
        "rsi": round(rsi, 1),
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
        # SL/TP verifies sur TOUTES les bougies depuis l'ouverture de la position ou le
        # dernier check (remplit retroactivement -> les trous de surveillance ne faussent pas la simu)
        try:
            open_ms = datetime.fromisoformat(state["open_time"]).timestamp() * 1000
        except Exception:
            open_ms = 0
        since_ms = max(open_ms, state.get("last_check_ms", 0))
        missed = [x for x in candles if x["t"] > since_ms] or [candles[-1]]
        for c in missed:
            if state["open_side"] == "LONG":
                if c["l"] <= state["open_sl"]:
                    exit_price, close_reason = state["open_sl"], "SL"; break
                if c["h"] >= state["open_tp"]:
                    exit_price, close_reason = state["open_tp"], "TP"; break
            else:
                if c["h"] >= state["open_sl"]:
                    exit_price, close_reason = state["open_sl"], "SL"; break
                if c["l"] <= state["open_tp"]:
                    exit_price, close_reason = state["open_tp"], "TP"; break
        if exit_price is None and live_price is not None:  # bougie en cours (prix temps reel)
            if state["open_side"] == "LONG" and live_price <= state["open_sl"]:
                exit_price, close_reason = state["open_sl"], "SL"
            elif state["open_side"] == "LONG" and live_price >= state["open_tp"]:
                exit_price, close_reason = state["open_tp"], "TP"
            elif state["open_side"] == "SHORT" and live_price >= state["open_sl"]:
                exit_price, close_reason = state["open_sl"], "SL"
            elif state["open_side"] == "SHORT" and live_price <= state["open_tp"]:
                exit_price, close_reason = state["open_tp"], "TP"
        # ---- sortie forcee : position ouverte depuis trop longtemps -> on ferme au prix
        if exit_price is None:
            try:
                opened_ms = datetime.fromisoformat(state["open_time"]).timestamp() * 1000
            except Exception:
                opened_ms = 0
            if opened_ms and time.time() * 1000 - opened_ms > MAX_HOLD_MIN * 60 * 1000:
                exit_price, close_reason = price, "TIMEOUT"

        if exit_price is None and (
            (state["open_side"] == "LONG" and cross_down)
            or (state["open_side"] == "SHORT" and cross_up)
        ):
            exit_price, close_reason = price, "SIGNAL_INVERSE"

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

    # ---- 2) pas de position -> chercher une entrée
    elif state["status"] == "RUNNING":
        now_ms = datetime.now().timestamp() * 1000
        if now_ms - last["t"] > 15 * 60 * 1000:   # PAXG a des periodes calmes : 15 min de tolerance
            result["detail"] = "Dernière bougie trop ancienne (marché fermé ?)"
        else:
            side, reason = None, ""
            if flip_t and flip_t <= state.get("last_flip_t", 0):
                side = None  # flip deja joue (anti re-entree)
            elif cross_up and 45 < rsi < 75:
                side, reason = "LONG", f"EMA9 > EMA21 (croisement haussier), RSI {round(rsi)}"
            elif cross_down and 25 < rsi < 55:
                side, reason = "SHORT", f"EMA9 < EMA21 (croisement baissier), RSI {round(rsi)}"

            if side:
                d = 1 if side == "LONG" else -1
                sl_dist, tp_dist = SL_ATR * atr, TP_ATR * atr
                risk_eur = state["equity"] * RISK_PCT
                size_oz = max(0.01, (risk_eur * eurusd / sl_dist) // 0.01 / 100)
                sl = round(price - d * sl_dist, 2)
                tp = round(price + d * tp_dist, 2)
                trade_id = uuid.uuid4().hex[:12]
                state["last_flip_t"] = flip_t
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
                    "open_sl": sl, "open_tp": tp, "open_reason": reason,
                    "position_id": trade_id,
                    "open_time": datetime.now(ZoneInfo("Europe/Paris")).isoformat(),
                })
                result.update({
                    "action": "OPEN_" + side,
                    "detail": f"Trade ouvert : {side} {size_oz} oz @ {round(price,2)} | "
                              f"SL {sl} / TP {tp} | {reason}",
                    "position": {"side": side, "entry": price, "size": size_oz,
                                 "sl": sl, "tp": tp, "floating_eur": 0.0},
                })
            else:
                result["detail"] = (
                    "Croisement mais RSI non confirmé" if (cross_up or cross_down)
                    else "Aucun croisement, en surveillance"
                )

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

    # un seul check par run : run court (~30 s) que GitHub ne throttle pas
    state["run_count"] = state.get("run_count", 0) + 1
    today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")
    if state.get("dispatch_day") != today:
        state["dispatch_day"] = today
        state["dispatches_today"] = 0
    state["dispatches_today"] = state.get("dispatches_today", 0) + 1
    result = run_cycle(state, trades)
    heure = datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M:%S")
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
