"""MVP — Chatbot Mistral × Super U × Picard (version Cloud, sans subprocess MCP)."""

import json
import os
import random
import re
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

APP_DIR = os.path.dirname(os.path.abspath(__file__))

import streamlit as st
from mistralai import Mistral
from mistralai.models import SDKError

from picard.client import PicardClient
from picard.models import Cart as PicardCart
from picard.models import Product as PicardProduct
from superu.client import SuperUClient
from superu.models import Cart as SuperUCart
from superu.models import Product as SuperUProduct

RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
MAX_STORES = 7
MAX_ROUNDS = 6
TEMPERATURE = 0.2
API_KEY = os.getenv("MISTRAL_API_KEY", "")
MODELS = ["mistral-large-latest", "mistral-medium-latest"]

THINKING_VERBS = [
    "Je pousse le chariot…",
    "Je passe à la caisse…",
    "Je cherche le rayon des conserves…",
    "Je compare les étiquettes prix…",
    "Je farfouille au rayon surgelés…",
    "Je scanne les promos du moment…",
    "Je remplis le panier…",
    "Je fais le tour des rayons…",
    "Je vérifie le ticket de caisse…",
    "Je compare les prix au kilo…",
]

# ── Config Super U ───────────────────────────────────────────────────────────

_superu_config_path = os.path.join(APP_DIR, "superu", "config", "config.json")
try:
    with open(_superu_config_path, encoding="utf-8") as f:
        _superu_config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    _superu_config = {}

_picard_config_path = os.path.join(APP_DIR, "picard", "config", "config.json")
try:
    with open(_picard_config_path, encoding="utf-8") as f:
        _picard_config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    _picard_config = {}

superu_client = SuperUClient(
    store_slug=_superu_config.get("store", _superu_config.get("store_slug", "puteaux")),
    stores=_superu_config.get("stores"),
    cache_ttl_minutes=_superu_config.get("cache_ttl_minutes", 30),
)
picard_client = PicardClient(
    cache_ttl_minutes=_picard_config.get("cache_ttl_minutes", 30),
    request_delay=_picard_config.get("request_delay_seconds", 1.0),
)
superu_cart = SuperUCart(id=str(uuid.uuid4()))
picard_cart = PicardCart(id=str(uuid.uuid4()))


# ── Tool specs (identiques aux MCP servers) ──────────────────────────────────

SUPERU_TOOLS = [
    {"type": "function", "function": {"name": "superu__search_products", "description": "Rechercher des produits dans le catalogue Super U Drive.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 10}, "sort_by": {"type": "string", "enum": ["relevance", "price_asc", "price_desc"], "default": "relevance"}, "store": {"type": "string", "description": "Optionnel : magasin ciblé (preset, nom ou slug)."}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "superu__get_product_details", "description": "Détails d'un produit Super U (prix/unité, nutriscore, nutrition, ingrédients).", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}, "store": {"type": "string"}}, "required": ["product_id"]}}},
    {"type": "function", "function": {"name": "superu__compare_prices", "description": "Comparer le prix d'un produit entre magasins préconfigurés.", "parameters": {"type": "object", "properties": {"product": {"type": "string"}, "stores": {"type": "array", "items": {"type": "string"}}}, "required": ["product"]}}},
    {"type": "function", "function": {"name": "superu__find_stores", "description": "Trouver les magasins/drives Super U proches d'un code postal.", "parameters": {"type": "object", "properties": {"postal_code": {"type": "string"}}, "required": ["postal_code"]}}},
    {"type": "function", "function": {"name": "superu__get_promotions", "description": "Promotions Super U en cours pour une catégorie.", "parameters": {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]}}},
    {"type": "function", "function": {"name": "superu__set_store", "description": "Changer le magasin actif (les prix en dépendent).", "parameters": {"type": "object", "properties": {"store": {"type": "string"}}, "required": ["store"]}}},
    {"type": "function", "function": {"name": "superu__list_stores", "description": "Lister les magasins préconfigurés et le magasin actif.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "superu__add_to_cart", "description": "Ajouter des produits au panier Super U.", "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"product_id": {"type": "string"}, "name": {"type": "string"}, "price": {"type": "number"}, "quantity": {"type": "integer", "default": 1}}, "required": ["product_id", "name", "price"]}}}, "required": ["items"]}}},
    {"type": "function", "function": {"name": "superu__view_cart", "description": "Voir le panier Super U.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "superu__remove_from_cart", "description": "Retirer un produit du panier Super U.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]}}},
]

PICARD_TOOLS = [
    {"type": "function", "function": {"name": "picard__search_products", "description": "Rechercher des produits surgelés Picard.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 10}, "sort_by": {"type": "string", "enum": ["relevance", "price_asc", "price_desc", "rating", "reviews"], "default": "relevance"}, "nutriscore_filter": {"type": "string", "enum": ["A", "B", "C", "D", "E"]}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "picard__get_product_details", "description": "Fiche complète d'un produit Picard (prix, nutrition, ingrédients).", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]}}},
    {"type": "function", "function": {"name": "picard__browse_category", "description": "Parcourir un rayon Picard (feculents, legumes, plats-cuisines, poissons, viandes, desserts, pizzas, promotions, etc.).", "parameters": {"type": "object", "properties": {"category": {"type": "string"}, "sort_by": {"type": "string", "enum": ["relevance", "price_asc", "price_desc", "rating", "reviews"], "default": "relevance"}, "nutriscore_filter": {"type": "string", "enum": ["A", "B", "C", "D", "E"]}, "max_results": {"type": "integer", "default": 20}}, "required": ["category"]}}},
    {"type": "function", "function": {"name": "picard__get_promotions", "description": "Produits Picard en promotion.", "parameters": {"type": "object", "properties": {"max_results": {"type": "integer", "default": 20}}}}},
    {"type": "function", "function": {"name": "picard__compare_nutrition", "description": "Comparer plusieurs produits Picard par valeurs nutritionnelles.", "parameters": {"type": "object", "properties": {"product_ids": {"type": "array", "items": {"type": "string"}}, "sort_by_field": {"type": "string", "enum": ["proteines", "kcal", "fibres", "lipides", "glucides", "sucres", "sel", "prix_kg"], "default": "proteines"}}, "required": ["product_ids"]}}},
    {"type": "function", "function": {"name": "picard__find_stores", "description": "Magasins Picard proches d'un code postal.", "parameters": {"type": "object", "properties": {"postal_code": {"type": "string"}}, "required": ["postal_code"]}}},
    {"type": "function", "function": {"name": "picard__add_to_cart", "description": "Ajouter des produits au panier Picard.", "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"product_id": {"type": "string"}, "name": {"type": "string"}, "price": {"type": "number"}, "quantity": {"type": "integer", "default": 1}}, "required": ["product_id", "name", "price"]}}}, "required": ["items"]}}},
    {"type": "function", "function": {"name": "picard__view_cart", "description": "Voir le panier Picard.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "picard__remove_from_cart", "description": "Retirer un produit du panier Picard.", "parameters": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]}}},
]


# ── Tool handlers ────────────────────────────────────────────────────────────

def _fmt_superu_product(i: int, p: dict) -> str:
    price_str = f"{p['price']:.2f}€" if p["price"] > 0 else "Prix non dispo"
    bio = " [BIO]" if p.get("is_bio") else ""
    note = f" | Note: {p['rating']}/5" if p.get("rating") else ""
    promo = f"\n   PROMO: {p['promo_detail']}" if p.get("is_promo") and p.get("promo_detail") else ""
    lien = f"\n   Lien: {p['url']}" if p.get("url") else ""
    return (
        f"{i}. **{p['name']}**{bio}\n"
        f"   Prix: {price_str} | Marque: {p['brand'] or '—'} | EAN: {p['ean'] or '—'} | ID: {p['id']}{note}{promo}{lien}\n"
    )


def _fmt_picard_product(i: int, p: dict) -> str:
    price_str = f"{p['price']:.2f}€" if p["price"] > 0 else "Prix non dispo"
    fmt = f" ({p['format']})" if p.get("format") else ""
    ns = f" | Nutri: {p['nutriscore']}" if p.get("nutriscore") else ""
    ps = f" | Planet: {p['planetscore']}" if p.get("planetscore") else ""
    note = f" | Note: {p['rating']}/5" if p.get("rating") else ""
    promo = f"\n   PROMO: {p['promo']}" if p.get("promo") else ""
    lien = f"\n   Lien: {p['url']}" if p.get("url") else ""
    return (
        f"{i}. **{p['name']}**{fmt}\n"
        f"   Prix: {price_str}{ns}{ps}{note} | ID: {p['id']}{promo}{lien}\n"
    )


def handle_tool(name: str, args: dict) -> str:
    global superu_cart, picard_cart

    try:
        # ── Super U ──────────────────────────────────────────────────────
        if name == "superu__search_products":
            if args.get("store"):
                superu_client.set_store(args["store"])
            results = superu_client.search_products(
                query=args["query"],
                max_results=min(args.get("max_results", 10), 30),
                sort_by=args.get("sort_by", "relevance"),
            )
            if not results:
                return "Aucun produit trouvé pour cette recherche."
            lines = [f"**{len(results)} produit(s) pour \"{args['query']}\"** (magasin actif : {superu_client.store_label()})\n"]
            lines += [_fmt_superu_product(i, p) for i, p in enumerate(results, 1)]
            return "\n".join(lines)

        elif name == "superu__get_product_details":
            if args.get("store"):
                superu_client.set_store(args["store"])
            product = superu_client.get_product_details(args["product_id"])
            if not product:
                return "Produit introuvable."
            product["store"] = superu_client.store_label()
            return json.dumps(product, indent=2, ensure_ascii=False)

        elif name == "superu__compare_prices":
            data = superu_client.compare_prices(args["product"], args.get("stores"))
            rows = data["results"]
            if not rows:
                return f"Produit introuvable : {args['product']}"
            lines = [f"**Comparaison pour : {data['product']}** (id {data['id']})\n"]
            for i, r in enumerate(rows, 1):
                price = f"{r['price']:.2f}€" if r["price"] > 0 else "non dispo"
                unit = f" ({r['price_per_unit']})" if r["price_per_unit"] else ""
                tag = "  ⬅ le moins cher" if i == 1 and r["price"] > 0 else ""
                lien = f"\n   Lien: {r['url']}" if r.get("url") else ""
                lines.append(f"{i}. **{r['store_name']}** — {price}{unit}{tag}{lien}")
            valid = [r["price"] for r in rows if r["price"] > 0]
            if len(valid) > 1:
                lines.append(f"\nÉcart : {max(valid) - min(valid):.2f}€ entre le plus cher et le moins cher.")
            return "\n".join(lines)

        elif name == "superu__find_stores":
            stores = superu_client.find_stores(postal_code=args["postal_code"])
            if not stores:
                return "Aucun magasin trouvé."
            lines = [f"**{len(stores)} magasin(s) près de {args['postal_code']}**\n"]
            for s in stores:
                slug = f"\n  Drive slug: `{s['drive_slug']}`" if s.get("drive_slug") else ""
                lines.append(f"- **{s['name']}** (ID: {s['id']})\n  {s['address']}{slug}")
            return "\n".join(lines)

        elif name == "superu__get_promotions":
            results = superu_client.get_promotions(args["category"])
            if not results:
                return "Aucune promotion trouvée."
            lines = [f"**Promotions pour \"{args['category']}\" :**\n"]
            for p in results:
                price_str = f"{p['price']:.2f}€" if p["price"] > 0 else "Prix non dispo"
                promo = f" — {p['promo_detail']}" if p.get("promo_detail") else ""
                lien = f"\n    Lien: {p['url']}" if p.get("url") else ""
                lines.append(f"  - {p['name']} — {price_str}{promo} (ID: {p['id']}){lien}")
            return "\n".join(lines)

        elif name == "superu__set_store":
            slug = superu_client.set_store(args["store"])
            label = next(
                (p["name"] for p in superu_client.stores.values() if p.get("slug") == slug),
                slug,
            )
            return f"Magasin actif : **{label}** (slug `{slug}`). Les prix suivants viendront de ce magasin."

        elif name == "superu__list_stores":
            if not superu_client.stores:
                return f"Magasin actif (slug) : {superu_client.store_slug}"
            lines = ["**Magasins préconfigurés :**\n"]
            for key, p in superu_client.stores.items():
                active = "  ← actif" if p.get("slug") == superu_client.store_slug else ""
                lines.append(f"- `{key}` — {p.get('name', '')} ({p.get('address', '')}){active}")
            return "\n".join(lines)

        elif name == "superu__add_to_cart":
            added = []
            for item in args["items"]:
                product = SuperUProduct(id=item["product_id"], name=item["name"], brand="", price=item["price"], price_per_unit="", image_url="", available=True, url="", ean=item["product_id"])
                superu_cart.add_item(product, item.get("quantity", 1))
                added.append(f"  - {item.get('quantity', 1)}x {item['name']} ({item['price']:.2f}€)")
            return f"**Produit(s) ajouté(s) :**\n" + "\n".join(added) + f"\n\n**Total : {superu_cart.total:.2f}€** ({superu_cart.item_count} article(s))"

        elif name == "superu__view_cart":
            if not superu_cart.items:
                return "Le panier Super U est vide."
            lines = ["**Panier Super U :**\n"]
            for item in superu_cart.items:
                lines.append(f"  - {item.quantity}x {item.product.name} — {item.subtotal:.2f}€ ({item.product.price:.2f}€/u)")
            lines.append(f"\n**Total : {superu_cart.total:.2f}€** ({superu_cart.item_count} article(s))")
            return "\n".join(lines)

        elif name == "superu__remove_from_cart":
            before = superu_cart.item_count
            superu_cart.remove_item(args["product_id"])
            if superu_cart.item_count < before:
                return f"Produit retiré. Total: {superu_cart.total:.2f}€"
            return "Produit non trouvé dans le panier."

        # ── Picard ───────────────────────────────────────────────────────
        elif name == "picard__search_products":
            results = picard_client.search_products(
                query=args["query"],
                max_results=min(args.get("max_results", 10), 40),
                sort_by=args.get("sort_by", "relevance"),
                nutriscore_filter=args.get("nutriscore_filter"),
            )
            if not results:
                return "Aucun produit trouvé pour cette recherche."
            lines = [f"**{len(results)} produit(s) pour \"{args['query']}\"**\n"]
            lines += [_fmt_picard_product(i, p) for i, p in enumerate(results, 1)]
            return "\n".join(lines)

        elif name == "picard__get_product_details":
            product = picard_client.get_product_details(args["product_id"])
            if not product:
                return "Produit introuvable."
            return json.dumps(product, indent=2, ensure_ascii=False)

        elif name == "picard__browse_category":
            results = picard_client.browse_category(
                category=args["category"],
                sort_by=args.get("sort_by", "relevance"),
                nutriscore_filter=args.get("nutriscore_filter"),
                max_results=min(args.get("max_results", 20), 48),
            )
            if not results:
                cats = ", ".join(picard_client.available_categories())
                return f"Aucun produit pour « {args['category']} ».\nCatégories disponibles : {cats}"
            lines = [f"**{len(results)} produit(s) — rayon « {args['category']} »**\n"]
            lines += [_fmt_picard_product(i, p) for i, p in enumerate(results, 1)]
            return "\n".join(lines)

        elif name == "picard__get_promotions":
            results = picard_client.get_promotions(max_results=min(args.get("max_results", 20), 48))
            if not results:
                return "Aucune promotion trouvée."
            lines = [f"**{len(results)} produit(s) en promotion :**\n"]
            lines += [_fmt_picard_product(i, p) for i, p in enumerate(results, 1)]
            return "\n".join(lines)

        elif name == "picard__compare_nutrition":
            ids = args["product_ids"]
            if not ids or len(ids) < 2:
                return "Donne au moins 2 IDs produits à comparer."
            data = picard_client.compare_nutrition(ids, args.get("sort_by_field", "proteines"))
            if not data["products"]:
                return "Aucune donnée nutritionnelle récupérée."
            lines = [f"**Comparaison de {data['count']} produit(s) — tri par {data['sorted_by']} ({data['order']})** (valeurs /100 g)\n"]
            for i, r in enumerate(data["products"], 1):
                ns = f" | Nutri {r['nutriscore']}" if r.get("nutriscore") else ""
                ppk = f" | {r['price_per_kg']}" if r.get("price_per_kg") else ""
                lien = f"\n   Lien: {r['url']}" if r.get("url") else ""
                lines.append(
                    f"{i}. **{r['name']}** (ID {r['id']}){ns}{ppk}\n"
                    f"   kcal: {r.get('kcal_100g')} | protéines: {r.get('proteins_100g')}g | "
                    f"glucides: {r.get('carbs_100g')}g | lipides: {r.get('fats_100g')}g | "
                    f"fibres: {r.get('fibers_100g')}g | sel: {r.get('salt_100g')}g{lien}\n"
                )
            return "\n".join(lines)

        elif name == "picard__find_stores":
            stores = picard_client.find_stores(postal_code=args["postal_code"])
            if not stores:
                return f"Pas de magasin trouvé pour « {args['postal_code']} ».\nFinder officiel : https://www.picard.fr/picard-a-votre-service/magasins/"
            lines = [f"**{len(stores)} magasin(s) près de {args['postal_code']} :**\n"]
            for s in stores:
                addr = f"\n  {s['address']}" if s.get("address") else ""
                lines.append(f"- **{s['name']}**{addr}")
            return "\n".join(lines)

        elif name == "picard__add_to_cart":
            added = []
            for item in args["items"]:
                product = PicardProduct(id=item["product_id"], name=item["name"], price=item["price"])
                picard_cart.add_item(product, item.get("quantity", 1))
                added.append(f"  - {item.get('quantity', 1)}x {item['name']} ({item['price']:.2f}€)")
            return f"**Produit(s) ajouté(s) :**\n" + "\n".join(added) + f"\n\n**Total : {picard_cart.total:.2f}€** ({picard_cart.item_count} article(s))"

        elif name == "picard__view_cart":
            if not picard_cart.items:
                return "Le panier Picard est vide."
            lines = ["**Panier Picard :**\n"]
            for item in picard_cart.items:
                lines.append(f"  - {item.quantity}x {item.product.name} — {item.subtotal:.2f}€ ({item.product.price:.2f}€/u)")
            lines.append(f"\n**Total : {picard_cart.total:.2f}€** ({picard_cart.item_count} article(s))")
            return "\n".join(lines)

        elif name == "picard__remove_from_cart":
            before = picard_cart.item_count
            picard_cart.remove_item(args["product_id"])
            if picard_cart.item_count < before:
                return f"Produit retiré. Total: {picard_cart.total:.2f}€"
            return "Produit non trouvé dans le panier."

        else:
            return f"Outil inconnu : {name}"

    except Exception as e:
        return f"Erreur : {e}"


# ── Prompts ──────────────────────────────────────────────────────────────────

def _parse_store_text(text: str) -> list[dict]:
    stores = []
    for block in re.split(r"\n(?=- \*\*)", "\n" + text):
        name_m = re.match(r"-\s+\*\*(.+?)\*\*", block)
        if not name_m:
            continue
        lines = [ln.strip() for ln in block.strip().splitlines()]
        address = lines[1] if len(lines) > 1 else ""
        slug_m = re.search(r"`([\w-]+)`", block)
        stores.append({
            "name": name_m.group(1).strip(),
            "address": address,
            "slug": slug_m.group(1).strip() if slug_m else "",
        })
    return stores


def _build_system_prompt(selected: list[dict], use_superu: bool, use_picard: bool) -> str:
    if use_superu and use_picard:
        scope = (
            "Tu disposes d'outils préfixés `superu__` (Super U Drive) et "
            "`picard__` (Picard, surgelés). Choisis l'enseigne pertinente selon la "
            "question, ou interroge les deux pour comparer."
        )
    elif use_superu:
        scope = (
            "Tu disposes uniquement des outils `superu__` (Super U Drive). "
            "Picard est désactivé — n'en parle pas."
        )
    elif use_picard:
        scope = (
            "Tu disposes uniquement des outils `picard__` (Picard, surgelés). "
            "Super U est désactivé — n'en parle pas."
        )
    else:
        scope = "Aucun connecteur n'est actif : préviens l'utilisateur d'en activer un."

    base = (
        "Tu es un assistant de courses intelligent et conversationnel. "
        "Tu aides à chercher, comparer et discuter des produits alimentaires. "
        f"{scope}\n\n"
        "RÈGLES D'UTILISATION DES OUTILS :\n"
        "- Dès qu'une question mentionne un produit, un prix, une promo, une catégorie "
        "ou une comparaison → appelle les outils appropriés (search_products, "
        "compare_prices, get_promotions, etc.), préfixés du bon connecteur.\n"
        "- Ne dis JAMAIS que tu n'as pas accès aux outils ou que tu ne peux pas chercher.\n"
        "- Si une recherche ne donne rien, dis-le en une phrase.\n\n"
        "CONVERSATION :\n"
        "- Tu peux discuter librement : donner des conseils cuisine, comparer des "
        "produits entre eux, expliquer un nutriscore, suggérer des alternatives, "
        "répondre à des questions générales sur l'alimentation.\n"
        "- Quand tu as déjà récupéré des données via un outil dans la conversation, "
        "tu peux t'y référer sans rappeler l'outil.\n"
        "- Tu peux mélanger données réelles (issues des outils) et connaissances "
        "générales (recettes, conseils, avis), tant que tu distingues les deux.\n\n"
        "STYLE :\n"
        "- Concis, concret, droit au but. Pas de blabla ni de reformulation.\n"
        "- Prix en gras, listes à puces ou tableaux pour les produits.\n"
        "- Quand un résultat d'outil contient un champ 'Lien' ou 'url', inclus-le "
        "sous forme de lien markdown : [Nom du produit](url) — jamais d'URL brute."
    )
    if not (use_superu and selected):
        return base
    store_lines = "\n".join(f"  - {s['name']} (slug: `{s['slug']}`)" for s in selected)
    return (
        base
        + f"\n\nMagasins Super U sélectionnés ({len(selected)}) :\n{store_lines}\n"
        "Dès qu'une question porte sur un prix, une comparaison, ou un produit "
        "Super U, vérifie SYSTÉMATIQUEMENT CHACUN de ces magasins : appelle "
        "`superu__set_store` avec le slug, puis `superu__search_products`, et répète "
        "pour chaque magasin de la liste avant de répondre."
    )


# ── Error helpers ────────────────────────────────────────────────────────────

def _unwrap_exception(e: BaseException) -> str:
    if isinstance(e, BaseExceptionGroup):
        return "\n".join(_unwrap_exception(sub) for sub in e.exceptions)
    return f"{type(e).__name__}: {e}"


def _find_status(e: BaseException) -> int | None:
    if isinstance(e, BaseExceptionGroup):
        for sub in e.exceptions:
            status = _find_status(sub)
            if status is not None:
                return status
        return None
    return getattr(getattr(e, "raw_response", None), "status_code", None)


def _render_error(e: BaseException, context: str = "Erreur") -> None:
    if _find_status(e) == 429:
        st.warning(
            "🛒 Il y a trop de monde aux caisses en ce moment, le service est "
            "surchargé. Réessaie dans quelques secondes, ou choisis un autre "
            "modèle dans le menu de gauche.",
            icon="⏳",
        )
    else:
        st.error(f"{context} : {_unwrap_exception(e)}")


def _call_with_retry(fn, retries: int = 3, base_delay: float = 2.0):
    for attempt in range(retries + 1):
        try:
            return fn()
        except SDKError as e:
            status = getattr(e.raw_response, "status_code", None)
            if status not in RETRYABLE_STATUSES or attempt == retries:
                raise
            time.sleep(base_delay * (2 ** attempt))


# ── Agent loop (synchrone, sans MCP) ────────────────────────────────────────

def _assistant_to_dict(msg) -> dict:
    d = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return d


def run_agent_tools(history: list[dict], model: str, primary_slug: str = "", enabled: set[str] | None = None):
    enabled = enabled or {"superu", "picard"}
    tools_spec = []
    if "superu" in enabled:
        tools_spec += SUPERU_TOOLS
    if "picard" in enabled:
        tools_spec += PICARD_TOOLS

    if primary_slug and "superu" in enabled:
        try:
            superu_client.set_store(primary_slug)
        except Exception:
            pass

    client = Mistral(api_key=API_KEY)
    messages = list(history)
    trace: list[dict] = []

    for _ in range(MAX_ROUNDS):
        resp = _call_with_retry(
            lambda: client.chat.complete(
                model=model,
                messages=messages,
                tools=tools_spec,
                tool_choice="auto",
                temperature=TEMPERATURE,
            )
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            break

        messages.append(_assistant_to_dict(msg))

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            content = handle_tool(tc.function.name, args)
            trace.append({"tool": tc.function.name, "args": args, "result": content})
            messages.append({
                "role": "tool",
                "name": tc.function.name,
                "tool_call_id": tc.id,
                "content": content,
            })

    return messages, trace


# ── UI ───────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Courses MCP — Super U × Picard", page_icon="🛒")
st.title("🛒 Chatbot Courses — Super U × Picard")
st.caption("Mistral + MCP : prix réels, nutriscore, nutrition, promos, panier.")

if "found_stores" not in st.session_state:
    st.session_state.found_stores: list[dict] = []
if "selected_stores" not in st.session_state:
    st.session_state.selected_stores: list[dict] = []

with st.sidebar:
    st.header("⚙️ Réglages")

    st.subheader("🔌 Connecteurs")
    c1, c2 = st.columns(2)
    with c1:
        use_superu = st.toggle("Super U", value=st.session_state.get("use_superu", True))
    with c2:
        use_picard = st.toggle("Picard", value=st.session_state.get("use_picard", True))
    st.session_state.use_superu = use_superu
    st.session_state.use_picard = use_picard

    st.divider()

    if use_superu:
        st.subheader("🏪 Magasins Super U")
        postal_input = st.text_input("Code postal", placeholder="Ex : 69007", label_visibility="collapsed")
        if st.button("🔍 Rechercher", use_container_width=True):
            cp = postal_input.strip()
            if not re.match(r"^\d{5}$", cp):
                st.error("Code postal à 5 chiffres requis.")
            else:
                with st.spinner("Recherche…"):
                    try:
                        result = superu_client.find_stores(postal_code=cp)
                        found_stores = []
                        for s in result:
                            found_stores.append({
                                "name": s.get("name", ""),
                                "address": s.get("address", ""),
                                "slug": s.get("drive_slug", ""),
                            })
                        st.session_state.found_stores = found_stores
                        if not found_stores:
                            st.warning("Aucun magasin trouvé.")
                    except Exception as e:
                        st.error(f"Erreur : {e}")

        found: list[dict] = st.session_state.found_stores
        selected: list[dict] = st.session_state.selected_stores

        if found:
            st.caption("Cliquez sur ＋ pour ajouter")
            with st.container(height=220):
                for i, store in enumerate(found):
                    already = any(s["slug"] == store["slug"] for s in selected)
                    full = len(selected) >= MAX_STORES
                    sc1, sc2 = st.columns([6, 1])
                    with sc1:
                        st.markdown(store["name"])
                    with sc2:
                        if already:
                            st.markdown("✓")
                        else:
                            if st.button("＋", key=f"add_{i}", disabled=full):
                                st.session_state.selected_stores.append(store)
                                st.rerun()

        if selected:
            st.divider()
            st.caption(f"Sélectionnés ({len(selected)}/{MAX_STORES}) — cliquez sur ✕ pour retirer")
            for i, store in enumerate(selected):
                sc1, sc2 = st.columns([6, 1])
                with sc1:
                    st.markdown(f"**{store['name']}**")
                    if store.get("address"):
                        st.caption(store["address"])
                with sc2:
                    if st.button("✕", key=f"rm_{i}"):
                        st.session_state.selected_stores.pop(i)
                        st.rerun()
        st.divider()
    else:
        selected = st.session_state.selected_stores
        st.caption("Super U désactivé.")
        st.divider()

    model = st.selectbox("Modèle Mistral", MODELS, index=0)
    st.divider()

    if st.button("🗑️ Réinitialiser la conversation", use_container_width=True):
        st.session_state.history = [
            {"role": "system", "content": _build_system_prompt(selected, use_superu, use_picard)}
        ]
        st.rerun()

selected = st.session_state.selected_stores
system_msg = {"role": "system", "content": _build_system_prompt(selected, use_superu, use_picard)}

if "history" not in st.session_state:
    st.session_state.history = [system_msg]
else:
    if st.session_state.history and st.session_state.history[0]["role"] == "system":
        st.session_state.history[0] = system_msg

for m in st.session_state.history:
    if m["role"] == "user":
        with st.chat_message("user"):
            st.markdown(m["content"])
    elif m["role"] == "assistant" and m.get("content"):
        with st.chat_message("assistant"):
            st.markdown(m["content"])

prompt = st.chat_input("Ex : compare le prix du saumon fumé entre Picard et Super U")
if prompt:
    if not API_KEY:
        st.error("Variable MISTRAL_API_KEY non définie.")
        st.stop()
    if not use_superu and not use_picard:
        st.error("Active au moins un connecteur (Super U ou Picard) dans la barre latérale.")
        st.stop()

    st.session_state.history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    enabled_servers = {n for n, on in (("superu", use_superu), ("picard", use_picard)) if on}
    primary_slug = selected[0]["slug"] if (use_superu and selected) else ""
    thinking_label = random.choice(THINKING_VERBS)

    with st.chat_message("assistant"):
        with st.spinner(thinking_label):
            try:
                messages_ready, trace = run_agent_tools(
                    st.session_state.history, model, primary_slug, enabled_servers
                )
            except Exception as e:
                _render_error(e)
                st.stop()

        if trace:
            with st.expander(f"🔧 {len(trace)} appel(s) MCP", expanded=False):
                for t in trace:
                    st.markdown(f"**`{t['tool']}`** · `{json.dumps(t['args'], ensure_ascii=False)}`")
                    st.code(t["result"][:2000])

        mistral_client = Mistral(api_key=API_KEY)

        def _stream_gen():
            attempt = 0
            while True:
                yielded_any = False
                try:
                    with mistral_client.chat.stream(
                        model=model, messages=messages_ready, temperature=TEMPERATURE
                    ) as stream:
                        for event in stream:
                            delta = event.data.choices[0].delta.content
                            if delta and isinstance(delta, str):
                                yielded_any = True
                                yield delta
                    return
                except SDKError as e:
                    status = getattr(e.raw_response, "status_code", None)
                    if yielded_any or status not in RETRYABLE_STATUSES or attempt >= 3:
                        raise
                    attempt += 1
                    time.sleep(2.0 * (2 ** (attempt - 1)))

        try:
            response_text = st.write_stream(_stream_gen())
        except Exception as e:
            _render_error(e, context="Erreur streaming")
            st.stop()

        messages_ready.append({"role": "assistant", "content": response_text or ""})
        st.session_state.history = messages_ready
