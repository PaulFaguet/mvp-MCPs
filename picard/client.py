"""Client HTTP pour Picard (picard.fr) — Salesforce Commerce Cloud / Demandware.

Particularités (vérifiées en live) :
- Catalogue 100 % Picard, prix NATIONAL uniforme : aucune sélection de magasin,
  aucun cookie store, aucun CSRF nécessaire (le plus simple des enseignes).
- Chaque tuile produit (recherche / catégorie) expose un attribut HTML `data-gtm`
  contenant un JSON complet (item_id, prix, nutriscore, planetscore, note, origine,
  format, catégories, bannière promo, disponibilité) — pas besoin de visiter la
  fiche pour ces champs.
- L'endpoint AJAX qui renvoie la grille produit est `Search-UpdateGrid` (sous le
  path Demandware). Le `/Search-Show` nu renvoie 410.
- Prix/kg et valeurs nutritionnelles ne sont QUE sur la fiche produit
  (`Product-Show?pid=...`, qui redirige vers /produits/{slug}-{id}.html).
"""

import html as html_lib
import json
import logging
import re
import time
from urllib.parse import quote_plus

from curl_cffi import requests as cffi_requests

from picard.models import NutritionFacts, Product, Store
from picard.cache import Cache

logger = logging.getLogger(__name__)

BASE_URL = "https://www.picard.fr"
SITE_PATH = "/on/demandware.store/Sites-picard-Site/fr_FR"

# Picard n'a pas de Cloudflare agressif : 1 s entre requêtes suffit.
REQUEST_DELAY = 1.0

SORT_RULES = {
    "relevance": "",
    "price_asc": "du-moins-cher-au-plus-cher",
    "price_desc": "du-plus-cher-au-moins-cher",
    "rating": "les-mieux-notes",
    "reviews": "les-plus-commentes",
}

# Alias conviviaux -> cgid réellement valides (vérifiés en live).
CATEGORIES = {
    "feculents": "feculents",
    "legumes": "legumes",
    "legumes-fruits": "legumes",        # alias du plan
    "fruits-legumes": "legumes",
    "plats-cuisines": "plats-cuisines",
    "plats": "plats-cuisines",
    "poissons": "poissons",
    "poissons-crustaces": "poissons",
    "viandes": "viandes",
    "desserts": "desserts",
    "glaces": "desserts",
    "petit-dejeuner": "petit-dejeuner",
    "epicerie": "epicerie",
    "aperitifs": "aperitifs",
    "apero": "aperitifs",
    "apero-entrees": "aperitifs",       # alias du plan
    "entrees": "entrees",
    "soupes": "soupes",
    "sans-gluten": "sans-gluten",
    "pains-viennoiseries": "pains-viennoiseries",
    "boulangerie": "pains-viennoiseries",
    "pizzas": "pizzas",
    "promotions": "promotions",
    "promos": "promotions",
    "meilleures-ventes": "meilleures-ventes",
}

# Champs triables par compare_nutrition -> (attribut NutritionFacts, décroissant ?)
NUTRITION_SORT = {
    "proteines": ("proteins_100g", True),
    "fibres": ("fibers_100g", True),
    "kcal": ("kcal_100g", False),
    "lipides": ("fats_100g", False),
    "glucides": ("carbs_100g", False),
    "sucres": ("sugars_100g", False),
    "sel": ("salt_100g", False),
    "prix_kg": ("_price_per_kg_num", False),
}

# data-gtm="...json html-encodé..."
_GTM_RE = re.compile(r'data-gtm="([^"]+)"')


class PicardClient:
    def __init__(self, cache_ttl_minutes: int = 30, request_delay: float = REQUEST_DELAY):
        self.cache = Cache(ttl_minutes=cache_ttl_minutes)
        self.request_delay = request_delay
        self._last_request_time = 0.0
        self._session_ready = False
        self._session = cffi_requests.Session(impersonate="chrome")

    # ------------------------------------------------------------------ session
    def _init_session(self) -> None:
        if self._session_ready:
            return
        logger.info("Initializing Picard session (homepage)...")
        try:
            self._session.get(f"{BASE_URL}/", timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Homepage warmup failed (continuing): {e}")
        self._session_ready = True

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self._last_request_time = time.time()

    def _get(self, url: str, retries: int = 2) -> str:
        self._init_session()
        self._throttle()
        logger.info(f"Fetching: {url}")
        resp = None
        for attempt in range(retries + 1):
            resp = self._session.get(url, timeout=30, allow_redirects=True)
            if resp.status_code in (403, 429):
                wait = 3 * (attempt + 1)
                logger.warning(f"{resp.status_code} on attempt {attempt + 1}, waiting {wait}s...")
                time.sleep(wait)
                self._session_ready = False
                self._init_session()
                continue
            if resp.status_code >= 400:
                logger.error(f"HTTP {resp.status_code} for {url}")
                break
            return resp.text
        raise RuntimeError(
            f"Failed to fetch {url} after {retries + 1} attempts "
            f"(last status: {resp.status_code if resp else 'n/a'})"
        )

    # ------------------------------------------------------------------ grille
    def _grid_url(self, base_qs: str, start: int, sz: int) -> str:
        return f"{BASE_URL}{SITE_PATH}/Search-UpdateGrid?{base_qs}&start={start}&sz={sz}&format=ajax"

    def _fetch_grid(self, base_qs: str, max_results: int) -> list[Product]:
        """Récupère une grille produit avec pagination automatique."""
        products: list[Product] = []
        seen: set[str] = set()
        page = min(max(max_results, 1), 48)
        start = 0
        while len(products) < max_results:
            html = self._get(self._grid_url(base_qs, start, page))
            tiles = self._parse_gtm_tiles(html)
            new = 0
            for p in tiles:
                if p.id in seen:
                    continue
                seen.add(p.id)
                products.append(p)
                new += 1
                if len(products) >= max_results:
                    break
            if new == 0:  # plus rien à paginer
                break
            start += page
        return products[:max_results]

    def search_products(
        self,
        query: str,
        max_results: int = 10,
        sort_by: str = "relevance",
        nutriscore_filter: str | None = None,
    ) -> list[dict]:
        ns = (nutriscore_filter or "").strip().upper()
        cache_key = f"search:{query}:{max_results}:{sort_by}:{ns}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            logger.info(f"Cache hit: {cache_key}")
            return cached

        qs = f"q={quote_plus(query)}&prefn1=category-id&prefv1=rayons"
        srule = SORT_RULES.get(sort_by, "")
        if srule:
            qs += f"&srule={srule}"
        if ns in ("A", "B", "C", "D", "E"):
            qs += f"&prefn2=nutriscore2&prefv2={ns}"

        products = self._fetch_grid(qs, max_results)
        result = [p.to_dict() for p in products]
        self.cache.set(cache_key, result)
        return result

    def browse_category(
        self,
        category: str,
        sort_by: str = "relevance",
        nutriscore_filter: str | None = None,
        max_results: int = 20,
    ) -> list[dict]:
        cgid = CATEGORIES.get(category.strip().lower(), category.strip().lower())
        ns = (nutriscore_filter or "").strip().upper()
        cache_key = f"category:{cgid}:{max_results}:{sort_by}:{ns}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            logger.info(f"Cache hit: {cache_key}")
            return cached

        qs = f"cgid={cgid}"
        srule = SORT_RULES.get(sort_by, "")
        if srule:
            qs += f"&srule={srule}"
        if ns in ("A", "B", "C", "D", "E"):
            qs += f"&prefn1=nutriscore2&prefv1={ns}"

        products = self._fetch_grid(qs, max_results)
        result = [p.to_dict() for p in products]
        self.cache.set(cache_key, result)
        return result

    def get_promotions(self, max_results: int = 20) -> list[dict]:
        return self.browse_category("promotions", max_results=max_results)

    @staticmethod
    def available_categories() -> list[str]:
        # Valeurs canoniques (sans les alias), dédupliquées en gardant l'ordre.
        seen, out = set(), []
        for v in CATEGORIES.values():
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    # ------------------------------------------------------------------ parsing tuiles
    def _parse_gtm_tiles(self, html: str) -> list[Product]:
        products: list[Product] = []
        seen: set[str] = set()
        for raw in _GTM_RE.findall(html):
            decoded = html_lib.unescape(raw)
            if "item_id" not in decoded:
                continue
            try:
                obj = json.loads(decoded)
            except json.JSONDecodeError:
                continue
            item = self._unescape_item(self._find_item(obj))
            if not item:
                continue
            pid = str(item.get("item_id", "")).strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            products.append(self._item_to_product(item))
        return products

    @staticmethod
    def _unescape_item(item: dict | None) -> dict:
        """Dé-échappe les entités HTML (&#160;, &eacute;...) dans les valeurs str du GTM."""
        if not item:
            return {}
        return {
            k: (html_lib.unescape(v).replace("\xa0", " ") if isinstance(v, str) else v)
            for k, v in item.items()
        }

    @staticmethod
    def _find_item(obj) -> dict | None:
        """Trouve récursivement le dict porteur de 'item_id' dans un payload GTM."""
        if isinstance(obj, dict):
            if "item_id" in obj:
                return obj
            for v in obj.values():
                found = PicardClient._find_item(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = PicardClient._find_item(v)
                if found:
                    return found
        return None

    @staticmethod
    def _item_to_product(item: dict) -> Product:
        pid = str(item.get("item_id", "")).strip()
        try:
            price = float(str(item.get("price", 0)).replace(",", ".") or 0)
        except (ValueError, TypeError):
            price = 0.0

        rating = item.get("item_average_rating")
        try:
            rating = round(float(rating), 2) if rating not in (None, "", "0") else None
        except (ValueError, TypeError):
            rating = None

        promo = item.get("item_promo_banner") or None

        return Product(
            id=pid,
            name=item.get("item_name", "").strip(),
            price=price,
            format=item.get("item_format", "") or "",
            nutriscore=PicardClient._norm_score(item.get("item_nutriscore")),
            planetscore=PicardClient._norm_score(item.get("item_planetscore")),
            rating=rating,
            origin=item.get("item_origin", "") or "",
            category=item.get("item_category", "") or "",
            subcategory=item.get("item_category2", "") or "",
            brand=item.get("item_brand", "") or "",
            label=item.get("item_label", "") or "",
            promo=promo,
            available=(item.get("item_availability", "").lower() == "en stock"),
            url=f"{SITE_PATH}/Product-Show?pid={pid}",
        )

    @staticmethod
    def _norm_score(value) -> str | None:
        """Normalise un score 'b' / 'd,c' -> 'B' / 'D' (1ère lettre a-e)."""
        if not value:
            return None
        m = re.search(r"[a-eA-E]", str(value))
        return m.group(0).upper() if m else None

    # ----------------------------------------------------------- fiche produit
    def get_product_details(self, product_id: str) -> dict | None:
        pid = self._extract_pid(product_id)
        cache_key = f"product:{pid}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        url = f"{BASE_URL}{SITE_PATH}/Product-Show?pid={pid}"
        html = self._get(url)
        product = self._parse_product_page(html, pid)
        if product:
            result = product.to_dict()
            self.cache.set(cache_key, result)
            return result
        return None

    @staticmethod
    def _extract_pid(value: str) -> str:
        """Accepte un pid brut, une URL Product-Show?pid=..., ou /produits/...-{id}.html."""
        value = value.strip()
        m = re.search(r"pid=(\d+)", value)
        if m:
            return m.group(1)
        m = re.search(r"(\d{6,18})\.html", value)
        if m:
            return m.group(1)
        return value.strip("/").split("/")[-1]

    def _parse_product_page(self, html: str, pid: str) -> Product | None:
        # Le data-gtm de la fiche porte la majorité des champs (fiable).
        gtm: dict = {}
        for raw in _GTM_RE.findall(html):
            decoded = html_lib.unescape(raw)
            if "item_id" in decoded:
                try:
                    gtm = self._unescape_item(self._find_item(json.loads(decoded)))
                except json.JSONDecodeError:
                    gtm = {}
                if gtm:
                    break

        name = (gtm.get("item_name") or "").strip()
        if not name:
            name = self._first(html, r"<h1[^>]*>\s*(.*?)\s*</h1>") or ""
            name = re.sub(r"<[^>]+>", " ", name)
            name = re.sub(r"\s+", " ", html_lib.unescape(name)).strip()
        if not name:
            return None

        # Prix : meta itemprop (float propre), repli sur le data-gtm
        price = 0.0
        price_raw = self._first(html, r'itemprop="price"\s+content="([\d.]+)"')
        if price_raw:
            try:
                price = float(price_raw)
            except ValueError:
                pass
        if price == 0.0 and gtm.get("price"):
            try:
                price = float(str(gtm["price"]).replace(",", "."))
            except (ValueError, TypeError):
                pass

        # Prix/kg : texte après le libellé sr-only "Prix au kilo/litre" -> "8,00 €/kg".
        # (Cibler la classe seule matche la définition CSS inline, pas le vrai div.)
        price_per_kg = ""
        kg_raw = self._first(html, r"Prix au [^<]*</span>\s*([^<]+)")
        if kg_raw:
            price_per_kg = re.sub(r"\s+", " ", html_lib.unescape(kg_raw)).strip()

        # nutriscore / planetscore : data-gtm d'abord, repli sur le SVG
        nutriscore = self._norm_score(gtm.get("item_nutriscore")) \
            or self._norm_score(self._first(html, r"#nutriscore-([a-e])"))
        planetscore = self._norm_score(gtm.get("item_planetscore")) \
            or self._norm_score(self._first(html, r"#planetscore-([a-e])"))

        rating = gtm.get("item_average_rating")
        try:
            rating = round(float(rating), 2) if rating not in (None, "", "0") else None
        except (ValueError, TypeError):
            rating = None

        nutrition = self._parse_nutrition(html)
        ingredients = self._parse_ingredients(html)

        return Product(
            id=pid,
            name=name,
            price=price,
            price_per_kg=price_per_kg,
            format=gtm.get("item_format", "") or "",
            nutriscore=nutriscore,
            planetscore=planetscore,
            rating=rating,
            origin=gtm.get("item_origin", "") or "",
            category=gtm.get("item_category", "") or "",
            subcategory=gtm.get("item_category2", "") or "",
            brand=gtm.get("item_brand", "") or "",
            label=gtm.get("item_label", "") or "",
            promo=gtm.get("item_promo_banner") or None,
            available=(gtm.get("item_availability", "").lower() == "en stock") if gtm else True,
            url=f"{SITE_PATH}/Product-Show?pid={pid}",
            nutrition=nutrition,
            ingredients=ingredients,
        )

    @staticmethod
    def _parse_nutrition(html: str) -> NutritionFacts | None:
        """Parse la table nutritionnelle. La colonne « Pour 100 g » est la 1ʳᵉ
        colonne de valeurs, donc le 1er nombre rencontré par nutriment = la valeur
        /100 g (la colonne « par portion » vient après dans le flux)."""
        i = html.find("pi-ProductTabsNutrition-table")
        if i == -1:
            return None
        j = html.find("</table>", i)
        block = html[i: j if j != -1 else i + 12000]
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", block))
        text = re.sub(r"\s+", " ", text)

        def num(*patterns: str) -> float | None:
            for pat in patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    try:
                        return float(m.group(1).replace(",", "."))
                    except ValueError:
                        continue
            return None

        nf = NutritionFacts(
            kj_100g=num(r"([\d.,]+)\s*kJ"),
            kcal_100g=num(r"([\d.,]+)\s*kcal"),
            # ':' obligatoire pour ces 4 -> saute les libellés d'en-tête sans valeur
            fats_100g=num(r"mati[èe]res?\s+grasses?\s*:\s*([\d.,]+)"),
            saturated_fats_100g=num(r"satur[ée]s?\s*:\s*([\d.,]+)"),
            carbs_100g=num(r"glucides\s*:\s*([\d.,]+)"),
            sugars_100g=num(r"sucres?\s*:\s*([\d.,]+)"),
            # ces 3 lignes n'ont pas de ':' (libellé puis valeur directement)
            fibers_100g=num(r"fibres[^\d:%<]*[:<]?\s*([\d.,]+)\s*g"),
            proteins_100g=num(r"prot[ée]ines?\s*[:<]?\s*([\d.,]+)\s*g"),
            # tolère "Sel < 0,01 g" (valeur trace) -> 0.01
            salt_100g=num(r"\bsel\s*[:<]?\s*([\d.,]+)\s*g"),
        )
        return None if nf.is_empty() else nf

    @staticmethod
    def _parse_ingredients(html: str) -> str | None:
        """Extrait la liste d'ingrédients du panneau « Ingrédients et allergènes »."""
        i = html.find('id="tab2"')
        if i == -1:
            i = html.find("Liste des ingr")
        if i == -1:
            return None
        seg = html[i: i + 4000]
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", seg))
        text = re.sub(r"\s+", " ", text).strip()
        # On garde à partir de "Liste des ingrédients" si présent, sinon le bloc.
        m = re.search(r"Liste des ingr[ée]dients\s*(.*?)(?:Peut contenir|Valeurs nutri|$)", text, re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()
        m = re.search(r"D[ée]nomination[^:]*\s*(.*?)(?:Valeurs nutri|$)", text, re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()[:1500] or None
        return None

    @staticmethod
    def _first(html: str, pattern: str) -> str | None:
        if not html:
            return None
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        return m.group(1) if m else None

    # ------------------------------------------------------- compare_nutrition
    def compare_nutrition(self, product_ids: list[str], sort_by_field: str = "proteines") -> dict:
        """Récupère les fiches de plusieurs produits et les trie par un champ nutri/prix."""
        attr, descending = NUTRITION_SORT.get(sort_by_field.strip().lower(), ("proteins_100g", True))

        rows: list[dict] = []
        for raw_id in product_ids:
            details = self.get_product_details(raw_id)
            if not details:
                continue
            nutri = details.get("nutrition") or {}
            price_kg_num = self._kg_to_float(details.get("price_per_kg", ""))
            row = {
                "id": details["id"],
                "name": details["name"],
                "price": details["price"],
                "price_per_kg": details.get("price_per_kg", ""),
                "nutriscore": details.get("nutriscore"),
                "url": details.get("url", ""),
                "kcal_100g": nutri.get("kcal_100g"),
                "proteins_100g": nutri.get("proteins_100g"),
                "carbs_100g": nutri.get("carbs_100g"),
                "sugars_100g": nutri.get("sugars_100g"),
                "fats_100g": nutri.get("fats_100g"),
                "fibers_100g": nutri.get("fibers_100g"),
                "salt_100g": nutri.get("salt_100g"),
                "_price_per_kg_num": price_kg_num,
            }
            rows.append(row)

        def sort_key(r: dict):
            v = r.get(attr)
            # Les valeurs manquantes finissent toujours en bas.
            if v is None:
                return (1, 0.0)
            return (0, -v if descending else v)

        rows.sort(key=sort_key)
        for r in rows:
            r.pop("_price_per_kg_num", None)

        return {
            "sorted_by": sort_by_field,
            "order": "décroissant" if descending else "croissant",
            "count": len(rows),
            "products": rows,
        }

    @staticmethod
    def _kg_to_float(price_per_kg: str) -> float | None:
        if not price_per_kg:
            return None
        m = re.search(r"([\d.,]+)", price_per_kg)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None

    # ------------------------------------------------------------------ stores
    def find_stores(self, postal_code: str, radius: int = 20) -> list[dict]:
        """Best-effort : Picard a un prix national uniforme, l'API magasins du
        front ne renvoie pas de résultats fiables. On tente l'endpoint et on
        renvoie ce qu'on peut parser (souvent vide -> le serveur renvoie le lien
        officiel magasins.picard.fr)."""
        cp = quote_plus(postal_code.strip())
        url = (
            f"{BASE_URL}{SITE_PATH}/Stores-NearStores"
            f"?postalCode={cp}&address={cp}&radius={radius}"
        )
        try:
            html = self._get(url)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"find_stores failed: {e}")
            return []
        stores: list[Store] = []
        for m in re.finditer(
            r'pi-AccountStoreSearch-storeName[^>]*>\s*([^<]+?)\s*<', html
        ):
            name = html_lib.unescape(m.group(1)).strip()
            if name:
                stores.append(Store(id="", name=name, address=""))
        return [s.to_dict() for s in stores]

    def close(self) -> None:
        self._session.close()
