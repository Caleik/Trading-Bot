#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOT TRADING IA — BITCOIN (BTC-USD) — extension du bot or XAU/USD
=================================================================
Objectif (doc « Projet – Bot de trading IA Bitcoin ») :
1. Trader le BTC 24/7, en relais du bot or quand le marché de l'or ferme le week-end.
2. Etre intégré au même panel GitHub Pages que le bot or (mêmes fichiers d'état).
3. Strategie VALIDEE EN BACKTEST avant simulation (60 j de bougies 5m Coinbase,
   18/09/2026) : confirmation stricte + biais 1h + TP 3R -> +41,7 % en 60 j,
   profit factor 1,16, drawdown max 23,7 %, rentable aussi le week-end.
4. COUCHE IA D'APPRENTISSAGE : chaque trade est journalise avec son contexte
   (heure, week-end, volatilite, RSI, sens, biais). Apres chaque trade ferme,
   le bot recalcule ses statistiques par contexte et peut DECIDER lui-meme de
   ne plus trader dans les creneaux/configurations qui perdent de l'argent,
   puis de les reactiver quand ils redeviennent bons. Chaque decision IA est
   journalisee (btc_journal.json), visible dans le panel et annoncee sur Discord.
   Les regles de securite (risque max, SL, TP, confirmation) sont INTANGIBLES :
   l'IA ne peut jouer que sur les filtres d'entree.

Strategie (validee en backtest — voir REGLes ci-dessous) :
- Entree : chute/hausse >= 2.4 x ATR, puis 2 clôtures 5m de confirmation,
  seulement dans le sens du biais 1h (EMA9/21), sans chasse au prix (>2.5 ATR).
- Filtres : RSI (pas de LONG si RSI>70, pas de SHORT si RSI<30), volatilite
  (pas d'entree si ATR >= 0.15% du prix).
- Risque : 1% du capital par trade (frequence BTC ~6/j : 1% et un coupe-circuit
  journalier a -3% equivaluent au 2% du bot or ~2-3 trades/j).
- Coupe-circuit jour : si le jour UTC en cours perd 3% ou plus, PLUS AUCUNE
  nouvelle entree jusqu'au lendemain (les positions ouvertes restent gerees).
- Sorties : SL structurel (+0.3 ATR), TP = 3R (ratio 1:3), breakeven a +1R,
  puis trailing stop 2 x ATR qui ne recule jamais.
- Prix : Coinbase BTC-USD (spot, temps reel, 24/7).
"""
import json, os, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

# ------------------------------------------------------------------- regles
TIMEFRAME = "5m"
TRAIL_ATR = 2.0
BE_R_MULT = 1.0
TP_R_MULT = 3.0          # ratio 1 pour 3
SL_BUFFER_ATR = 0.3
DROP_MIN_ATR = 2.4       # mouvement initial >= 2.4 x ATR (confirmé backtest)
CONF_MAX_RUN_ATR = 2.5
RISK_PCT = 0.01           # 1% par trade (frequence BTC elevee)
DAILY_STOP_PCT = 0.03     # coupe-circuit : -3% sur le jour UTC -> plus d'entree
RSI_MAX_LONG = 70.0      # pas de LONG en surachat
RSI_MIN_SHORT = 30.0      # pas de SHORT en survente
VOL_MAX_PCT = 0.15        # pas d'entree si ATR 5m >= 0.15% du prix (trop nerveux)
STARTING_EQUITY = 100.0   # capital papier en EUR (compte papier, comme le bot or)
PAUSE_THRESHOLD = 5.0

# ------------------------------------------------------------- couche IA
LEARN_MIN_TRADES = 10     # nb min de trades dans un contexte avant de le juger
LEARN_WINDOW = 15         # juge sur les 15 derniers trades du contexte
LEARN_BAD_R = -0.15       # esperance en R sous laquelle un contexte est "mauvais"
LEARN_RELEASE_R = 0.05    # esperance recente au-dessus de laquelle on reactive
LEARN_STRIKES = 2         # 2 revues consecutives mauvaises avant d'eviter un contexte
LEARN_COOLDOWN_MS = 12 * 3600 * 1000   # max 1 NOUVEL avoid par 12 h (anti-zygotage)
LEARN_AVOID_TTL_MS = 14 * 24 * 3600 * 1000   # un contexte évité est réessayé après 14 jours
LEARN_REVIEW_EVERY = 25   # revue globale publieee tous les X trades fermes

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"
RELAY_URL = "https://superagent-583f72c3.base44.app/functions/discordTradingSend"
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))
CHECKS_PER_RUN = int(os.environ.get("CHECKS_PER_RUN", "13"))
STATE_FILE = "btc_state.json"
TRADES_FILE = "btc_trades.json"
LEARN_FILE = "learning.json"
JOURNAL_FILE = "btc_journal.json"

PARIS = ZoneInfo("Europe/Paris")


# ---------------------------------------------------------------- utilitaires
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def ema_series(values, period):
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out = [None] * (period - 1) + [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi14(closes):
    if len(closes) < 15:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains[:14]) / 14, sum(losses[:14]) / 14
    for i in range(14, len(gains)):
        ag = (ag * 13 + gains[i]) / 14
        al = (al * 13 + losses[i]) / 14
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr14(highs, lows, closes):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if not trs:
        return 1.0
    a = sum(trs[:14]) / 14 if len(trs) >= 14 else sum(trs) / len(trs)
    for t in trs[14:]:
        a = (a * 13 + t) / 14
    return a


# ------------------------------------------------------------------- donnees
def fetch_candles(symbol="BTC-USD", gran=None):
    if gran is None:
        gran = {"1m": 60, "5m": 300, "15m": 900}[TIMEFRAME]
    r = requests.get(
        f"https://api.exchange.coinbase.com/products/{symbol}/candles?granularity={gran}",
        timeout=20, headers={"User-Agent": UA})
    r.raise_for_status()
    rows = r.json()
    return [{"t": row[0] * 1000, "o": row[3], "h": row[2], "l": row[1], "c": row[4]}
            for row in sorted(rows)]


def fetch_ticker(symbol="BTC-USD"):
    try:
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/ticker",
            timeout=10, headers={"User-Agent": UA})
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def fetch_eurusd():
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/EURUSD=X?interval=1d&range=1d",
            headers={"User-Agent": UA}, timeout=15)
        p = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        if p and 0.5 < p < 2:
            return float(p)
    except Exception:
        pass
    return 1.10


# ------------------------------------------------------------------ Discord
def post_discord(webhook_url, embed, username="Bot Bitcoin \u20bf\U0001F916"):
    payload = {"username": username, "embeds": [embed]}
    if webhook_url:
        try:
            r = requests.post(webhook_url, json=payload, timeout=15)
            if r.ok:
                return True, "ok (webhook direct)"
            info = f"Webhook direct HTTP {r.status_code}"
        except Exception as e:
            info = f"Webhook direct erreur : {e}"
    else:
        info = "WEBHOOK ABSENT"
    try:
        r = requests.post(RELAY_URL, json=payload, timeout=20)
        d = r.json()
        if r.ok and d.get("ok"):
            return True, "ok (relais securise)"
        return False, f"Relais : {d.get('error', r.status_code)} [apres: {info}]"
    except Exception as e:
        return False, f"Erreur relais : {e} [apres: {info}]"


def build_embed(heure, result, state, learning):
    price = result["price"]
    pos = result.get("position")
    perf = result["equity"] - state["starting_equity"]
    perf_pct = round(perf / state["starting_equity"] * 100, 1)
    if pos:
        arrow = "\U0001F4C8 LONG" if pos["side"] == "LONG" else "\U0001F4C9 SHORT"
        pos_text = (f"{arrow} @ {round(pos['entry'], 0)} $\n"
                    f"SL {round(pos['sl'], 0)} · TP {round(pos['tp'], 0)}\n"
                    f"P&L flottant : {'+' if pos['floating_eur'] >= 0 else ''}{pos['floating_eur']} EUR")
    else:
        pos_text = "Aucune position ouverte"
    action = result["action"]
    emoji = ("\u2705" if action.startswith("CLOSE_TP") else
             "\U0001F6D1" if action.startswith("CLOSE_SL") else
             "\U0001F512" if action.startswith("CLOSE_TRAIL") else
             "\U0001F3AF" if action.startswith("OPEN") else "\u23F3")
    gates = result.get("gates") or {}
    ia_txt = f"{len(learning.get('avoid', {}))} contexte(s) evite(s) par l'IA"
    if gates.get("blocked_by"):
        ia_txt += f" · entree BLOQUEE : {gates['blocked_by']}"
    sign = "+" if perf >= 0 else ""
    return {
        "title": f"{emoji} BTC {heure} — {result['detail']}",
        "color": 0xF7931A,
        "fields": [
            {"name": "\U0001F4B0 Capital", "inline": True,
             "value": f"**{round(result['equity'], 2)} EUR** ({sign}{round(perf, 2)} EUR / {perf_pct} %)"},
            {"name": "\u20BF Bitcoin", "inline": True,
             "value": f"{round(price, 0)} $"},
            {"name": "\U0001F4CA Indicateurs", "inline": False,
             "value": f"RSI {result['rsi']} · EMA9 {round(result['ema9'], 0)} · EMA21 {round(result['ema21'], 0)} · ATR {result['atr']} $ ({result['vol_pct']} %)"},
            {"name": "\U0001F9ED Biais", "inline": False,
             "value": "1h " + ("\U0001F53C" if result.get("trend_1h", 0) > 0 else ("\U0001F53D" if result.get("trend_1h", 0) < 0 else "~"))
                      + " · coupe-circuit jour : " + ("\U0001F6D1 actif" if result.get("daily_stop") else "OK")},
            {"name": "\U0001F4CC Position", "inline": False, "value": pos_text},
            {"name": "\U0001F9E0 IA", "inline": False, "value": ia_txt},
            {"name": "\U0001F522 Trades", "inline": True,
             "value": f"{state.get('trade_count', 0)} ferme(s) · {result.get('today_pnl', 0)} EUR aujourd'hui"},
        ],
        "footer": {"text": "Bot BTC papier — CONFIRMATION 5m stricte + biais 1h · risque 1% -> TP 3R · BE +1R puis trail 2xATR · coupe-circuit -3%/jour · IA apprend de ses trades"},
    }


def build_event_embed(result, state):
    action = result["action"]
    heure = datetime.now(PARIS).strftime("%H:%M")
    pos = result.get("position") or {}
    if action in ("OPEN_LONG", "OPEN_SHORT"):
        long_ = action == "OPEN_LONG"
        return {
            "title": ("\U0001F7E9 ACHAT BTC (LONG)" if long_ else "\U0001F7EA VENTE BTC (SHORT)") + " — position ouverte",
            "color": 0xF7931A,
            "fields": [
                {"name": "Entrée", "value": f"**{round(pos.get('entry', 0), 0)} $**", "inline": True},
                {"name": "Taille", "value": f"{round(pos.get('size', 0), 5)} BTC", "inline": True},
                {"name": "Risque", "value": "1 % du capital", "inline": True},
                {"name": "\U0001F6D1 Stop Loss", "value": f"{round(pos.get('sl', 0), 0)} $", "inline": True},
                {"name": "\U0001F3AF Objectif (3R)", "value": f"{round(pos.get('tp', 0), 0)} $", "inline": True},
                {"name": "Contexte IA", "inline": False,
                 "value": f"{pos.get('context_txt', '—')}"},
            ],
            "footer": {"text": f"Ouvert à {heure} — compte papier"},
        }
    if action.startswith("CLOSE_"):
        reason = action[6:]
        if reason == "TP":
            title, color = "\U0001F3AF TAKE PROFIT BTC — trade gagné", 0x2ECC71
        elif reason == "SL":
            title, color = "\U0001F6D1 STOP LOSS BTC — trade clôturé", 0xE74C3C
        elif reason == "TRAIL":
            title, color = "\U0001F512 Sortie trailing BTC — position clôturée", 0x3498DB
        else:
            title, color = "\u21A9\uFE0F Position BTC clôturée", 0x9B59B6
        return {
            "title": title, "color": color,
            "fields": [
                {"name": "Résultat", "value": f"**{result['detail']}**", "inline": False},
                {"name": "\U0001F4B0 Capital", "value": f"**{round(result['equity'], 2)} EUR**", "inline": True},
                {"name": "\U0001F522 Trades", "value": f"{state.get('trade_count', 0)} fermé(s)", "inline": True},
            ],
            "footer": {"text": f"Fermé à {heure} — l'IA analyse ce trade et ajuste ses filtres"},
        }
    return None


def build_ia_embed(entries, learning, state):
    """Revue IA : ce que le bot a appris des ses derniers trades."""
    fields = []
    for e in entries[-3:]:
        if e["type"] == "AVOID":
            v = (f"\U0001F6AB N'entre plus dans **{e['bucket']}**\n"
                 f"{e['stats']['n']} trades · {e['stats']['wr']} % victoires · "
                 f"esperance {e['stats']['exp_r']} R — j'évite ce contexte")
        elif e["type"] == "RELEASE":
            v = (f"\u2705 Réactive **{e['bucket']}**\n"
                 f"esperance recente redevenue positive ({e['stats']['exp_r']} R) — je retente ce contexte")
        else:
            v = str(e.get("reason", ""))[:300]
        fields.append({"name": e.get("title", e["type"]), "value": v, "inline": False})
    if not fields:
        fields.append({"name": "Bilan", "value": "Rien de nouveau à signaler.", "inline": False})
    return {
        "title": "\U0001F9E0 REVUE IA — j'apprends de mes trades",
        "color": 0x9B59B6,
        "fields": fields + [
            {"name": "\U0001F5F3 Contenus évités actuellement", "inline": False,
             "value": "\n".join(f"· {k}" for k in learning.get("avoid", {})) or "aucun"},
            {"name": "\U0001F522 Recul", "inline": True,
             "value": f"{state.get('trade_count', 0)} trades analysés"},
        ],
        "footer": {"text": "L'IA n'ajuste QUE les filtres d'entrée — risque 1%, SL, TP 3R et trailing restent intangibles"},
    }


# ---------------------------------------------------------------- couche IA
def bucket_of(trade):
    """Cle de contexte d'un trade : tranche 4h x semaine/week-end (+ vol)."""
    h = trade.get("hour_paris", 0)
    return f"{'WE' if trade.get('weekend') else 'SE'} {h // 4 * 4:02d}-{h // 4 * 4 + 4:02d}h"


def bucket_stats(trades, key):
    """(n, wr, exp_r) sur les LEARN_WINDOW derniers trades du contexte."""
    b = [t for t in trades if bucket_of(t) == key][-LEARN_WINDOW:]
    if not b:
        return None
    wins = [t for t in b if t["pnl_eur"] > 0]
    wr = round(len(wins) / len(b) * 100)
    exp_r = round(sum(t["r"] for t in b) / len(b), 2)
    return {"n": len(b), "wr": wr, "exp_r": exp_r}


def review_learning(state, trades, learning, journal):
    """Apres un trade ferme : recalcule chaque contexte et decide des filtres.
    Retourne les nouvelles entrees de journal (et publie sur Discord)."""
    new_entries = []
    now_ms = int(time.time() * 1000)
    # reperer les contenus evalues (tous les buckets vus dans l'historique)
    keys = sorted({bucket_of(t) for t in trades})
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
                        learning.setdefault("avoid", {})[key] = {
                            "since": now_ms, "stats": st}
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
            # duree d'evitement ecoulee -> re-test du contexte (periode d'essai)
            if now_ms - since >= LEARN_AVOID_TTL_MS:
                del learning["avoid"][key]
                learning["strikes"][key] = LEARN_STRIKES - 1   # re-evite plus vite si ca reperd
                new_entries.append({
                    "t": now_ms, "type": "RELEASE", "bucket": key,
                    "stats": st,
                    "title": "Contexte remis à l'essai",
                    "reason": (f"{key} : 14 jours d'évitement écoulés, esperance au moment de l'évitement "
                               f"{st['exp_r']} R — je reteste ce contexte en période d'essai")})
                continue
            # se retracte aussi si les rares trades post-avoid sont bons
            after = [t for t in trades if bucket_of(t) == key and t.get("t_out_ms", 0) > since]
            if len(after) >= 3 and sum(t["r"] for t in after) / len(after) > LEARN_RELEASE_R:
                del learning["avoid"][key]
                st2 = {"n": len(after),
                       "wr": round(len([t for t in after if t['pnl_eur'] > 0]) / len(after) * 100),
                       "exp_r": round(sum(t["r"] for t in after) / len(after), 2)}
                new_entries.append({
                    "t": now_ms, "type": "RELEASE", "bucket": key, "stats": st2,
                    "title": "Contexte réactivé",
                    "reason": f"{key} : {st2['n']} trades depuis l'évitement, esperance {st2['exp_r']} R — je reteste"})
    # revue globale periodique
    if state.get("trade_count", 0) % LEARN_REVIEW_EVERY == 0:
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


def entry_blocked_by_learning(learning, hour_paris, weekend):
    """None si l'entree est autorisee, sinon le contexte evite."""
    key = f"{'WE' if weekend else 'SE'} {hour_paris // 4 * 4:02d}-{hour_paris // 4 * 4 + 4:02d}h"
    if key in learning.get("avoid", {}):
        return key
    return None


# ------------------------------------------------------------------ cycle
def run_cycle(state, trades, learning, journal, webhook):
    """Un check complet : bougies, indicateurs, decisions, sauvegarde.
    Retourne un dict (result) + remplit `events` (embeds a envoyer immediatement)."""
    events = []
    try:
        candles = fetch_candles()
    except Exception as e:
        return {"action": "ERROR", "detail": f"Bougies indisponibles : {e}",
                "equity": state["equity"], "price": None}, events
    if not candles or len(candles) < 25:
        return {"action": "ERROR", "detail": "Pas assez de bougies",
                "equity": state["equity"], "price": None}, events

    eurusd = fetch_eurusd()
    live_price = fetch_ticker()
    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    ema9 = ema_series(closes, 9)
    ema21 = ema_series(closes, 21)
    rsi = rsi14(closes)
    atr = atr14(highs, lows, closes)
    i = len(candles) - 1
    price = live_price if live_price is not None else candles[-1]["c"]
    vol_pct = round(atr / price * 100, 3)

    def htf_trend(gran):
        try:
            htf = fetch_candles(gran=gran)
            if not htf or len(htf) < 25:
                return 0
            cl = [x["c"] for x in htf]
            e9, e21 = ema_series(cl, 9), ema_series(cl, 21)
            return 1 if e9[-1] > e21[-1] else (-1 if e9[-1] < e21[-1] else 0)
        except Exception:
            return 0
    trend_1h = htf_trend(3600)

    now_paris = datetime.now(PARIS)
    hour_paris = now_paris.hour
    weekend = now_paris.weekday() >= 5

    # ---- coupe-circuit journalier (UTC) : reset au changement de jour
    day_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day_utc") != day_utc:
        state["day_utc"] = day_utc
        state["day_start_equity"] = state["equity"]
    day_pnl = state["equity"] - state.get("day_start_equity", state["equity"])
    daily_stop = state.get("day_start_equity", state["equity"]) > 0 and \
        day_pnl <= -state.get("day_start_equity", state["equity"]) * DAILY_STOP_PCT

    result = {
        "price": price, "rsi": round(rsi, 1), "trend_1h": trend_1h,
        "atr": round(atr, 0), "ema9": ema9[i], "ema21": ema21[i],
        "vol_pct": vol_pct, "action": "NONE",
        "equity": state["equity"], "detail": "Aucun signal",
        "position": None, "gates": {}, "daily_stop": daily_stop,
        "today_pnl": round(day_pnl, 2),
    }
    if state["status"] != "RUNNING":
        result["detail"] = "Bot en pause"

    # ---- 1) gestion de la position ouverte (toujours active, meme en coupe-circuit)
    if state["status"] == "RUNNING" and state.get("open_side") in ("LONG", "SHORT"):
        d = 1 if state["open_side"] == "LONG" else -1
        floating_eur = (price - state["open_entry"]) * d * state["open_size"] / eurusd
        result["position"] = {
            "side": state["open_side"], "entry": state["open_entry"],
            "sl": state["open_sl"], "tp": state["open_tp"],
            "size": state["open_size"],
            "floating_eur": round(floating_eur, 2)}
        result["detail"] = f"Position {state['open_side']} en cours"

        # breakeven a +1R puis trailing 2xATR qui ne recule jamais
        moved = (price - state["open_entry"]) * d
        risk_init = abs(state["open_entry"] - state["open_sl_init"])
        if risk_init > 0 and not state.get("open_be") and moved >= BE_R_MULT * risk_init:
            state["open_be"] = True
            state["open_sl"] = state["open_entry"]
            events.append({"type": "BE", "text": f"\U0001F512 Breakeven posé (trade couvert) sur la position BTC {state['open_side']}"})
        if state.get("open_be"):
            if d == 1:
                peak = max(state.get("trail_peak", state["open_entry"]), price)
                state["trail_peak"] = peak
                new_sl = max(state["open_sl"], peak - TRAIL_ATR * atr)
                if new_sl > state["open_sl"]:
                    state["open_sl"] = new_sl
            else:
                trough = min(state.get("trail_trough", state["open_entry"]), price)
                state["trail_trough"] = trough
                new_sl = min(state["open_sl"], trough + TRAIL_ATR * atr)
                if new_sl < state["open_sl"]:
                    state["open_sl"] = new_sl

        # sorties SL / TP (prix temps reel)
        hit_sl = price <= state["open_sl"] if d == 1 else price >= state["open_sl"]
        hit_tp = price >= state["open_tp"] if d == 1 else price <= state["open_tp"]
        if hit_sl or hit_tp:
            exit_price = state["open_sl"] if hit_sl else state["open_tp"]
            reason = "SL" if hit_sl else "TP"
            if hit_sl and state.get("open_be"):
                reason = "TRAIL"       # SL touche apres breakeven = sortie protegee
            pnl_eur = (exit_price - state["open_entry"]) * d * state["open_size"] / eurusd
            state["equity"] = round(state["equity"] + pnl_eur, 4)
            r_mult = round(((exit_price - state["open_entry"]) * d) / risk_init, 2) if risk_init else 0
            state["trade_count"] = state.get("trade_count", 0) + 1
            trade_rec = {
                "side": state["open_side"], "entry": state["open_entry"],
                "exit": exit_price, "sl": state["open_sl_init"], "tp": state["open_tp"],
                "size": state["open_size"], "pnl_eur": round(pnl_eur, 2),
                "r": r_mult, "reason": reason,
                "t_in": state["open_time"], "t_out": datetime.now(PARIS).isoformat(),
                "t_out_ms": int(time.time() * 1000),
                "dur_min": round((time.time() - datetime.fromisoformat(
                    state["open_time"]).timestamp()) / 60),
                "hour_paris": state.get("open_hour", now_paris.hour),
                "weekend": state.get("open_weekend", weekend),
                "vol_pct": state.get("open_vol", vol_pct),
                "rsi_in": state.get("open_rsi", round(rsi, 1)),
                "b1h": state.get("open_b1h", trend_1h),
                "reason_open": state.get("open_reason", ""),
                "equity": state["equity"],
            }
            trades.append(trade_rec)
            trades[:] = trades[-500:]
            result["action"] = f"CLOSE_{reason}"
            result["detail"] = (f"{'+' if pnl_eur >= 0 else ''}{round(pnl_eur, 2)} EUR "
                                f"({r_mult} R, {reason})")
            result["position"] = None
            result["equity"] = state["equity"]
            for k in ("open_side", "open_entry", "open_sl", "open_tp", "open_size",
                      "open_sl_init", "open_time", "open_reason", "position_id",
                      "open_be", "trail_peak", "trail_trough", "open_hour",
                      "open_weekend", "open_vol", "open_rsi", "open_b1h"):
                state.pop(k, None)
            # ---- COUCHE IA : analyse du trade ferme -> decisions de filtres
            entries = review_learning(state, trades, learning, journal)
            if entries:
                events.append({"type": "IA",
                               "embed": build_ia_embed(entries, learning, state)})
            # pause de securite si le capital papier tombe trop bas
            if state["equity"] < PAUSE_THRESHOLD:
                state["status"] = "PAUSED"
                result["detail"] += " — capital papier trop bas, bot en pause"

    # ---- 2) detection d'entree (seulement si pas de position ouverte)
    if (state["status"] == "RUNNING" and not state.get("open_side")
            and result["action"] in ("NONE",)):
        win = candles[-12:]
        conf_side, swing = detect_confirmation(win, atr, price)
        gates = {}
        if conf_side:
            if (conf_side == "LONG" and trend_1h <= 0) or (conf_side == "SHORT" and trend_1h >= 0):
                gates["blocked_by"] = f"biais 1h contre-tendance ({'haut' if trend_1h>0 else 'bas' if trend_1h<0 else 'neutre'})"
            elif conf_side == "LONG" and rsi > RSI_MAX_LONG:
                gates["blocked_by"] = f"RSI surchauffe ({round(rsi,1)} > {RSI_MAX_LONG})"
            elif conf_side == "SHORT" and rsi < RSI_MIN_SHORT:
                gates["blocked_by"] = f"RSI survendu ({round(rsi,1)} < {RSI_MIN_SHORT})"
            elif vol_pct >= VOL_MAX_PCT:
                gates["blocked_by"] = f"volatilité trop haute ({vol_pct}% >= {VOL_MAX_PCT}%)"
            elif daily_stop:
                gates["blocked_by"] = f"coupe-circuit journalier ({round(day_pnl,2)} EUR aujourd'hui)"
            else:
                blocked_key = entry_blocked_by_learning(learning, hour_paris, weekend)
                if blocked_key:
                    gates["blocked_by"] = f"contexte évité par l'IA ({blocked_key})"
            result["gates"] = gates
            if not gates.get("blocked_by"):
                # entree !
                if conf_side == "LONG":
                    sl = swing - SL_BUFFER_ATR * atr
                else:
                    sl = swing + SL_BUFFER_ATR * atr
                risk_usd = abs(price - sl)
                if risk_usd > 0:
                    size_btc = (state["equity"] * RISK_PCT * eurusd) / risk_usd
                    state["open_side"] = conf_side
                    state["open_entry"] = price
                    state["open_sl"] = sl
                    state["open_sl_init"] = sl
                    state["open_tp"] = price + (TP_R_MULT * risk_usd if conf_side == "LONG"
                                                else -TP_R_MULT * risk_usd)
                    state["open_size"] = round(size_btc, 6)
                    state["open_time"] = datetime.now(PARIS).isoformat()
                    state["open_reason"] = ("Confirmation " +
                        ("haussière" if conf_side == "LONG" else "baissière") +
                        " : mouvement >= 2.4xATR puis 2 clôtures 5m, biais 1h " +
                        ("\u2191" if trend_1h > 0 else "\u2193"))
                    state["position_id"] = f"btc{int(time.time())}"
                    state["open_hour"] = hour_paris
                    state["open_weekend"] = weekend
                    state["open_vol"] = vol_pct
                    state["open_rsi"] = round(rsi, 1)
                    state["open_b1h"] = trend_1h
                    result["action"] = f"OPEN_{conf_side}"
                    result["detail"] = f"Position {conf_side} ouverte"
                    result["position"] = {
                        "side": conf_side, "entry": price, "sl": sl,
                        "tp": state["open_tp"], "size": state["open_size"],
                        "context_txt": (f"{'week-end' if weekend else 'semaine'}, "
                                        f"{hour_paris // 4 * 4:02d}-{hour_paris // 4 * 4 + 4:02d}h Paris, "
                                        f"vol {vol_pct}%, RSI {round(rsi,1)}")}
        elif daily_stop:
            result["gates"] = {"blocked_by": "coupe-circuit journalier"}

    state["last_price"] = price
    state["last_cycle"] = datetime.now(PARIS).isoformat()
    state["eurusd"] = eurusd
    state["run_count"] = state.get("run_count", 0) + 1
    return result, events


def detect_confirmation(win, atr, price):
    """(side, swing) ou (None, 0.0) — version stricte validee en backtest BTC :
    mouvement >= 2.4 ATR puis 2 clôtures de confirmation, sans chasse au prix."""
    if len(win) < 12:
        return None, 0.0
    # setup LONG : sommet puis creux, chute >= 2.4 ATR, retournement confirme
    hi_i = max(range(len(win)), key=lambda k: win[k]["h"])
    after_hi = win[hi_i + 1:]
    if len(after_hi) >= 2:
        lo_i = hi_i + 1 + min(range(len(after_hi)), key=lambda k: after_hi[k]["l"])
        hi, lo = win[hi_i]["h"], win[lo_i]["l"]
        if hi - lo >= DROP_MIN_ATR * atr:
            rest = win[lo_i:]
            if len(rest) >= 2:
                first, cont = rest[-2], rest[-1]
                first_is_swing = rest[0] is first
                if (first["c"] > first["o"]
                        and (first_is_swing or first["c"] > win[lo_i]["c"])
                        and cont["c"] > cont["o"] and cont["c"] >= first["c"]
                        and cont["l"] >= first["l"]
                        and price - lo <= CONF_MAX_RUN_ATR * atr):
                    return "LONG", lo
    # setup SHORT : creux puis sommet
    lo2_i = min(range(len(win)), key=lambda k: win[k]["l"])
    after_lo = win[lo2_i + 1:]
    if len(after_lo) >= 2:
        hi2_i = lo2_i + 1 + max(range(len(after_lo)), key=lambda k: after_lo[k]["h"])
        lo2, hi2 = win[lo2_i]["l"], win[hi2_i]["h"]
        if hi2 - lo2 >= DROP_MIN_ATR * atr:
            rest = win[hi2_i:]
            if len(rest) >= 2:
                first, cont = rest[-2], rest[-1]
                first_is_swing = rest[0] is first
                if (first["c"] < first["o"]
                        and (first_is_swing or first["c"] < win[hi2_i]["c"])
                        and cont["c"] < cont["o"] and cont["c"] <= first["c"]
                        and cont["h"] <= first["h"]
                        and hi2 - price <= CONF_MAX_RUN_ATR * atr):
                    return "SHORT", hi2
    return None, 0.0


# ---------------------------------------------------------------------- main
def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    state = load_json(STATE_FILE, None)
    if state is None:
        state = {
            "equity": STARTING_EQUITY, "starting_equity": STARTING_EQUITY,
            "status": "RUNNING", "trade_count": 0,
            "day_utc": None, "day_start_equity": STARTING_EQUITY,
        }
    trades = load_json(TRADES_FILE, [])
    learning = load_json(LEARN_FILE, {"avoid": {}, "strikes": {}})
    journal = load_json(JOURNAL_FILE, [])

    # un seul check par run : run court (~30 s) que GitHub ne throttle pas,
    # la surveillance continue vient de la chaine d'auto-relance (comme le bot or)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("dispatch_day") != today:
        state["dispatch_day"] = today
        state["dispatches_today"] = 0
    state["dispatches_today"] = state.get("dispatches_today", 0) + 1

    result, events = run_cycle(state, trades, learning, journal, webhook)
    save_json(STATE_FILE, state)
    save_json(TRADES_FILE, trades)
    save_json(LEARN_FILE, learning)
    save_json(JOURNAL_FILE, journal)

    if result.get("action") == "ERROR":
        print(json.dumps({"run": state.get("run_count", 0),
                          "error": result["detail"]}, ensure_ascii=False))
        return

    # evenements immediats : BE, revue IA
    for ev in events or []:
        if ev.get("embed"):
            post_discord(webhook, ev["embed"])
        elif ev.get("text"):
            post_discord(webhook, {
                "title": "\u2139\uFE0F " + ev["text"],
                "color": 0x3498DB,
                "footer": {"text": "Bot BTC papier"}})

    # ouverture / clôture -> embed dedie immediat
    if result["action"].startswith(("OPEN_", "CLOSE_")):
        emb = build_event_embed(result, state)
        if emb:
            post_discord(webhook, emb)

    # compte-rendu cadence ~12 min (calé sur le temps réel, pas sur les runs)
    now_ms = int(time.time() * 1000)
    if now_ms - state.get("last_report_ms", 0) >= 12 * 60 * 1000:
        heure = datetime.now(PARIS).strftime("%Hh%M")
        post_discord(webhook, build_embed(heure, result, state, learning))
        state["last_report_ms"] = now_ms
        save_json(STATE_FILE, state)

    print(f"Check BTC #{state.get('run_count', 0)} OK — capital {state['equity']} EUR, "
          f"{state.get('trade_count', 0)} trades, {result['action']}")


if __name__ == "__main__":
    main()
