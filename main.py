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
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup


CONFIG_PATH = Path("config.yaml")
STATE_PATH = Path("state.json")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; SwatchAvailabilityMonitor/1.0; "
    "+https://github.com/your-org/your-repo)"
)

PRODUCT_URL_PATTERNS = [
    "/product/",
    "/products/",
    "/collection/",
    "/collections/",
    "/shop/",
    "/watches/",
]


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration introuvable: {path}")

    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    if not config.get("urls"):
        raise ValueError("config.yaml doit contenir au moins une URL dans 'urls'.")
    if not (config.get("required_keywords") or config.get("keywords")):
        raise ValueError("config.yaml doit contenir au moins un mot-cle dans 'required_keywords'.")

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


def clean_list(values: list[Any] | None) -> list[str]:
    return [str(value).strip() for value in values or [] if str(value).strip()]


def keyword_pattern(keyword: str) -> str:
    escaped = re.escape(keyword)
    prefix = r"(?<![A-Za-z0-9])" if keyword[0].isalnum() else ""
    suffix = r"(?![A-Za-z0-9])" if keyword[-1].isalnum() else ""
    return f"{prefix}{escaped}{suffix}"


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


def find_keywords(text: str, keywords: list[str]) -> list[str]:
    found = []
    seen = set()
    for keyword in keywords:
        if re.search(keyword_pattern(keyword), text, re.IGNORECASE):
            key = keyword.casefold()
            if key not in seen:
                seen.add(key)
                found.append(keyword)
    return found


def first_keyword_match(text: str, keywords: list[str]) -> re.Match[str] | None:
    best_match = None
    for keyword in keywords:
        match = re.search(keyword_pattern(keyword), text, re.IGNORECASE)
        if match and (best_match is None or match.start() < best_match.start()):
            best_match = match
    return best_match


def make_excerpt(text: str, keywords: list[str], max_chars: int = 500) -> str:
    if not text:
        return ""

    match = first_keyword_match(text, keywords)
    if not match:
        return text[:max_chars].strip()

    start = max(match.start() - max_chars // 2, 0)
    end = min(start + max_chars, len(text))
    start = max(end - max_chars, 0)

    excerpt = text[start:end].strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt = excerpt + "..."
    return excerpt[: max_chars + 6]


def alert_id(url: str, keyword: str, page_type: str) -> str:
    raw = f"{url}\n{keyword.casefold()}\n{page_type}"
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


def get_meta_content(soup: BeautifulSoup, **attrs: str) -> str:
    tag = soup.find("meta", attrs=attrs)
    content = tag.get("content", "") if tag else ""
    return normalize_text(str(content))


def get_page_metadata(soup: BeautifulSoup, url: str) -> dict[str, str]:
    title = normalize_text(soup.title.get_text(" ")) if soup.title else ""
    canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    canonical_url = str(canonical_tag.get("href", "")).strip() if canonical_tag else ""
    return {
        "title": title,
        "meta_description": get_meta_content(soup, name="description"),
        "og_title": get_meta_content(soup, property="og:title"),
        "og_description": get_meta_content(soup, property="og:description"),
        "canonical_url": canonical_url or url,
    }


def contains_jsonld_type(data: Any, expected_type: str) -> bool:
    if isinstance(data, list):
        return any(contains_jsonld_type(item, expected_type) for item in data)
    if not isinstance(data, dict):
        return False

    type_value = data.get("@type")
    if isinstance(type_value, str) and type_value.casefold() == expected_type.casefold():
        return True
    if isinstance(type_value, list) and any(
        str(item).casefold() == expected_type.casefold() for item in type_value
    ):
        return True

    graph = data.get("@graph")
    if graph is not None and contains_jsonld_type(graph, expected_type):
        return True

    return any(contains_jsonld_type(value, expected_type) for value in data.values())


def has_jsonld_product(soup: BeautifulSoup) -> bool:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            if '"Product"' in raw or "'Product'" in raw:
                return True
            continue
        if contains_jsonld_type(data, "Product"):
            return True
    return False


def is_homepage(url: str) -> bool:
    path = urlparse(url).path.strip("/").casefold()
    return path in {"", "fr-fr", "en-us", "fr", "en"}


def matches_any_pattern(url: str, patterns: list[str]) -> bool:
    url_lower = url.casefold()
    return any(pattern.casefold() in url_lower for pattern in patterns)


def detect_page_type(url: str, has_product_jsonld: bool) -> str:
    path = urlparse(url).path.casefold()
    if has_product_jsonld or any(part in path for part in ["/product/", "/products/", "/shop/", "/watches/"]):
        return "product"
    if any(part in path for part in ["/collection/", "/collections/"]):
        return "collection"
    return "generic"


def analyze_page(url: str, page_html: str, config: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(page_html, "html.parser")
    metadata = get_page_metadata(soup, url)
    text = extract_text(page_html)
    searchable_text = normalize_text(
        " ".join(
            [
                metadata["title"],
                metadata["meta_description"],
                metadata["og_title"],
                metadata["og_description"],
                metadata["canonical_url"],
                text,
            ]
        )
    )

    required_keywords = clean_list(config.get("required_keywords") or config.get("keywords"))
    secondary_keywords = clean_list(config.get("secondary_keywords"))
    purchase_keywords = clean_list(config.get("purchase_keywords"))
    excluded_keywords = clean_list(config.get("excluded_keywords"))
    allowed_url_patterns = clean_list(config.get("allowed_url_patterns"))
    blocked_url_patterns = clean_list(config.get("blocked_url_patterns"))

    required_found = find_keywords(searchable_text, required_keywords)
    secondary_found = find_keywords(searchable_text, secondary_keywords)
    purchase_found = find_keywords(searchable_text, purchase_keywords)
    excluded_found = find_keywords(searchable_text, excluded_keywords)
    title_required_found = find_keywords(metadata["title"], required_keywords)
    product_jsonld = has_jsonld_product(soup)
    canonical_url = metadata["canonical_url"] or url
    url_product_or_collection = matches_any_pattern(url, PRODUCT_URL_PATTERNS) or matches_any_pattern(
        canonical_url, PRODUCT_URL_PATTERNS
    )
    has_price = bool(re.search(r"(?:€|\$|£)\s?\d+|\d+(?:[,.]\d{2})?\s?(?:€|\$|£)", searchable_text))
    page_type = detect_page_type(canonical_url, product_jsonld)

    score = 0
    score_reasons = []
    rejection_reasons = []

    if required_found:
        score += 5
        score_reasons.append("mot-cle principal trouve: +5")
    else:
        rejection_reasons.append("aucun required_keyword trouve")

    if purchase_found:
        score += 3
        score_reasons.append("mot-cle achat trouve: +3")

    if url_product_or_collection:
        score += 3
        score_reasons.append("URL produit/collection trouvee: +3")

    if title_required_found:
        score += 4
        score_reasons.append("titre avec mot-cle principal: +4")

    if product_jsonld:
        score += 4
        score_reasons.append("JSON-LD Product detecte: +4")

    if has_price:
        score += 2
        score_reasons.append("prix detecte: +2")

    if is_homepage(url) or is_homepage(canonical_url):
        score -= 5
        score_reasons.append("page generique/homepage: -5")
        rejection_reasons.append("page generique/homepage")

    strong_terms = [
        "audemars",
        "audemars piguet",
        "audemars-piguet",
        "ap x swatch",
        "ap swatch",
        "royal pop",
        "royal oak",
        "royaloak",
    ]
    if excluded_found and not find_keywords(searchable_text, strong_terms):
        score -= 5
        score_reasons.append("MoonSwatch/exclusion sans Audemars/Royal Pop: -5")
        rejection_reasons.append(f"mot-cle exclu sans collaboration forte: {', '.join(excluded_found)}")

    if blocked_url_patterns and matches_any_pattern(url, blocked_url_patterns):
        rejection_reasons.append("URL bloquee par blocked_url_patterns")

    if allowed_url_patterns and not matches_any_pattern(url, allowed_url_patterns):
        rejection_reasons.append("URL hors allowed_url_patterns")

    purchase_context = bool(purchase_found or url_product_or_collection or product_jsonld)
    if not purchase_context:
        rejection_reasons.append("pas de contexte achat/produit/collection")

    min_score = int(config.get("min_score", 10))
    if score < min_score:
        rejection_reasons.append(f"score {score} inferieur a min_score {min_score}")

    blocking_reasons = {
        "page generique/homepage",
        "URL bloquee par blocked_url_patterns",
        "URL hors allowed_url_patterns",
    }
    should_alert = bool(
        score >= min_score
        and required_found
        and purchase_context
        and not any(
            reason in blocking_reasons or reason.startswith("mot-cle exclu sans collaboration forte:")
            for reason in rejection_reasons
        )
    )

    return {
        "url": url,
        "score": score,
        "min_score": min_score,
        "should_alert": should_alert,
        "rejection_reasons": rejection_reasons,
        "score_reasons": score_reasons,
        "required_found": required_found,
        "secondary_found": secondary_found,
        "purchase_found": purchase_found,
        "excluded_found": excluded_found,
        "title_required_found": title_required_found,
        "has_product_jsonld": product_jsonld,
        "has_price": has_price,
        "url_product_or_collection": url_product_or_collection,
        "page_type": page_type,
        "metadata": metadata,
        "excerpt": make_excerpt(text, required_found or purchase_found or secondary_found, max_chars=500),
    }


def build_message(analysis: dict[str, Any], detected_at: str) -> str:
    return (
        "Alerte Swatch\n"
        f"Heure UTC: {html.escape(detected_at)}\n\n"
        f"Score: {html.escape(str(analysis['score']))}\n"
        f"Mot-cle principal: {html.escape(analysis['required_found'][0])}\n"
        f"Type de page: {html.escape(analysis['page_type'])}\n"
        f"URL: {html.escape(analysis['url'])}\n"
        f"Titre: {html.escape(analysis['metadata'].get('title') or '(sans titre)')}\n\n"
        f"Extrait:\n{html.escape(analysis['excerpt'])}"
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
    user_agent = str(config.get("user_agent") or DEFAULT_USER_AGENT)
    urls = [str(url).strip() for url in config["urls"] if str(url).strip()]

    token, chat_id = get_telegram_credentials()
    sent_alerts = state.setdefault("sent_alerts", {})
    new_alerts = 0

    for index, url in enumerate(urls):
        if index > 0 and request_delay > 0:
            time.sleep(request_delay)

        try:
            page_html = fetch_page(url, timeout=timeout, user_agent=user_agent)
            analysis = analyze_page(url, page_html, config)
        except requests.RequestException as exc:
            print(f"[WARN] Erreur HTTP pour {url}: {exc}", file=sys.stderr)
            continue

        if not analysis["should_alert"]:
            reason = "; ".join(analysis["rejection_reasons"]) or "conditions strictes non remplies"
            print(f"[INFO] Rejet: {url} | score={analysis['score']} | raison={reason}")
            continue

        keyword = analysis["required_found"][0]
        key = alert_id(url, keyword, analysis["page_type"])
        if key in sent_alerts:
            print(f"[INFO] Alerte deja envoyee: {keyword} sur {url}")
            continue

        detected_at = utc_now_iso()
        message = build_message(analysis, detected_at)

        try:
            send_telegram_message(token, chat_id, message, timeout=timeout)
        except requests.RequestException as exc:
            print(f"[ERROR] Envoi Telegram impossible pour {url}: {exc}", file=sys.stderr)
            continue

        sent_alerts[key] = {
            "url": url,
            "keyword": keyword,
            "score": analysis["score"],
            "page_type": analysis["page_type"],
            "detected_at_utc": detected_at,
            "title": analysis["metadata"].get("title"),
            "excerpt_hash": hashlib.sha256(analysis["excerpt"].encode("utf-8")).hexdigest(),
        }
        new_alerts += 1
        print(f"[INFO] Alerte envoyee: {keyword} sur {url} | score={analysis['score']}")

    return new_alerts


def test_telegram(config: dict[str, Any]) -> None:
    timeout = int(config.get("timeout_seconds", 15))
    token, chat_id = get_telegram_credentials()
    message = f"Test Telegram Swatch monitor\nHeure UTC: {utc_now_iso()}"
    send_telegram_message(token, chat_id, message, timeout=timeout)
    print("[INFO] Message de test Telegram envoye.")


def debug_url(url: str, config: dict[str, Any]) -> None:
    timeout = int(config.get("timeout_seconds", 15))
    user_agent = str(config.get("user_agent") or DEFAULT_USER_AGENT)
    page_html = fetch_page(url, timeout=timeout, user_agent=user_agent)
    analysis = analyze_page(url, page_html, config)
    print(json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Surveille des pages Swatch et alerte via Telegram.")
    parser.add_argument("--once", action="store_true", help="Execute une seule verification puis quitte.")
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Envoie un message Telegram de test puis quitte.",
    )
    parser.add_argument(
        "--debug-url",
        help="Affiche le diagnostic complet de scoring pour une URL sans envoyer Telegram.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(CONFIG_PATH)

    if args.test_telegram:
        test_telegram(config)
        return 0

    if args.debug_url:
        debug_url(args.debug_url, config)
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
