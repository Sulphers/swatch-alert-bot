# Swatch Telegram Monitor

Petit bot Python qui surveille des pages Swatch avec `requests` et `BeautifulSoup`, puis envoie une notification Telegram seulement quand une page semble liee a Audemars Piguet x Swatch / AP x Swatch / Royal Pop et a un contexte produit, collection ou achat.

Il ne passe aucune commande d'achat, ne contourne aucun CAPTCHA, ne contourne aucune file d'attente et se limite a des requetes HTTP raisonnables avec timeout et `User-Agent`.

## Installation locale

```bash
python -m venv .venv
pip install -r requirements.txt
```

Sous Windows PowerShell :

```powershell
$env:TELEGRAM_BOT_TOKEN="123456789:token"
$env:TELEGRAM_CHAT_ID="123456789"
python main.py --test-telegram
python main.py --once
```

## Configuration

Modifiez `config.yaml` :

- `urls` : pages a surveiller.
- `required_keywords` : mots-cles principaux obligatoires.
- `secondary_keywords` : mots-cles de contexte.
- `purchase_keywords` : textes indiquant une page d'achat, disponibilite ou notification.
- `excluded_keywords` : mots-cles a penaliser, par exemple MoonSwatch sans lien Audemars/Royal Pop.
- `allowed_url_patterns` : motifs d'URL autorises, utile pour limiter a la France.
- `blocked_url_patterns` : motifs d'URL refuses, par exemple `/en-us/`.
- `min_score` : score minimal requis pour envoyer une alerte.
- `timeout_seconds` : timeout HTTP.
- `request_delay_seconds` : pause entre deux URLs.
- `user_agent` : identifiant de requete raisonnable.

Le bot analyse aussi `title`, meta description, `og:title`, `og:description`, URL canonique et JSON-LD `Product`.

## Scoring

Une alerte est envoyee uniquement si toutes les conditions suivantes sont vraies :

- score superieur ou egal a `min_score`.
- au moins un `required_keyword` est trouve.
- un contexte produit/achat est trouve : `purchase_keyword`, URL produit/collection, ou JSON-LD `Product`.

Baremes :

- mot-cle principal trouve : `+5`.
- mot-cle achat trouve : `+3`.
- URL produit/collection trouvee : `+3`.
- titre contenant un mot-cle principal : `+4`.
- JSON-LD `Product` detecte : `+4`.
- prix avec `€`, `$` ou `£` detecte : `+2`.
- homepage ou page generique : `-5`.
- MoonSwatch ou exclusion sans Audemars/Royal Pop : `-5`.

Les mots comme `Swatch`, `available`, `add to cart`, `disponible`, `Bioceramic` ou `MoonSwatch` ne declenchent jamais une alerte seuls.

## Secrets GitHub Actions

Dans votre depot GitHub :

1. Ouvrez `Settings`.
2. Allez dans `Secrets and variables` puis `Actions`.
3. Cliquez sur `New repository secret`.
4. Ajoutez `TELEGRAM_BOT_TOKEN`.
5. Ajoutez `TELEGRAM_CHAT_ID`.

Le workflow `.github/workflows/monitor.yml` lance :

```bash
python main.py --once
```

toutes les 5 minutes avec le cron :

```yaml
*/5 * * * *
```

## Etat et anti-doublons

Les alertes deja envoyees sont conservees dans `state.json`. Apres chaque execution GitHub Actions, si `state.json` a change, le workflow le commit automatiquement dans le depot.

Pour que le commit automatique fonctionne, verifiez dans GitHub :

1. `Settings` > `Actions` > `General`.
2. Section `Workflow permissions`.
3. Selectionnez `Read and write permissions`.

Cette methode est simple et gratuite. Elle evite de renvoyer la meme alerte a chaque execution planifiee.

## Commandes

```bash
python main.py --test-telegram
python main.py --once
python main.py --debug-url "https://www.swatch.com/fr-fr/..."
```

`--debug-url` affiche le diagnostic complet de scoring pour une URL sans envoyer Telegram. Quand une page est rejetee, `--once` loggue l'URL, le score et la raison du rejet.

Sans `--once`, le bot tourne en boucle avec `interval_seconds`, ce qui est utile en local. GitHub Actions utilise toujours `--once`.
