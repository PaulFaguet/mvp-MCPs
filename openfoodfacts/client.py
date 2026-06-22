"""Client HTTP pour Open Food Facts (openfoodfacts.org) — base de données ouverte.

Particularités :
- API REST publique, gratuite, sans clé d'authentification.
- Product API v2 : `/api/v2/product/{barcode}.json` -> fiche complète (nutrition,
  nutriscore, NOVA, eco-score, additifs, allergènes, ingrédients).
- Search (legacy CGI, stable) : `/cgi/search.pl?search_terms=...&json=1`.
- OFF demande un User-Agent descriptif (nom d'app + contact/URL). Rate limit doux :
  15 req/min en lecture produit, 10 req/min en recherche -> cache 24 h + throttle.
"""

import logging
import time
from urllib.parse import quote_plus

from curl_cffi import requests as cffi_requests

from openfoodfacts.models import NutritionFacts, Product
from openfoodfacts.cache import Cache

logger = logging.getLogger(__name__)

USER_AGENT = "mvp-mcps-openfoodfacts/0.1 (https://github.com/PaulFaguet/mcp-openfoodfacts)"

# Champs demandés à l'API (limiter la charge réseau et le bruit).
PRODUCT_FIELDS = ",".join([
    "code", "product_name", "product_name_fr", "brands", "quantity",
    "nutriscore_grade", "nutrition_grades", "nova_group", "ecoscore_grade",
    "categories", "labels", "allergens_tags", "additives_tags",
    "nutrient_levels", "nutriments", "ingredients_text_fr", "ingredients_text",
    "image_url", "image_front_url",
])

# Champs triables par compare_products -> (attribut NutritionFacts, décroissant ?)
NUTRITION_SORT = {
    "proteines": ("proteins_100g", True),
    "fibres": ("fibers_100g", True),
    "kcal": ("kcal_100g", False),
    "lipides": ("fats_100g", False),
    "glucides": ("carbs_100g", False),
    "sucres": ("sugars_100g", False),
    "sel": ("salt_100g", False),
    "nutriscore": ("_nutriscore_num", False),
}

_GRADE_TO_NUM = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}


class OpenFoodFactsClient:
    def __init__(
        self,
        cache_ttl_minutes: int = 1440,
        request_delay: float = 1.0,
        country: str = "world",
    ):
        self.cache = Cache(ttl_minutes=cache_ttl_minutes)
        self.request_delay = request_delay
        self.country = (country or "world").strip().lower()
        self._last_request_time = 0.0
        self._session = cffi_requests.Session()

    @property
    def _base(self) -> str:
        return f"https://{self.country}.openfoodfacts.org"

    # ------------------------------------------------------------------ réseau
    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self._last_request_time = time.time()

    def _get_json(self, url: str, retries: int = 2) -> dict | None:
        self._throttle()
        logger.info(f"Fetching: {url}")
        resp = None
        for attempt in range(retries + 1):
            try:
                resp = self._session.get(
                    url, timeout=30, headers={"User-Agent": USER_AGENT}
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Request error (attempt {attempt + 1}): {e}")
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == retries:
                    logger.error(f"HTTP {resp.status_code} for {url} (gave up)")
                    return None
                wait = 3 * (attempt + 1)
                logger.warning(f"HTTP {resp.status_code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                logger.error(f"HTTP {resp.status_code} for {url}")
                return None
            try:
                return resp.json()
            except Exception as e:  # noqa: BLE001
                logger.error(f"JSON decode failed: {e}")
                return None
        return None

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _norm_grade(value) -> str | None:
        """Normalise un grade Nutri-Score / Eco-Score : 'a'..'e' -> 'A'..'E'.
        'unknown', 'not-applicable', '' -> None."""
        if not value:
            return None
        v = str(value).strip().lower()
        return v.upper() if v in ("a", "b", "c", "d", "e") else None

    @staticmethod
    def _norm_nova(value) -> int | None:
        try:
            n = int(value)
            return n if 1 <= n <= 4 else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _clean_tags(tags, upper: bool = False) -> list[str]:
        """Retire le préfixe de langue d'un tag OFF ('en:milk' -> 'milk')."""
        out = []
        for t in tags or []:
            label = str(t).split(":", 1)[-1].strip()
            if label:
                out.append(label.upper() if upper else label)
        return out

    @classmethod
    def _parse_product(cls, p: dict, fallback_code: str = "") -> Product:
        nutr = p.get("nutriments", {}) or {}

        def num(*keys: str) -> float | None:
            for key in keys:
                v = nutr.get(key)
                if v in (None, ""):
                    continue
                try:
                    return float(v)
                except (ValueError, TypeError):
                    continue
            return None

        nf = NutritionFacts(
            kcal_100g=num("energy-kcal_100g", "energy-kcal"),
            kj_100g=num("energy-kj_100g", "energy-kj"),
            proteins_100g=num("proteins_100g"),
            carbs_100g=num("carbohydrates_100g"),
            sugars_100g=num("sugars_100g"),
            fats_100g=num("fat_100g"),
            saturated_fats_100g=num("saturated-fat_100g"),
            fibers_100g=num("fiber_100g", "fibers_100g"),
            salt_100g=num("salt_100g"),
            sodium_100g=num("sodium_100g"),
        )

        return Product(
            code=str(p.get("code") or fallback_code or "").strip(),
            name=(p.get("product_name_fr") or p.get("product_name") or "").strip(),
            brands=(p.get("brands") or "").strip(),
            quantity=(p.get("quantity") or "").strip(),
            nutriscore=cls._norm_grade(p.get("nutriscore_grade") or p.get("nutrition_grades")),
            nova_group=cls._norm_nova(p.get("nova_group")),
            ecoscore=cls._norm_grade(p.get("ecoscore_grade")),
            categories=(p.get("categories") or "").strip(),
            labels=(p.get("labels") or "").strip(),
            allergens=cls._clean_tags(p.get("allergens_tags")),
            additives=cls._clean_tags(p.get("additives_tags"), upper=True),
            nutrient_levels=p.get("nutrient_levels", {}) or {},
            ingredients_text=(p.get("ingredients_text_fr") or p.get("ingredients_text") or "").strip(),
            image_url=(p.get("image_url") or p.get("image_front_url") or "").strip(),
            nutrition=None if nf.is_empty() else nf,
        )

    @staticmethod
    def _clean_barcode(value: str) -> str:
        """Accepte un code brut ou une URL OFF -> ne garde que les chiffres."""
        value = str(value).strip()
        if "/" in value:
            value = value.rstrip("/").split("/")[-1]
        digits = "".join(c for c in value if c.isdigit())
        return digits or value

    # ------------------------------------------------------------------ produit
    def get_product(self, barcode: str) -> dict | None:
        code = self._clean_barcode(barcode)
        if not code:
            return None
        cache_key = f"product:{self.country}:{code}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            logger.info(f"Cache hit: {cache_key}")
            return cached

        url = f"{self._base}/api/v2/product/{code}.json?fields={PRODUCT_FIELDS}"
        data = self._get_json(url)
        if not data or data.get("status") != 1 or not data.get("product"):
            return None
        product = self._parse_product(data["product"], code)
        if not product.name and not product.nutrition:
            return None
        result = product.to_dict()
        self.cache.set(cache_key, result)
        return result

    # ------------------------------------------------------------------ recherche
    def search_products(self, query: str, max_results: int = 10) -> list[dict]:
        max_results = min(max(max_results, 1), 40)
        cache_key = f"search:{self.country}:{query}:{max_results}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            logger.info(f"Cache hit: {cache_key}")
            return cached

        url = (
            f"{self._base}/cgi/search.pl?search_terms={quote_plus(query)}"
            f"&search_simple=1&action=process&json=1&page_size={max_results}"
            f"&fields={PRODUCT_FIELDS}"
        )
        data = self._get_json(url)
        if not data:
            return []
        products = [
            self._parse_product(p, p.get("code", ""))
            for p in data.get("products", [])
        ]
        result = [p.to_dict() for p in products if p.name or p.code]
        self.cache.set(cache_key, result)
        return result

    # ------------------------------------------------------------------ comparaison
    def compare_products(self, barcodes: list[str], sort_by_field: str = "nutriscore") -> dict:
        """Récupère plusieurs fiches et les trie par un champ nutritionnel."""
        attr, descending = NUTRITION_SORT.get(
            sort_by_field.strip().lower(), ("_nutriscore_num", False)
        )

        rows: list[dict] = []
        for code in barcodes:
            details = self.get_product(code)
            if not details:
                continue
            nutri = details.get("nutrition") or {}
            grade = details.get("nutriscore")
            row = {
                "code": details["code"],
                "name": details["name"],
                "brands": details.get("brands", ""),
                "nutriscore": grade,
                "nova_group": details.get("nova_group"),
                "url": details.get("url", ""),
                "kcal_100g": nutri.get("kcal_100g"),
                "proteins_100g": nutri.get("proteins_100g"),
                "carbs_100g": nutri.get("carbs_100g"),
                "sugars_100g": nutri.get("sugars_100g"),
                "fats_100g": nutri.get("fats_100g"),
                "fibers_100g": nutri.get("fibers_100g"),
                "salt_100g": nutri.get("salt_100g"),
                "_nutriscore_num": _GRADE_TO_NUM.get(grade) if grade else None,
            }
            rows.append(row)

        def sort_key(r: dict):
            v = r.get(attr)
            if v is None:
                return (1, 0.0)
            return (0, -v if descending else v)

        rows.sort(key=sort_key)
        for r in rows:
            r.pop("_nutriscore_num", None)

        return {
            "sorted_by": sort_by_field,
            "order": "décroissant" if descending else "croissant",
            "count": len(rows),
            "products": rows,
        }

    def close(self) -> None:
        self._session.close()
