# Swatch Telegram Monitor

Petit bot Python qui surveille des pages Swatch avec `requests` et `BeautifulSoup`, puis envoie une notification Telegram quand un mot-cle configure est detecte.

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
- `keywords` : mots-cles a detecter.
- `timeout_seconds` : timeout HTTP.
- `request_delay_seconds` : pause entre deux URLs.
- `user_agent` : identifiant de requete raisonnable.

Le bot envoie une alerte Telegram avec l'URL, le mot-cle detecte, l'heure UTC et un extrait du texte trouve.

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
```

Sans `--once`, le bot tourne en boucle avec `interval_seconds`, ce qui est utile en local. GitHub Actions utilise toujours `--once`.
