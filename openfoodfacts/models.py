"""Modèles de données Open Food Facts."""

from dataclasses import dataclass, field

BASE_URL = "https://world.openfoodfacts.org"


@dataclass
class NutritionFacts:
    """Valeurs nutritionnelles pour 100 g (ou 100 ml), telles que fournies par OFF."""

    kcal_100g: float | None = None
    kj_100g: float | None = None
    proteins_100g: float | None = None
    carbs_100g: float | None = None
    sugars_100g: float | None = None
    fats_100g: float | None = None
    saturated_fats_100g: float | None = None
    fibers_100g: float | None = None
    salt_100g: float | None = None
    sodium_100g: float | None = None

    def is_empty(self) -> bool:
        return all(v is None for v in self.to_dict().values())

    def to_dict(self) -> dict:
        return {
            "kcal_100g": self.kcal_100g,
            "kj_100g": self.kj_100g,
            "proteins_100g": self.proteins_100g,
            "carbs_100g": self.carbs_100g,
            "sugars_100g": self.sugars_100g,
            "fats_100g": self.fats_100g,
            "saturated_fats_100g": self.saturated_fats_100g,
            "fibers_100g": self.fibers_100g,
            "salt_100g": self.salt_100g,
            "sodium_100g": self.sodium_100g,
        }


@dataclass
class Product:
    code: str                       # code-barres / EAN
    name: str
    brands: str = ""
    quantity: str = ""              # "400 g"
    nutriscore: str | None = None   # "A".."E"
    nova_group: int | None = None   # 1..4 (degré de transformation)
    ecoscore: str | None = None     # "A".."E" (impact environnemental)
    categories: str = ""
    labels: str = ""                # "Bio, Label Rouge..."
    allergens: list[str] = field(default_factory=list)   # ["milk", "nuts"]
    additives: list[str] = field(default_factory=list)   # ["E322", "E471"]
    nutrient_levels: dict = field(default_factory=dict)  # {fat: high, salt: low...}
    ingredients_text: str = ""
    image_url: str = ""
    nutrition: NutritionFacts | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "brands": self.brands,
            "quantity": self.quantity,
            "nutriscore": self.nutriscore,
            "nova_group": self.nova_group,
            "ecoscore": self.ecoscore,
            "categories": self.categories,
            "labels": self.labels,
            "allergens": self.allergens,
            "additives": self.additives,
            "nutrient_levels": self.nutrient_levels,
            "ingredients_text": self.ingredients_text,
            "image_url": self.image_url,
            "url": f"{BASE_URL}/product/{self.code}" if self.code else "",
            "nutrition": self.nutrition.to_dict() if self.nutrition else None,
        }
