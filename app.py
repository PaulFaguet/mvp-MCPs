"""MVP — Chatbot Mistral branché sur les MCP Super U & Picard."""

import asyncio
import hashlib
import io
import json
import os
import random
import re
import threading
import time
from contextlib import AsyncExitStack

from dotenv import load_dotenv

load_dotenv()

import streamlit as st
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mistralai import Mistral
from mistralai.models import SDKError

try:
    import zxingcpp
    from PIL import Image

    _BARCODE_OK = True
except ImportError:
    _BARCODE_OK = False

RETRYABLE_STATUSES = (429, 500, 502, 503, 504)

HOME = os.path.expanduser("~")

SERVERS = {
    "superu": StdioServerParameters(command="/bin/bash", args=[f"{HOME}/mcp-superu/run.sh"]),
    "picard": StdioServerParameters(command="/bin/bash", args=[f"{HOME}/mcp-picard/run.sh"]),
    "openfoodfacts": StdioServerParameters(command="/bin/bash", args=[f"{HOME}/mcp-openfoodfacts/run.sh"]),
}

CONNECTOR_LABELS = {
    "superu": "`superu__` (Super U Drive — prix réels par magasin)",
    "picard": "`picard__` (Picard — surgelés, prix national)",
    "openfoodfacts": (
        "`openfoodfacts__` (Open Food Facts — nutrition, Nutri-Score, NOVA, "
        "additifs, allergènes par code-barres/EAN)"
    ),
}

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


# -------------------------------------------------------------- barcode scanner

def _decode_barcode(img_bytes: bytes) -> str | None:
    """Décode un code-barres (EAN-13, UPC…) depuis une image. None si rien trouvé."""
    if not _BARCODE_OK:
        return None
    try:
        img = Image.open(io.BytesIO(img_bytes))
        for r in zxingcpp.read_barcodes(img):
            if r.text and r.valid:
                return r.text
    except Exception:  # noqa: BLE001
        return None
    return None


def render_scanner(use_off: bool) -> None:
    """Affiche le module webcam : capture une photo, décode l'EAN, et injecte
    une question produit dans le chat (traitée par le connecteur Open Food Facts)."""
    with st.expander("📷 Scanner un produit (webcam)", expanded=False):
        if not use_off:
            st.caption("Active le connecteur **Nutri (OFF)** pour scanner un produit.")
            return
        if not _BARCODE_OK:
            st.caption("Module de scan indisponible (`pip install zxing-cpp`).")
            return
        st.caption("Vise le code-barres du produit puis prends la photo.")
        photo = st.camera_input("Code-barres", key="scan_cam", label_visibility="collapsed")
        if photo is None:
            return
        img_bytes = photo.getvalue()
        h = hashlib.md5(img_bytes).hexdigest()
        if st.session_state.get("last_scan_hash") == h:
            return  # déjà traité (évite la boucle de rerun)
        st.session_state.last_scan_hash = h
        ean = _decode_barcode(img_bytes)
        if not ean:
            st.warning("Aucun code-barres détecté. Recadre, rapproche, et réessaie.")
            return
        st.success(f"Code détecté : **{ean}**")
        st.session_state.pending_scan_prompt = (
            f"Le code-barres scanné est {ean}. Donne la fiche Open Food Facts de ce "
            "produit (Nutri-Score, NOVA, additifs, allergènes, valeurs nutritionnelles) "
            "puis ton avis nutritionnel en une phrase."
        )
        st.rerun()


# ----------------------------------------------------------------- store utils

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


def _build_system_prompt(selected: list[dict], enabled: set[str]) -> str:
    active = [CONNECTOR_LABELS[k] for k in CONNECTOR_LABELS if k in enabled]
    if not active:
        scope = "Aucun connecteur MCP n'est actif : préviens l'utilisateur d'en activer un."
    elif len(active) == 1:
        scope = (
            f"Tu disposes uniquement des outils MCP {active[0]}. "
            "N'utilise et n'évoque aucun autre connecteur."
        )
    else:
        scope = (
            "Tu disposes d'outils MCP : " + " ; ".join(active) + ". "
            "Choisis le ou les connecteurs pertinents selon la question. Tu peux "
            "croiser les sources — ex: récupérer l'EAN d'un produit via `superu__` "
            "ou `picard__`, puis le passer à `openfoodfacts__get_product` pour le "
            "détail nutritionnel (Nutri-Score, NOVA, additifs)."
        )

    base = (
        "Tu es un assistant de courses intelligent et conversationnel. "
        "Tu aides à chercher, comparer et discuter des produits alimentaires. "
        f"{scope}\n\n"
        "RÈGLES D'UTILISATION DES OUTILS :\n"
        "- Dès qu'une question mentionne un produit, un prix, une promo, une catégorie "
        "ou une comparaison → appelle les outils MCP appropriés (search_products, "
        "compare_prices, get_promotions, get_product, etc.), préfixés du bon connecteur.\n"
        "- Pour une question de nutrition/santé/composition d'un produit (Nutri-Score, "
        "additifs, allergènes, ultra-transformation) → utilise `openfoodfacts__` "
        "(par code-barres/EAN, ou via search_products pour trouver l'EAN).\n"
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
    if not ("superu" in enabled and selected):
        return base
    store_lines = "\n".join(f"  - {s['name']} (slug: `{s['slug']}`)" for s in selected)
    return (
        base
        + f"\n\nMagasins Super U sélectionnés ({len(selected)}) :\n{store_lines}\n"
        "Dès qu'une question porte sur un prix, une comparaison, ou un produit "
        "Super U, vérifie SYSTÉMATIQUEMENT CHACUN de ces magasins : appelle "
        "`superu__set_store` avec le slug, puis `superu__search_products`, et répète "
        "pour chaque magasin de la liste avant de répondre — ne te limite jamais à un "
        "seul magasin si plusieurs sont sélectionnés."
    )


# --------------------------------------------------------------- async helpers

def _unwrap_exception(e: BaseException) -> str:
    """Déroule récursivement un ExceptionGroup pour afficher la vraie erreur sous-jacente."""
    if isinstance(e, BaseExceptionGroup):
        return "\n".join(_unwrap_exception(sub) for sub in e.exceptions)
    return f"{type(e).__name__}: {e}"


def _find_status(e: BaseException) -> int | None:
    """Cherche un code HTTP (ex: 429) en déroulant un ExceptionGroup si besoin."""
    if isinstance(e, BaseExceptionGroup):
        for sub in e.exceptions:
            status = _find_status(sub)
            if status is not None:
                return status
        return None
    return getattr(getattr(e, "raw_response", None), "status_code", None)


def _render_error(e: BaseException, context: str = "Erreur") -> None:
    """Affiche l'erreur de façon adaptée : warning orange si surcharge passagère,
    erreur rouge sinon (avec le détail technique pour le debug)."""
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
    """Réessaie fn() avec backoff exponentiel sur erreurs transitoires (429/5xx)."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except SDKError as e:
            status = getattr(e.raw_response, "status_code", None)
            if status not in RETRYABLE_STATUSES or attempt == retries:
                raise
            time.sleep(base_delay * (2 ** attempt))


def run_sync(coro):
    box = {}

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            box["value"] = loop.run_until_complete(coro)
        except Exception as e:  # noqa: BLE001
            box["error"] = e
        finally:
            loop.close()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


async def fetch_superu_stores(postal_code: str) -> list[dict]:
    params = SERVERS["superu"]
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("find_stores", {"postal_code": postal_code})
            raw = "\n".join(getattr(c, "text", "") for c in result.content).strip()
    return _parse_store_text(raw)


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


async def run_agent_tools(
    history: list[dict], model: str, primary_slug: str = "", enabled: set[str] | None = None
):
    enabled = enabled if enabled is not None else set(SERVERS.keys())
    active_servers = {k: v for k, v in SERVERS.items() if k in enabled}

    async with AsyncExitStack() as stack:
        sessions: dict[str, ClientSession] = {}
        tools_spec: list[dict] = []
        routing: dict[str, tuple[str, str]] = {}

        for name, params in active_servers.items():
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            sessions[name] = session
            for tool in (await session.list_tools()).tools:
                full = f"{name}__{tool.name}"
                tools_spec.append({
                    "type": "function",
                    "function": {
                        "name": full,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                    },
                })
                routing[full] = (name, tool.name)

        if primary_slug and "superu" in sessions:
            try:
                await sessions["superu"].call_tool("set_store", {"store": primary_slug})
            except Exception:  # noqa: BLE001
                pass

        client = Mistral(api_key=API_KEY)
        messages = list(history)
        trace: list[dict] = []

        for _ in range(MAX_ROUNDS):
            resp = await asyncio.to_thread(
                lambda: _call_with_retry(
                    lambda: client.chat.complete(
                        model=model,
                        messages=messages,
                        tools=tools_spec,
                        tool_choice="auto",
                        temperature=TEMPERATURE,
                    )
                )
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:
                break

            messages.append(_assistant_to_dict(msg))

            for tc in msg.tool_calls:
                server_tool = routing.get(tc.function.name)
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if server_tool is None:
                    content = f"Outil inconnu : {tc.function.name}"
                else:
                    server, tool_name = server_tool
                    result = await sessions[server].call_tool(tool_name, args)
                    content = "\n".join(
                        getattr(c, "text", "") for c in result.content
                    ).strip() or "(réponse vide)"
                trace.append({"tool": tc.function.name, "args": args, "result": content})
                messages.append({
                    "role": "tool",
                    "name": tc.function.name,
                    "tool_call_id": tc.id,
                    "content": content,
                })

        return messages, trace


# --------------------------------------------------------------------------- UI

st.set_page_config(page_title="Courses MCP — Super U × Picard", page_icon="🛒")
st.title("🛒 Chatbot Courses — Super U × Picard")
st.caption("Mistral + MCP : prix réels, nutriscore, nutrition, promos, panier.")

# ── Session state ─────────────────────────────────────────────────────────────
if "found_stores" not in st.session_state:
    st.session_state.found_stores: list[dict] = []
if "selected_stores" not in st.session_state:
    st.session_state.selected_stores: list[dict] = []

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Réglages")

    # ── Connecteurs MCP ──
    st.subheader("🔌 Connecteurs")
    c1, c2, c3 = st.columns(3)
    with c1:
        use_superu = st.toggle("Super U", value=st.session_state.get("use_superu", True))
    with c2:
        use_picard = st.toggle("Picard", value=st.session_state.get("use_picard", True))
    with c3:
        use_off = st.toggle("Nutri (OFF)", value=st.session_state.get("use_off", True))
    st.session_state.use_superu = use_superu
    st.session_state.use_picard = use_picard
    st.session_state.use_off = use_off
    enabled_servers = {
        n for n, on in (("superu", use_superu), ("picard", use_picard), ("openfoodfacts", use_off)) if on
    }

    st.divider()

    # ── Recherche magasins (uniquement si Super U actif) ──
    if use_superu:
        st.subheader("🏪 Magasins Super U")

        postal_input = st.text_input(
            "Code postal",
            placeholder="Ex : 69007",
            label_visibility="collapsed",
        )

        if st.button("🔍 Rechercher", use_container_width=True):
            cp = postal_input.strip()
            if not re.match(r"^\d{5}$", cp):
                st.error("Code postal à 5 chiffres requis.")
            else:
                with st.spinner("Recherche…"):
                    try:
                        st.session_state.found_stores = run_sync(fetch_superu_stores(cp))
                        if not st.session_state.found_stores:
                            st.warning("Aucun magasin trouvé.")
                    except Exception as e:  # noqa: BLE001
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
            {"role": "system", "content": _build_system_prompt(selected, enabled_servers)}
        ]
        st.rerun()

    st.caption("Serveurs MCP : Super U · Picard · Open Food Facts")

# ── Historique ────────────────────────────────────────────────────────────────
selected = st.session_state.selected_stores
system_msg = {
    "role": "system",
    "content": _build_system_prompt(selected, enabled_servers),
}

if "history" not in st.session_state:
    st.session_state.history = [system_msg]
else:
    if st.session_state.history and st.session_state.history[0]["role"] == "system":
        st.session_state.history[0] = system_msg

render_scanner(use_off)

for m in st.session_state.history:
    if m["role"] == "user":
        with st.chat_message("user"):
            st.markdown(m["content"])
    elif m["role"] == "assistant" and m.get("content"):
        with st.chat_message("assistant"):
            st.markdown(m["content"])

# ── Chat ──────────────────────────────────────────────────────────────────────
typed = st.chat_input("Ex : compare le prix du saumon fumé entre Picard et Super U")
prompt = typed or st.session_state.pop("pending_scan_prompt", None)
if prompt:
    if not API_KEY:
        st.error("Variable MISTRAL_API_KEY non définie.")
        st.stop()
    if not enabled_servers:
        st.error("Active au moins un connecteur dans la barre latérale.")
        st.stop()

    st.session_state.history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    primary_slug = selected[0]["slug"] if (use_superu and selected) else ""
    thinking_label = random.choice(THINKING_VERBS)

    with st.chat_message("assistant"):

        with st.spinner(thinking_label):
            try:
                messages_ready, trace = run_sync(
                    run_agent_tools(st.session_state.history, model, primary_slug, enabled_servers)
                )
            except Exception as e:  # noqa: BLE001
                _render_error(e)
                st.stop()

        if trace:
            with st.expander(f"🔧 {len(trace)} appel(s) MCP", expanded=False):
                for t in trace:
                    st.markdown(
                        f"**`{t['tool']}`** · `{json.dumps(t['args'], ensure_ascii=False)}`"
                    )
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
        except Exception as e:  # noqa: BLE001
            _render_error(e, context="Erreur streaming")
            st.stop()

        messages_ready.append({"role": "assistant", "content": response_text or ""})
        st.session_state.history = messages_ready
