"""Modèles de données Super U."""

from dataclasses import dataclass, field

BASE_URL = "https://www.coursesu.com"


@dataclass
class Product:
    id: str
    name: str
    brand: str
    price: float
    price_per_unit: str
    image_url: str
    available: bool
    url: str
    nutriscore: str | None = None
    is_bio: bool = False
    is_promo: bool = False
    promo_detail: str | None = None
    category: str = ""
    ean: str | None = None
    rating: float | None = None
    nutrition: dict | None = None
    ingredients: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "brand": self.brand,
            "price": self.price,
            "price_per_unit": self.price_per_unit,
            "image_url": self.image_url,
            "available": self.available,
            "url": f"{BASE_URL}{self.url}" if self.url.startswith("/") else self.url,
            "nutriscore": self.nutriscore,
            "is_bio": self.is_bio,
            "is_promo": self.is_promo,
            "promo_detail": self.promo_detail,
            "category": self.category,
            "ean": self.ean,
            "rating": self.rating,
            "nutrition": self.nutrition,
            "ingredients": self.ingredients,
        }


@dataclass
class Store:
    id: str
    name: str
    address: str
    drive_slug: str = ""
    store_type: str = "drive"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "address": self.address,
            "drive_slug": self.drive_slug,
            "store_type": self.store_type,
            "url": f"{BASE_URL}/drive-{self.drive_slug}" if self.drive_slug else "",
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
