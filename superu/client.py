"""Client HTTP pour Super U Drive (coursesu.com) — Salesforce Commerce Cloud / Demandware.

Particularités vs Carrefour :
- Les données produit sont dans l'attribut HTML `data-tc-product-tile` (JSON HTML-encodé),
  pas dans un `__INITIAL_STATE__` Nuxt.
- Les prix ne sont déverrouillés qu'après sélection d'un magasin : naviguer vers
  `/drive-superu-{slug}` pose automatiquement le cookie `storeId`.
- Le nutriscore et la nutrition sont sur la page produit (pas d'endpoint JSON dédié fiable).
"""

import html as html_lib
import json
import logging
import re
import time
from urllib.parse import quote_plus

from curl_cffi import requests as cffi_requests

from superu.models import Product, Store
from superu.cache import Cache

logger = logging.getLogger(__name__)

BASE_URL = "https://www.coursesu.com"
SITE_PATH = "/on/demandware.store/Sites-DigitalU-Site/fr_FR"

# Délai entre requêtes pour éviter le rate limiting Cloudflare
REQUEST_DELAY = 2.0

SORT_RULES = {
    "relevance": "",
    "price_asc": "price-low-to-high",
    "price_desc": "price-high-to-low",
}

# Pattern pour extraire les tuiles produit (JSON dans un attribut HTML)
_TILE_RE = re.compile(r'data-tc-product-tile=(["\'])(.*?)\1', re.DOTALL)


class SuperUClient:
    def __init__(
        self,
        store_slug: str = "puteaux",
        stores: dict | None = None,
        cache_ttl_minutes: int = 30,
    ):
        # `stores` : presets nommés {cle: {name, slug, address}}
        self.stores = stores or {}
        self.store_slug = self._resolve_slug(store_slug)
        self.cache = Cache(ttl_minutes=cache_ttl_minutes)
        self._last_request_time = 0.0
        self._session_ready = False
        self._session = cffi_requests.Session(impersonate="chrome")

    def _resolve_slug(self, value: str) -> str:
        """Résout une clé de preset ou un nom de magasin vers son slug drive."""
        if not value:
            return value
        key = value.strip().lower()
        preset = self.stores.get(key)
        if preset and preset.get("slug"):
            return preset["slug"]
        # Sinon, chercher par nom de magasin
        for p in self.stores.values():
            if p.get("name", "").strip().lower() == key:
                return p["slug"]
        return value  # slug brut

    def set_store(self, value: str) -> str:
        """Change de magasin à chaud (preset, nom ou slug). Force une nouvelle session."""
        self.store_slug = self._resolve_slug(value)
        self._session_ready = False
        return self.store_slug

    def store_label(self) -> str:
        """Nom lisible du magasin actif (sinon le slug)."""
        for p in self.stores.values():
            if p.get("slug") == self.store_slug:
                return p.get("name", self.store_slug)
        return self.store_slug

    # ------------------------------------------------------------------ session
    def _init_session(self) -> None:
        """Initialise la session puis sélectionne le magasin pour déverrouiller les prix."""
        if self._session_ready:
            return
        logger.info("Initializing session (homepage + store selection)...")
        self._session.get(f"{BASE_URL}/drive/home", timeout=30)
        # Naviguer vers le slug magasin pose le cookie storeId automatiquement
        self._session.get(f"{BASE_URL}{self._store_path()}", timeout=30)
        store_id = self._session.cookies.get("storeId")
        logger.info(f"Store '{self.store_slug}' selected, storeId={store_id}")

        # Vérifier que le magasin est actif
        try:
            resp = self._session.get(f"{BASE_URL}{SITE_PATH}/Stores-GetStatus", timeout=30)
            status = resp.json()
            if status.get("storeNotActive"):
                logger.warning(f"Store '{self.store_slug}' not active, prices may be hidden")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not verify store status: {e}")

        self._session_ready = True
        time.sleep(1)

    # Préfixes d'enseignes U (un slug peut déjà les inclure)
    _STORE_PREFIXES = ("superu-", "hyperu-", "uexpress-", "marketu-", "u-")

    def _store_path(self) -> str:
        """Chemin de sélection du magasin (gère les slugs bruts ou préfixés)."""
        slug = self.store_slug
        if slug.startswith("/drive-"):
            return slug
        if any(slug.startswith(p) for p in self._STORE_PREFIXES):
            return f"/drive-{slug}"
        return f"/drive-superu-{slug}"

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self._last_request_time = time.time()

    def _get(self, url: str, retries: int = 2) -> str:
        self._init_session()
        self._throttle()
        logger.info(f"Fetching: {url}")
        resp = None
        for attempt in range(retries + 1):
            resp = self._session.get(url, timeout=30)
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

    # ------------------------------------------------------------------ search
    def search_products(
        self, query: str, max_results: int = 10, sort_by: str = "relevance"
    ) -> list[dict]:
        cache_key = f"search:{self.store_slug}:{query}:{max_results}:{sort_by}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            logger.info(f"Cache hit: {cache_key}")
            return cached

        srule = SORT_RULES.get(sort_by, "")
        url = (
            f"{BASE_URL}{SITE_PATH}/Search-Show"
            f"?q={quote_plus(query)}&start=0&sz={max_results}&format=ajax"
        )
        if srule:
            url += f"&srule={srule}"

        html = self._get(url)
        products = self._parse_tiles(html, max_results)
        result = [p.to_dict() for p in products]
        self.cache.set(cache_key, result)
        return result

    def compare_prices(self, product: str, store_keys: list[str] | None = None) -> dict:
        """Compare le prix d'un même produit entre plusieurs magasins.

        `product` peut être un id produit ou un terme de recherche (on prend alors
        le 1er résultat du magasin actif comme référence). L'id produit est
        commun à tous les magasins ; seul le prix change selon le cookie store.
        """
        if product.strip().isdigit():
            pid, name = product.strip(), None
        else:
            res = self.search_products(product, max_results=1)
            if not res:
                return {"product": product, "id": None, "results": []}
            pid, name = res[0]["id"], res[0]["name"]

        keys = store_keys or list(self.stores.keys())
        original = self.store_slug
        results = []
        for key in keys:
            self.set_store(key)
            details = self.get_product_details(pid)
            if details:
                name = name or details["name"]
                results.append({
                    "store": key,
                    "store_name": self.stores.get(key, {}).get("name", key),
                    "price": details["price"],
                    "price_per_unit": details["price_per_unit"],
                    "url": details["url"],
                })
        self.set_store(original)

        results.sort(key=lambda r: (r["price"] <= 0, r["price"]))
        return {"product": name or product, "id": pid, "results": results}

    def _parse_tiles(self, html: str, max_results: int) -> list[Product]:
        """Parse les tuiles produit depuis l'attribut `data-tc-product-tile`."""
        products: list[Product] = []
        seen: set[str] = set()

        for _, raw in _TILE_RE.findall(html):
            if len(products) >= max_results:
                break
            try:
                tile = json.loads(html_lib.unescape(raw))
            except json.JSONDecodeError:
                continue

            pid = str(tile.get("id", ""))
            if not pid or pid in seen:
                continue
            seen.add(pid)

            name = tile.get("name", "")
            brand = tile.get("brand", "")
            try:
                price = float(str(tile.get("price", "0")).replace(",", "."))
            except (ValueError, TypeError):
                price = 0.0

            discount = tile.get("discount") or ""
            promo = tile.get("promotion") or {}
            promo_name = promo.get("name") if isinstance(promo, dict) else None
            is_promo = bool(discount) or (promo_name not in (None, "", "unknown"))
            promo_detail = discount or (promo_name if promo_name not in ("unknown", "") else None)

            rating = tile.get("notation")
            rating = round(float(rating), 2) if isinstance(rating, (int, float)) else None

            category = " > ".join(
                c for c in (tile.get("product_cat1"), tile.get("product_cat2"), tile.get("product_cat3")) if c
            )
            haystack = f"{name} {brand} {category}".lower()

            products.append(Product(
                id=pid,
                name=name,
                brand=brand,
                price=price,
                price_per_unit="",
                image_url=tile.get("product_url_picture", ""),
                available=True,
                url=f"{SITE_PATH}/Product-Show?pid={pid}",
                ean=str(tile.get("EAN", "")) or None,
                category=category,
                is_bio="bio" in haystack,
                is_promo=is_promo,
                promo_detail=promo_detail,
                rating=rating,
            ))

        return products

    # ----------------------------------------------------------- product detail
    def get_product_details(self, product_id: str) -> dict | None:
        product_id = self._extract_pid(product_id)
        cache_key = f"product:{self.store_slug}:{product_id}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        url = f"{BASE_URL}{SITE_PATH}/Product-Show?pid={product_id}"
        html = self._get(url)
        product = self._parse_product_page(html, product_id)
        if product:
            result = product.to_dict()
            self.cache.set(cache_key, result)
            return result
        return None

    @staticmethod
    def _extract_pid(value: str) -> str:
        """Accepte un pid brut ou une URL Product-Show?pid=..."""
        m = re.search(r"pid=(\w+)", value)
        return m.group(1) if m else value.strip().strip("/").split("/")[-1]

    def _parse_product_page(self, html: str, pid: str) -> Product | None:
        # Nom : attribut data-name-link du h1 (propre et complet)
        name = self._first(html, r'<h1[^>]*data-name-link="([^"]+)"') \
            or self._first(html, r'<h1[^>]*>\s*([^<]+?)\s*</h1>')
        if not name:
            return None
        name = re.sub(r"\s+", " ", html_lib.unescape(name)).strip()

        # Prix de vente : attribut data-item-price (float propre)
        price = 0.0
        price_raw = self._first(html, r'data-item-price="([\d.]+)"') \
            or self._first(html, r'class="sale-price[^"]*"[^>]*>\s*([\d,]+)\s*&#8364;') \
            or self._first(html, r'pdp-standard-price-value">\s*([\d,\.]+)\s*&#8364;')
        if price_raw:
            try:
                price = float(price_raw.replace(",", "."))
            except ValueError:
                pass

        # Prix par unité (ex: "2,05 €/l") — la classe exacte est "unit-info su-font..."
        per_unit_raw = self._first(html, r'class="unit-info [^"]*"[^>]*>\s*([^<]+?)\s*</span>')
        price_per_unit = html_lib.unescape(per_unit_raw).strip() if per_unit_raw else ""

        # Marque
        brand = self._first(html, r'data-product-brand="([^"]+)"') \
            or self._first(html, r'class="[^"]*pdp-brand[^"]*"[^>]*>\s*([^<]+?)\s*<') or ""
        brand = html_lib.unescape(brand).strip()

        # EAN
        ean = self._first(html, r'\b(\d{13})\b')

        # Nutriscore (A-E) via le nom de fichier de l'icône
        ns = self._first(html, r'nutri_score_([A-E])') \
            or self._first(html, r'alt="Nutriscore\s+([A-E])"')

        # Image
        image_url = self._first(html, r'<img[^>]+class="[^"]*primary-image[^"]*"[^>]*src="([^"]+)"') \
            or self._first(html, r'property="og:image"\s+content="([^"]+)"') or ""

        nutrition = self._parse_nutrition(html)
        ingredients = self._parse_ingredients(html)
        haystack = f"{name} {brand}".lower()

        return Product(
            id=pid,
            name=name,
            brand=brand,
            price=price,
            price_per_unit=price_per_unit,
            image_url=image_url,
            available=True,
            url=f"{SITE_PATH}/Product-Show?pid={pid}",
            ean=ean,
            nutriscore=ns,
            is_bio="bio" in haystack,
            nutrition=nutrition or None,
            ingredients=ingredients,
        )

    @staticmethod
    def _parse_ingredients(html: str) -> str | None:
        """Extrait la liste d'ingrédients (paragraphe sous le libellé 'Ingrédients')."""
        i = html.find("Ingr&eacute;dients")
        if i == -1:
            i = html.find("Ingrédients")
        if i == -1:
            return None
        m = re.search(
            r"<p[^>]*>(.*?)</p>",
            html[i:i + 1500],
            re.DOTALL,
        )
        if not m:
            return None
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"^Ingr[ée]dients\s*:?\s*", "", text, flags=re.I).strip()
        return text or None

    @staticmethod
    def _parse_nutrition(html: str) -> dict:
        """Extrait les valeurs nutritionnelles, en couvrant les formats du site :

        - paragraphe "valeur après unité" : "Energie : 1510 kJ/358kcal, Protéines : 14 g ..."
        - paragraphe "unité avant valeur" : "Energie (kcal) : 366 ... Protéines (g) : 14 ..."
        - tableau (`pdp-nutritional-tabel`), parfois exprimé par portion et non /100 g.

        Retourne des champs structurés + `base` (ex: "100g" ou "portion 60g") + `resume`.
        """
        # 1. Paragraphe sous le titre "Valeurs nutritionnelles"
        idx = html.find("Valeurs nutritionnelles")
        if idx != -1:
            m = re.search(
                r'pdp-description-text"[^>]*>\s*(.*?)\s*</p>',
                html[idx:idx + 2000],
                re.DOTALL,
            )
            if m:
                text = html_lib.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
                if re.search(r"\d", text):
                    parsed = SuperUClient._parse_nutrition_text(text)
                    if parsed:
                        return parsed

        # 2. Tableau structuré (paires libellé / valeur)
        return SuperUClient._parse_nutrition_table(html)

    @staticmethod
    def _parse_nutrition_table(html: str) -> dict:
        i = html.find("pdp-nutritional-tabel")
        if i == -1:
            return {}
        end = html.find("Conseils", i)
        block = html[i:end if end != -1 else i + 12000]

        # Le tableau peut contenir plusieurs sections (ex: "portion 60g" ET "Pour 100 g")
        headers = list(re.finditer(r"data-nutritional-header>\s*([^<]+?)\s*<", block))
        sections: list[tuple[str, list]] = []
        for n, h in enumerate(headers):
            seg = block[h.end():headers[n + 1].start() if n + 1 < len(headers) else len(block)]
            label = html_lib.unescape(re.sub(r"\s+", " ", h.group(1))).strip()
            pairs = re.findall(
                r'class="name"[^>]*>\s*([^<]+?)\s*<.*?class="value"[^>]*>\s*(.*?)\s*</',
                seg,
                re.DOTALL,
            )
            sections.append((label, pairs))
        if not sections:
            return {}

        # Préférer la section "Pour 100 g" (comparable), sinon la première (portion)
        chosen = next(
            (s for s in sections if re.search(r"100\s*(g|ml)", s[0], re.I)),
            sections[0],
        )
        header, pairs = chosen
        parts = []
        for label, value in pairs:
            label = html_lib.unescape(re.sub(r"\s+", " ", label)).strip()
            value = html_lib.unescape(re.sub(r"\s+", " ", value)).strip()
            if label and value:
                parts.append(f"{label} : {value}")
        if not parts:
            return {}
        return SuperUClient._parse_nutrition_text(f"{header} {', '.join(parts)}")

    @staticmethod
    def _parse_nutrition_text(text: str) -> dict:
        """Parse un bloc texte nutritionnel en champs structurés."""
        def num(*patterns: str) -> str | None:
            for pat in patterns:
                mm = re.search(pat, text, re.IGNORECASE)
                if mm:
                    return mm.group(1).replace(",", ".")
            return None

        # Énergie : la valeur peut précéder ("358kcal") ou suivre ("(kcal) : 366") l'unité
        unit = r"(?:\([^)]*\))?\s*:?\s*"  # tolère un "(g)" / "(kJ)" et un ":" optionnels
        fields = {
            "energie_kcal": num(r"([\d.,]+)\s*kcal", r"kcal\s*\)?\s*:?\s*([\d.,]+)"),
            "energie_kj": num(r"([\d.,]+)\s*kJ", r"kJ\s*\)?\s*:?\s*([\d.,]+)"),
            "matieres_grasses_g": num(rf"(?:mati[èe]res?\s+grasses?|graisses?)\s*{unit}([\d.,]+)"),
            "acides_gras_satures_g": num(rf"satur[ée]s?\s*{unit}([\d.,]+)"),
            "glucides_g": num(rf"glucides\s*{unit}([\d.,]+)"),
            "sucres_g": num(rf"sucres?\s*{unit}([\d.,]+)"),
            "fibres_g": num(rf"fibres[^:0-9(]*{unit}([\d.,]+)"),
            "proteines_g": num(rf"prot[ée]ines?\s*{unit}([\d.,]+)"),
            "sel_g": num(rf"sel\s*{unit}([\d.,]+)"),
        }
        out = {k: v for k, v in fields.items() if v}
        if not out:
            return {}
        if re.search(r"portion", text, re.I):
            mg = re.search(r"portion\s+de\s+([\d.,]+)\s*g", text, re.I)
            out["base"] = f"portion {mg.group(1).replace(',', '.')}g" if mg else "portion"
        elif re.search(r"100\s*(g|ml)", text, re.I) or re.search(r"pour\s*100", text, re.I):
            out["base"] = "100g"
        else:
            out["base"] = "?"

        # Normalisation /100 g quand la base est une portion (pour comparaisons fiables)
        grams = re.match(r"portion ([\d.]+)g", out["base"])
        if grams and float(grams.group(1)) > 0:
            factor = 100 / float(grams.group(1))
            per100 = {}
            for k, v in fields.items():
                if v and (k.endswith("_kcal") or k.endswith("_kj") or k.endswith("_g")):
                    try:
                        per100[k] = round(float(v) * factor, 1)
                    except ValueError:
                        pass
            if per100:
                out["per_100g"] = per100

        out["resume"] = text
        return out

    @staticmethod
    def _first(html: str, pattern: str) -> str | None:
        if not html:
            return None
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        return m.group(1) if m else None

    # ------------------------------------------------------------------ stores
    def find_stores(self, postal_code: str, max_results: int = 15) -> list[dict]:
        """Recherche dans la liste nationale des drives, filtrée par code postal.

        (L'endpoint StoreLocator-GetClosestStores ignore le paramètre d'adresse ;
        on s'appuie donc sur /drive-france qui liste tous les magasins drive.)
        """
        all_stores = self._all_stores()
        query = postal_code.strip().lower()
        matches = [s for s in all_stores if query in s["address"].lower()]
        if not matches:
            # Repli sur le préfixe de département (2 premiers chiffres)
            dept = query[:2]
            matches = [s for s in all_stores if dept and re.search(rf"\b{dept}\d{{3}}\b", s["address"])]
        return matches[:max_results]

    def _all_stores(self) -> list[dict]:
        cache_key = "all_stores"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        html = self._get(f"{BASE_URL}/drive-france")
        result = [s.to_dict() for s in self._parse_stores(html)]
        self.cache.set(cache_key, result)
        return result

    @staticmethod
    def _parse_stores(html: str) -> list[Store]:
        stores: list[Store] = []
        # Découper par bloc magasin
        blocks = re.split(r'data-wrapper-store-details', html)
        for block in blocks[1:]:
            store_id = SuperUClient._first(block, r'data-store-id="([^"]+)"') or ""
            name = SuperUClient._first(block, r'data-store-name[^>]*>\s*([^<]+?)\s*<') or ""
            address = SuperUClient._first(block, r'stores-text-address[^>]*>\s*([^<]+?)\s*<') or ""
            slug = SuperUClient._first(block, r'href="https://www\.coursesu\.com/drive-([\w-]+)"') or ""
            name = html_lib.unescape(name).strip()
            address = html_lib.unescape(address).strip()
            if name:
                stores.append(Store(
                    id=store_id,
                    name=name,
                    address=address,
                    drive_slug=slug,
                ))
        return stores

    # -------------------------------------------------------------- promotions
    def get_promotions(self, category: str, max_results: int = 15) -> list[dict]:
        """Promotions par catégorie — basé sur la recherche + filtre discount."""
        results = self.search_products(category, max_results=max_results, sort_by="relevance")
        promos = [p for p in results if p.get("is_promo")]
        return promos or results

    def close(self) -> None:
        self._session.close()
