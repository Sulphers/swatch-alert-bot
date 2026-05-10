import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from bs4 import BeautifulSoup


CONFIG_PATH = Path("config.yaml")
STATE_PATH = Path("state.json")


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; SwatchAvailabilityMonitor/1.0; "
    "+https://github.com/your-org/your-repo)"
)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration introuvable: {path}")

    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    urls = config.get("urls") or []
    keywords = config.get("keywords") or []
    if not urls:
        raise ValueError("config.yaml doit contenir au moins une URL dans 'urls'.")
    if not keywords:
        raise ValueError("config.yaml doit contenir au moins un mot-cle dans 'keywords'.")

    return config


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sent_alerts": {}, "last_run_utc": None}

    try:
        with path.open("r", encoding="utf-8") as file:
            state = json.load(file)
    except json.JSONDecodeError:
        return {"sent_alerts": {}, "last_run_utc": None}

    if not isinstance(state, dict):
        return {"sent_alerts": {}, "last_run_utc": None}

    state.setdefault("sent_alerts", {})
    state.setdefault("last_run_utc", None)
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["last_run_utc"] = utc_now_iso()
    with path.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def fetch_page(url: str, timeout: int, user_agent: str) -> str:
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


def extract_text(page_html: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    return normalize_text(soup.get_text(" "))


def find_keyword_matches(text: str, keywords: list[str], excerpt_chars: int) -> list[dict[str, str]]:
    matches = []
    seen_keywords = set()

    for keyword in keywords:
        keyword = str(keyword).strip()
        if not keyword:
            continue

        pattern = re.compile(re.escape(keyword), re.IGNORECASE)
        match = pattern.search(text)
        if not match:
            continue

        keyword_key = keyword.casefold()
        if keyword_key in seen_keywords:
            continue
        seen_keywords.add(keyword_key)

        start = max(match.start() - excerpt_chars // 2, 0)
        end = min(match.end() + excerpt_chars // 2, len(text))
        excerpt = text[start:end].strip()
        if start > 0:
            excerpt = "..." + excerpt
        if end < len(text):
            excerpt = excerpt + "..."

        matches.append({"keyword": keyword, "excerpt": excerpt})

    return matches


def alert_id(url: str, keyword: str, excerpt: str) -> str:
    normalized_excerpt = normalize_text(excerpt).casefold()
    raw = f"{url}\n{keyword.casefold()}\n{normalized_excerpt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def send_telegram_message(token: str, chat_id: str, message: str, timeout: int) -> None:
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    response = requests.post(endpoint, json=payload, timeout=timeout)
    response.raise_for_status()


def build_message(url: str, keyword: str, excerpt: str, detected_at: str) -> str:
    safe_url = html.escape(url)
    safe_keyword = html.escape(keyword)
    safe_excerpt = html.escape(excerpt)
    safe_time = html.escape(detected_at)

    return (
        "Alerte Swatch\n"
        f"URL: {safe_url}\n"
        f"Mot-cle detecte: {safe_keyword}\n"
        f"Heure UTC: {safe_time}\n\n"
        f"Extrait:\n{safe_excerpt}"
    )


def get_telegram_credentials() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError(
            "Variables d'environnement manquantes: TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID."
        )
    return token, chat_id


def run_once(config: dict[str, Any], state: dict[str, Any]) -> int:
    timeout = int(config.get("timeout_seconds", 15))
    request_delay = float(config.get("request_delay_seconds", 2))
    excerpt_chars = int(config.get("excerpt_chars", 260))
    user_agent = str(config.get("user_agent") or DEFAULT_USER_AGENT)
    urls = [str(url).strip() for url in config["urls"] if str(url).strip()]
    keywords = [str(keyword).strip() for keyword in config["keywords"] if str(keyword).strip()]

    token, chat_id = get_telegram_credentials()
    sent_alerts = state.setdefault("sent_alerts", {})
    new_alerts = 0

    for index, url in enumerate(urls):
        if index > 0 and request_delay > 0:
            time.sleep(request_delay)

        try:
            page_html = fetch_page(url, timeout=timeout, user_agent=user_agent)
            text = extract_text(page_html)
        except requests.RequestException as exc:
            print(f"[WARN] Erreur HTTP pour {url}: {exc}", file=sys.stderr)
            continue

        matches = find_keyword_matches(text, keywords, excerpt_chars=excerpt_chars)
        for match in matches:
            keyword = match["keyword"]
            excerpt = match["excerpt"]
            key = alert_id(url, keyword, excerpt)

            if key in sent_alerts:
                continue

            detected_at = utc_now_iso()
            message = build_message(url, keyword, excerpt, detected_at)

            try:
                send_telegram_message(token, chat_id, message, timeout=timeout)
            except requests.RequestException as exc:
                print(f"[ERROR] Envoi Telegram impossible pour {url}: {exc}", file=sys.stderr)
                continue

            sent_alerts[key] = {
                "url": url,
                "keyword": keyword,
                "detected_at_utc": detected_at,
                "excerpt_hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            }
            new_alerts += 1
            print(f"[INFO] Alerte envoyee: {keyword} sur {url}")

    return new_alerts


def test_telegram(config: dict[str, Any]) -> None:
    timeout = int(config.get("timeout_seconds", 15))
    token, chat_id = get_telegram_credentials()
    message = f"Test Telegram Swatch monitor\nHeure UTC: {utc_now_iso()}"
    send_telegram_message(token, chat_id, message, timeout=timeout)
    print("[INFO] Message de test Telegram envoye.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Surveille des pages Swatch et alerte via Telegram.")
    parser.add_argument("--once", action="store_true", help="Execute une seule verification puis quitte.")
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Envoie un message Telegram de test puis quitte.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(CONFIG_PATH)

    if args.test_telegram:
        test_telegram(config)
        return 0

    state = load_state(STATE_PATH)

    if args.once:
        new_alerts = run_once(config, state)
        save_state(STATE_PATH, state)
        print(f"[INFO] Execution terminee. Nouvelles alertes: {new_alerts}")
        return 0

    interval = int(config.get("interval_seconds", 300))
    while True:
        new_alerts = run_once(config, state)
        save_state(STATE_PATH, state)
        print(f"[INFO] Cycle termine. Nouvelles alertes: {new_alerts}")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
