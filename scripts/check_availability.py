"""
Verifie la disponibilite et le prix de bouteilles rares de Chartreuse (Jaune
VEP, Reine des Liqueurs, Jeroboam) sur une liste de sites distributeurs
europeens.

Ecrit le resultat dans docs/status.json (etat courant) et ajoute une ligne
dans docs/history.json (historique, utile pour le dashboard).

Strategie en deux temps, par ordre de confiance :
1) Donnees structurees schema.org (JSON-LD) que la plupart des boutiques
   integrent pour Google Shopping/rich snippets : prix et disponibilite
   exacts, pas devines. C'est la source fiable.
2) A defaut (site sans JSON-LD), repli heuristique : on cherche le nom du
   produit dans la page et on regarde un peu de texte autour pour deviner
   stock/rupture/prix. Marque comme confidence="heuristique" dans le JSON
   pour que le dashboard affiche un badge "a verifier" plutot que d'annoncer
   un statut trompeur.
"""
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

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
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

UNAVAILABLE_PATTERNS = [
    "rupture de stock", "epuise", "indisponible", "non disponible",
    "out of stock", "sold out", "unavailable", "currently unavailable",
    "agotado", "no disponible", "nicht verfugbar", "nicht auf lager",
    "ausverkauft", "victime de son succes",
]

AVAILABLE_PATTERNS = [
    "ajouter au panier", "ajouter a mon panier", "add to basket",
    "add to cart", "in den warenkorb", "anadir al carrito",
    "en stock", "disponibilite immediate",
    "derniere piece", "dernieres pieces",
]

WINDOW = 350  # nombre de caracteres regardes avant/apres le mot-cle (repli heuristique)

PRICE_PATTERN = re.compile(r"(\d{1,4}(?:[.,]\d{2})?)\s?€|€\s?(\d{1,4}(?:[.,]\d{2})?)")

LDJSON_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def squash(text: str) -> str:
    """minuscule, sans accents, sans ponctuation/espaces -> comparaison robuste
    aux variations de mise en forme ("V.E.P. Jaune" == "vep jaune")."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(text.lower()))


def is_paris_21h(now_utc: datetime) -> bool:
    """Vrai si l'heure locale a Paris est entre 21h00 et 21h30 (marge DST)."""
    paris_now = now_utc.astimezone(ZoneInfo("Europe/Paris"))
    return paris_now.hour == 21 and paris_now.minute < 30


def fetch(url: str) -> str | None:
    try:
        resp = SESSION.get(url, timeout=20)
        resp.raise_for_status()
        # Certains sites ne declarent pas leur charset dans l'entete HTTP ;
        # requests retombe alors sur ISO-8859-1 par defaut (RFC 2616) et les
        # caracteres accentues (site suisse observe: "Ã©" au lieu de "é")
        # cassent la comparaison de mots-cles. La detection automatique est
        # plus fiable pour du HTML europeen.
        if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or resp.encoding
        return resp.text
    except requests.RequestException as exc:
        print(f"  [erreur] {url} -> {exc}", file=sys.stderr)
        return None


def _walk_ldjson(node, products: list) -> None:
    if isinstance(node, dict):
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if any(t and "product" in str(t).lower() for t in types):
            products.append(node)
        for value in node.values():
            _walk_ldjson(value, products)
    elif isinstance(node, list):
        for item in node:
            _walk_ldjson(item, products)


def extract_structured_products(html: str) -> list[dict]:
    products: list[dict] = []
    for match in LDJSON_PATTERN.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            # strict=False : de nombreux sites mettent des retours a la ligne
            # bruts dans les descriptions, ce qui n'est pas du JSON strict.
            data = json.loads(raw, strict=False)
        except json.JSONDecodeError:
            continue
        _walk_ldjson(data, products)
    return products


def product_price_and_availability(product: dict) -> tuple[float | None, str | None, str]:
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None, None, ""
    price_raw = offers.get("price") or offers.get("lowPrice")
    price = None
    if price_raw is not None:
        try:
            price = float(str(price_raw).replace(",", "."))
        except ValueError:
            price = None
    currency = offers.get("priceCurrency")
    availability = str(offers.get("availability") or "")
    return price, currency, availability


def availability_to_status(availability: str) -> str | None:
    a = availability.lower()
    if any(s in a for s in ("instock", "limitedavailability", "preorder", "backorder")):
        return "disponible"
    if any(s in a for s in ("outofstock", "soldout", "discontinued")):
        return "rupture"
    return None


def find_matching_products(products: list[dict], keywords: list[str]) -> list[dict]:
    """Renvoie TOUS les produits structures qui correspondent (pas juste le
    premier) : une meme page peut lister plusieurs millesimes/couleurs de
    Jeroboam ou de VEP, et on veut les voir tous sur le dashboard."""
    matches = []
    seen = set()
    for product in products:
        name = (product.get("name") or "").strip()
        squashed_name = squash(name)
        matched_kw = next((kw for kw in keywords if squash(kw) in squashed_name), None)
        if not matched_kw:
            continue
        price, currency, availability = product_price_and_availability(product)
        status = availability_to_status(availability)
        if status is None and price is None:
            continue  # bloc structure sans info exploitable -> pas utile
        dedup_key = (name.lower(), price)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        matches.append({
            "status": status or "a_verifier",
            "matched_keyword": matched_kw,
            "product_name": name or None,
            "price": price,
            "currency": currency or ("EUR" if price is not None else None),
            "confidence": "structure",
        })
    return matches


def extract_price_heuristic(scope: str) -> float | None:
    m = PRICE_PATTERN.search(scope)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def build_squash_index(text: str) -> tuple[str, list[int]]:
    """Renvoie le texte reduit aux caracteres alphanumeriques, et pour
    chacun sa position d'origine -> permet de retrouver "V.E.P. Jaune"
    avec le mot-cle "vep jaune" malgre la ponctuation, tout en pouvant
    localiser une fenetre de contexte dans le texte original."""
    chars = []
    index_map = []
    for i, ch in enumerate(text):
        if ch.isalnum():
            chars.append(ch)
            index_map.append(i)
    return "".join(chars), index_map


def analyze_heuristic(html: str, keywords: list[str]) -> dict:
    text = strip_accents(html.lower())
    squashed_text, index_map = build_squash_index(text)
    for kw in keywords:
        pos = squashed_text.find(squash(kw))
        if pos == -1:
            continue
        idx = index_map[pos]
        start = max(0, idx - WINDOW)
        end = min(len(text), idx + WINDOW)
        scope = text[start:end]
        price = extract_price_heuristic(scope)
        if any(p in scope for p in UNAVAILABLE_PATTERNS):
            status = "rupture"
        elif any(p in scope for p in AVAILABLE_PATTERNS):
            status = "disponible"
        else:
            status = "a_verifier"
        return {
            "status": status,
            "matched_keyword": kw,
            "product_name": None,
            "price": price,
            "currency": "EUR" if price is not None else None,
            "confidence": "heuristique",
        }
    return {
        "status": "non_trouve",
        "matched_keyword": "",
        "product_name": None,
        "price": None,
        "currency": None,
        "confidence": "heuristique",
    }


def analyze(html: str, keywords: list[str]) -> list[dict]:
    """Renvoie une LISTE de resultats : le plus souvent un seul, mais
    potentiellement plusieurs si la page structuree liste plusieurs
    variantes correspondant a la meme cible (plusieurs millesimes de VEP,
    plusieurs couleurs de Jeroboam...)."""
    products = extract_structured_products(html)
    matches = find_matching_products(products, keywords)
    if matches:
        return matches
    return [analyze_heuristic(html, keywords)]


def run_checks() -> list[dict]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    targets = config["targets"]
    watches = config["watches"]
    checked_at = datetime.now(ZoneInfo("Europe/Paris")).isoformat()

    results = []
    for watch in watches:
        print(f"Verification: {watch['site']}")
        html = fetch(watch["url"])
        for target_key in watch["targets"]:
            target = targets[target_key]
            if html is None:
                outcomes = [{
                    "status": "erreur", "matched_keyword": "", "product_name": None,
                    "price": None, "currency": None, "confidence": "heuristique",
                }]
            else:
                outcomes = analyze(html, target["keywords"])
            for outcome in outcomes:
                results.append({
                    "site": watch["site"],
                    "url": watch["url"],
                    "target_key": target_key,
                    "target_label": target["label"],
                    "product_name": outcome["product_name"],
                    "status": outcome["status"],
                    "matched_keyword": outcome["matched_keyword"],
                    "price": outcome["price"],
                    "currency": outcome["currency"],
                    "confidence": outcome["confidence"],
                    "checked_at": checked_at,
                })
                price_str = f" - {outcome['price']} {outcome['currency']}" if outcome["price"] else ""
                name_str = f" [{outcome['product_name']}]" if outcome["product_name"] else ""
                print(f"  - {target['label']}{name_str}: {outcome['status']} ({outcome['confidence']}){price_str}")
    return results


def result_key(r: dict) -> tuple:
    """Identifiant stable d'un produit suivi a travers les runs, pour
    detecter les transitions de statut (pas juste site+cible, un site peut
    avoir plusieurs produits pour la meme cible)."""
    return (r["site"], r["target_key"], r.get("product_name") or r.get("matched_keyword") or "")


def find_newly_available(previous_results: list[dict], new_results: list[dict]) -> list[dict]:
    previous_status = {result_key(r): r["status"] for r in previous_results}
    newly = []
    for r in new_results:
        if r["status"] != "disponible":
            continue
        if previous_status.get(result_key(r)) != "disponible":
            newly.append(r)
    return newly


def format_telegram_message(newly: list[dict]) -> str:
    lines = ["🟡 <b>Chartreuse disponible !</b>"]
    for r in newly:
        name = r.get("product_name") or r["target_label"]
        price = f" — {r['price']:.2f} {r['currency']}" if r.get("price") else ""
        lines.append(f'\n<b>{r["site"]}</b>\n{name}{price}\n{r["url"]}')
    return "\n".join(lines)


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents, alerte non envoyee.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        resp.raise_for_status()
        print("  [telegram] alerte envoyee.")
    except requests.RequestException as exc:
        print(f"  [telegram] erreur d'envoi: {exc}", file=sys.stderr)


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

    previous_results = load_json(STATUS_PATH, {}).get("results", [])
    results = run_checks()
    save_status_and_history(results)

    newly_available = find_newly_available(previous_results, results)
    if newly_available:
        print(f"{len(newly_available)} nouvelle(s) disponibilite(s) -> envoi alerte Telegram")
        send_telegram_message(format_telegram_message(newly_available))
    else:
        print("Aucune nouvelle disponibilite depuis le dernier check.")

    print("Termine. docs/status.json et docs/history.json mis a jour.")


if __name__ == "__main__":
    main()
