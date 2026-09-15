# Bot Trading Or XAU/USD — hébergement GitHub Actions

Version autonome du bot de trading IA (compte papier 50 EUR). Tourne gratuitement
sur GitHub Actions toutes les 15 minutes, sans Base44, sans serveur.

Stratégie : croisement EMA9/EMA21 + filtre RSI14, risque 2% par trade,
SL 1.5x ATR, TP 2.5x ATR. Prix : futures or GC=F (Yahoo Finance).
Compte-rendu posté à chaque cycle sur ton salon Discord trading.

## Contenu du pack

1. `bot.py` — le bot complet (analyse, trades, sauvegarde, compte-rendu Discord)
2. `trades.json` — historique des trades (créé automatiquement au premier cycle)
3. `state.json` — capital et position en cours (créé automatiquement)
4. `requirements.txt` — dépendances Python (requests)
5. `.github/workflows/trading-bot.yml` — la planification toutes les 15 min
6. `README.md` — ce fichier

## Installation pas à pas (≈ 10 minutes)

1. Crée un compte sur https://github.com si tu n'en as pas un.

2. Crée un nouveau dépôt : bouton **New repository**.
   - Nom : `bot-trading-or` (par exemple)
   - Visibilité : **Private** recommandé
   - Ne coche rien d'autre (pas de README automatique)
   - Clique **Create repository**

3. Sur la page du dépôt vide, clique **uploading an existing file**.
   Glisse-dépose le contenu de ce pack : `bot.py`, `requirements.txt`,
   et le dossier `.github` (contient le workflow).
   ⚠️ Le dossier `.github` est parfois masqué dans l'explorateur de fichiers —
   active l'affichage des fichiers cachés, ou crée à la main le fichier
   `.github/workflows/trading-bot.yml` dans GitHub (bouton *Create new file*)
   et copie-colle son contenu. Puis **Commit changes**.

4. Ajoute le secret du webhook Discord :
   - Onglet **Settings** du dépôt → **Secrets and variables** → **Actions** → **New repository secret**
   - Name : `DISCORD_WEBHOOK_URL`
   - Secret : l'URL du webhook de ton salon Discord trading
   (celle qui se termine par ...l4Cx7oB)

5. Clique sur l'onglet **Actions** du dépôt. GitHub te propose d'activer
   le workflow : clique **I understand my workflows, go ahead and enable them**.

6. Premier test : dans l'onglet **Actions**, sélectionne **Bot Trading Or XAUUSD**
   → **Run workflow** → bouton vert. Le bot tourne et poste son premier
   compte-rendu sur Discord. L'état (`state.json` / `trades.json`) est committé
   automatiquement dans le dépôt à chaque cycle.

7. C'est tout ! Ensuite il tourne tout seul toutes les 15 minutes.

## Vérifier / superviser

1. Onglet **Actions** : historique de chaque cycle, avec le résumé JSON en log
2. Fichier `state.json` du dépôt : capital, position en cours
3. Fichier `trades.json` du dépôt : historique complet des trades
4. Ton salon Discord : compte-rendu à chaque cycle

## Notes et limites

- GitHub peut retarder certains cycles de quelques minutes aux heures de pointe.
  Sans gravité pour la stratégie (elle se base sur les bougies clôturées).
- Un dépôt privé gratuit a droit à 2000 minutes d'Actions par mois ;
  ce bot consomme environ 1200-1500 minutes/mois (cycles courts). Ça passe.
  Un dépôt public est illimité mais ton historique de trades serait visible.
- Le week-end et la nuit (fermeture CME ~22h-23h Paris), le bot ne trade pas :
  il détecte que les bougies ne rafraîchissent plus et reste en surveillance.
- Pour arrêter le bot : onglet Actions → sélectionne le workflow → menu **...**
  → **Disable workflow**. Pour relancer : **Enable workflow**.

## Passer en réel plus tard

Le bot appelle `fetch_candles` et `post_discord` uniquement — il n'exécute
pas d'ordres réels. Pour du réel, il faudra ajouter un module broker (ex. OANDA,
compte démo d'abord) avec le sizing minimum du broker, et garder le SL côté
serveur du broker pour être protégé même si GitHub est en panne.
