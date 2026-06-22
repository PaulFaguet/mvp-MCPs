# MVP — Chatbot Courses (Super U × Picard × Open Food Facts)

Chatbot Streamlit connecté à **Mistral AI** et à 3 serveurs **MCP : Super U, Picard
et Open Food Facts**. Discutez en langage naturel : le LLM appelle les outils MCP
pour ramener des données réelles (prix, nutriscore, nutrition, additifs, promos, panier).

## Fonctionnalités

- Recherche de produits avec prix réels (Super U par magasin, Picard national)
- Comparaison de prix entre magasins Super U
- Profil nutritionnel détaillé via Open Food Facts (Nutri-Score, NOVA, additifs,
  allergènes) par code-barres — couplable à l'EAN des produits Super U / Picard
- Panier de courses local
- Streaming des réponses
- Multi-source : 3 connecteurs activables indépendamment
- Liens directs vers les fiches produits

## Prérequis

- Python 3.11+
- Les serveurs MCP : [`mcp-superu`](https://github.com/PaulFaguet/mcp-superU),
  [`mcp-picard`](https://github.com/PaulFaguet/mcp-picard) et `mcp-openfoodfacts`
- Une clé API [Mistral](https://console.mistral.ai/)

## Installation

```bash
git clone https://github.com/PaulFaguet/mvp-MCPs.git
cd mvp-MCPs
pip install -r requirements.txt
```

## Lancer

```bash
# Clé API dans un fichier .env (jamais dans le code)
echo "MISTRAL_API_KEY=ta_cle" > .env

./run.sh
# ou : streamlit run app.py
```

## Exemples de prompts

```
Compare le prix du saumon fumé entre Picard et Super U
Cherche du quinoa chez Picard, du moins cher au plus cher
Classe 3 plats cuisinés Picard par protéines
Scanne le 3017620422003 et dis-moi si c'est sain
C'est quoi le Nutri-Score et les additifs de ce produit ?
```

## Comment ça marche

1. À chaque message, l'app lance les serveurs MCP en stdio et récupère leurs outils.
2. Les outils sont exposés à Mistral, namespacés `superu__…` / `picard__…` / `openfoodfacts__…`.
3. Mistral décide quels outils appeler ; l'app exécute les appels MCP et renvoie les
   résultats au modèle, en boucle (max 6 tours), jusqu'à la réponse finale.
4. La réponse est streamée en temps réel.
5. Le détail des appels MCP est visible dans l'expander « 🔧 appels MCP ».

## Licence

MIT
