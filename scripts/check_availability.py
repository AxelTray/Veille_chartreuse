"""
Verifie la disponibilite de bouteilles rares de Chartreuse (Jaune VEP,
Reine des Liqueurs, Jeroboam) sur une liste de sites distributeurs europeens.

Ecrit le resultat dans docs/status.json (etat courant) et ajoute une ligne
dans docs/history.json (historique, utile pour le dashboard).

Ce script est volontairement heuristique : chaque site a une mise en page
differente, donc on cherche le nom du produit dans la page puis on regarde
le texte autour pour deviner s'il est en stock ou non. Les entrees
ambigues remontent en statut "a_verifier" avec un lien direct pour
verifier a la main -> a affiner site par site avec le temps.
"""
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sites.json"
DOCS_DIR = ROOT / "docs"
STATUS_PATH = DOCS_DIR / "status.json"
HISTORY_PATH = DOCS_DIR / "history.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

UNAVAILABLE_PATTERNS = [
    "rupture de stock", "rupture", "epuise", "indisponible", "non disponible",
    "out of stock", "sold out", "unavailable", "currently unavailable",
    "agotado", "no disponible", "nicht verfugbar", "nicht auf lager",
    "ausverkauft",
]

AVAILABLE_PATTERNS = [
    "ajouter au panier", "ajouter a mon panier", "add to basket",
    "add to cart", "in den warenkorb", "anadir al carrito",
    "en stock", "disponible", "in stock",
]

WINDOW = 260  # nombre de caracteres regardes avant/apres le mot-cle


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def is_paris_21h(now_utc: datetime) -> bool:
    """Vrai si l'heure locale a Paris est entre 20h45 et 21h30 (marge DST)."""
    paris_now = now_utc.astimezone(ZoneInfo("Europe/Paris"))
    return paris_now.hour == 21 and paris_now.minute < 30


def fetch(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        print(f"  [erreur] {url} -> {exc}", file=sys.stderr)
        return None


def analyze(html: str, keywords: list[str]) -> tuple[str, str]:
    text = strip_accents(html.lower())
    for kw in keywords:
        kw_norm = strip_accents(kw.lower())
        idx = text.find(kw_norm)
        if idx == -1:
            continue
        start = max(0, idx - WINDOW)
        end = min(len(text), idx + len(kw_norm) + WINDOW)
        window = text[start:end]
        if any(p in window for p in UNAVAILABLE_PATTERNS):
            return "rupture", kw
        if any(p in window for p in AVAILABLE_PATTERNS):
            return "disponible", kw
        return "a_verifier", kw
    return "non_trouve", ""


def run_checks() -> list[dict]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    targets = config["targets"]
    sites = config["sites"]
    checked_at = datetime.now(ZoneInfo("Europe/Paris")).isoformat()

    results = []
    for site in sites:
        print(f"Verification: {site['name']}")
        html = fetch(site["search_url"])
        for target in targets:
            if html is None:
                status, matched = "erreur", ""
            else:
                status, matched = analyze(html, target["keywords"])
            results.append({
                "site": site["name"],
                "url": site["search_url"],
                "target_key": target["key"],
                "target_label": target["label"],
                "status": status,
                "matched_keyword": matched,
                "checked_at": checked_at,
            })
            print(f"  - {target['label']}: {status}")
    return results


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_status_and_history(results: list[dict]) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    checked_at = results[0]["checked_at"] if results else datetime.now(ZoneInfo("Europe/Paris")).isoformat()
    status_doc = {"checked_at": checked_at, "results": results}
    STATUS_PATH.write_text(json.dumps(status_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    history = load_json(HISTORY_PATH, [])
    history.append({
        "checked_at": checked_at,
        "available_count": sum(1 for r in results if r["status"] == "disponible"),
        "results": results,
    })
    cutoff = datetime.now(ZoneInfo("Europe/Paris")) - timedelta(days=120)
    history = [h for h in history if datetime.fromisoformat(h["checked_at"]) >= cutoff]
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    force = "--force" in sys.argv
    now_utc = datetime.now(ZoneInfo("UTC"))
    if not force and not is_paris_21h(now_utc):
        print("Pas encore ~21h a Paris, on ne fait rien cette fois-ci.")
        return

    results = run_checks()
    save_status_and_history(results)
    print("Termine. docs/status.json et docs/history.json mis a jour.")


if __name__ == "__main__":
    main()
