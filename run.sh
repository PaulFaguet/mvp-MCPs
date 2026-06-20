#!/bin/bash
# Lance le chatbot MVP. Utilise python3.11 (qui a déjà mistralai + mcp).
cd "$(dirname "$0")"
PY=/opt/homebrew/opt/python@3.11/bin/python3.11

# Installe streamlit au 1er lancement si absent.
if ! "$PY" -c "import streamlit" 2>/dev/null; then
  echo "Installation de streamlit…"
  "$PY" -m pip install -r requirements.txt
fi

# Clé API : depuis l'env, sinon depuis .env si présent.
if [ -z "$MISTRAL_API_KEY" ] && [ -f .env ]; then
  export "$(grep -v '^#' .env | xargs)"
fi

exec "$PY" -m streamlit run app.py
