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


def find_matching_product(products: list[dict], keywords: list[str]) -> tuple[dict, str] | None:
    for product in products:
        name = product.get("name") or ""
        squashed_name = squash(name)
        for kw in keywords:
            if squash(kw) in squashed_name:
                return product, kw
    return None


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
            "price": price,
            "currency": "EUR" if price is not None else None,
            "confidence": "heuristique",
        }
    return {
        "status": "non_trouve",
        "matched_keyword": "",
        "price": None,
        "currency": None,
        "confidence": "heuristique",
    }


def analyze(html: str, keywords: list[str]) -> dict:
    products = extract_structured_products(html)
    match = find_matching_product(products, keywords)
    if match:
        product, kw = match
        price, currency, availability = product_price_and_availability(product)
        status = availability_to_status(availability)
        # Si on n'a ni prix ni statut exploitable (ex: produit "reserve
        # magasin" sans bloc offers), les donnees structurees n'apportent
        # rien de plus que le repli heuristique -> on bascule dessus.
        if status is not None or price is not None:
            return {
                "status": status or "a_verifier",
                "matched_keyword": kw,
                "price": price,
                "currency": currency or ("EUR" if price is not None else None),
                "confidence": "structure",
            }
    return analyze_heuristic(html, keywords)


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
                outcome = {
                    "status": "erreur", "matched_keyword": "",
                    "price": None, "currency": None, "confidence": "heuristique",
                }
            else:
                outcome = analyze(html, target["keywords"])
            results.append({
                "site": watch["site"],
                "url": watch["url"],
                "target_key": target_key,
                "target_label": target["label"],
                "status": outcome["status"],
                "matched_keyword": outcome["matched_keyword"],
                "price": outcome["price"],
                "currency": outcome["currency"],
                "confidence": outcome["confidence"],
                "checked_at": checked_at,
            })
            price_str = f" - {outcome['price']} {outcome['currency']}" if outcome["price"] else ""
            print(f"  - {target['label']}: {outcome['status']} ({outcome['confidence']}){price_str}")
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
