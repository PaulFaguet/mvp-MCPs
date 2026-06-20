"""Modèles de données Picard."""

from dataclasses import dataclass, field

BASE_URL = "https://www.picard.fr"


@dataclass
class NutritionFacts:
    """Valeurs nutritionnelles pour 100 g (ou 100 ml), telles que listées sur la fiche."""

    kcal_100g: float | None = None
    kj_100g: float | None = None
    proteins_100g: float | None = None
    carbs_100g: float | None = None
    sugars_100g: float | None = None
    fats_100g: float | None = None
    saturated_fats_100g: float | None = None
    fibers_100g: float | None = None
    salt_100g: float | None = None

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
        }


@dataclass
class Product:
    id: str
    name: str
    price: float
    price_per_kg: str = ""          # "8,00 €/kg" (uniquement sur la fiche)
    format: str = ""               # "le sachet de 600 g"
    nutriscore: str | None = None  # "A".."E"
    planetscore: str | None = None # "A".."E"
    rating: float | None = None    # 4.71
    origin: str = ""               # "Produit élaboré en France..."
    category: str = ""
    subcategory: str = ""
    brand: str = ""
    label: str = ""                # "Label rouge", "Bio"...
    promo: str | None = None       # libellé bannière promo
    available: bool = True
    url: str = ""
    nutrition: NutritionFacts | None = None  # rempli par get_product_details
    ingredients: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "price": self.price,
            "price_per_kg": self.price_per_kg,
            "format": self.format,
            "nutriscore": self.nutriscore,
            "planetscore": self.planetscore,
            "rating": self.rating,
            "origin": self.origin,
            "category": self.category,
            "subcategory": self.subcategory,
            "brand": self.brand,
            "label": self.label,
            "promo": self.promo,
            "available": self.available,
            "url": f"{BASE_URL}{self.url}" if self.url.startswith("/") else self.url,
            "nutrition": self.nutrition.to_dict() if self.nutrition else None,
            "ingredients": self.ingredients,
        }


@dataclass
class Store:
    id: str
    name: str
    address: str
    url: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "address": self.address,
            "url": self.url,
        }


@dataclass
class CartItem:
    product: Product
    quantity: int
    subtotal: float

    def to_dict(self) -> dict:
        return {
            "product": self.product.to_dict(),
            "quantity": self.quantity,
            "subtotal": self.subtotal,
        }


@dataclass
class Cart:
    id: str
    items: list[CartItem] = field(default_factory=list)
    total: float = 0.0

    @property
    def item_count(self) -> int:
        return sum(item.quantity for item in self.items)

    def add_item(self, product: Product, quantity: int = 1) -> None:
        for item in self.items:
            if item.product.id == product.id:
                item.quantity += quantity
                item.subtotal = round(item.product.price * item.quantity, 2)
                self._recalculate()
                return
        self.items.append(
            CartItem(product=product, quantity=quantity, subtotal=round(product.price * quantity, 2))
        )
        self._recalculate()

    def remove_item(self, product_id: str) -> None:
        self.items = [item for item in self.items if item.product.id != product_id]
        self._recalculate()

    def _recalculate(self) -> None:
        self.total = round(sum(item.subtotal for item in self.items), 2)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "item_count": self.item_count,
        }
