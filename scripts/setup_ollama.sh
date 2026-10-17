#!/usr/bin/env bash
# Check (and, only with your approval, prepare) the local model for the LOCAL DEMO.
# * Never installs applications for you: Ollama comes from its official site, https://ollama.com/download
# * Pulls the model only if it is missing, after showing its size and your free disk space, and only if you agree.
set -euo pipefail
MODEL="${LOCAL_LLM_MODEL:-qwen2.5:3b-instruct}"
URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
SIZE_GB=2                      # qwen2.5:3b-instruct is listed at 1.9 GB on ollama.com/library/qwen2.5/tags
case "$MODEL" in qwen2.5:1.5b-instruct) SIZE_GB=1 ;; qwen2.5:7b-instruct) SIZE_GB=5 ;; esac
case "$MODEL" in *-cloud|*:cloud) echo "Cloud models run remotely and are not allowed for the local demo."; exit 1 ;; esac

echo "OS: $(uname -s) $(uname -m)"
if [ "$(uname -s)" = "Darwin" ]; then
  echo "Memory: $(( $(sysctl -n hw.memsize) / 1073741824 )) GB"
fi
if ! command -v ollama >/dev/null 2>&1; then
  echo
  echo "Ollama is not installed."
  echo "Install it from the official site: https://ollama.com/download (macOS: download, open, move to Applications)."
  echo "Then re-run this script."
  exit 1
fi
echo "Ollama: $(ollama --version 2>/dev/null | tail -1)"
if ! curl -fsS -m 3 "$URL/api/version" >/dev/null 2>&1; then
  echo "The Ollama server is not running at $URL. Open the Ollama app, or run in another terminal:  ollama serve"
  exit 1
fi
if ollama list | awk 'NR>1{print $1}' | grep -qx "$MODEL"; then
  echo "Model $MODEL is already installed - nothing to download."
else
  FREE_GB=$(( $(df -k "$HOME" | awk 'NR==2{print $4}') / 1048576 ))
  echo "Model $MODEL is not installed. Download size about ${SIZE_GB} GB; free disk space: ${FREE_GB} GB."
  if [ "$FREE_GB" -lt $(( SIZE_GB * 2 + 2 )) ]; then
    echo "Not enough free space. Free some space or choose a smaller model: LOCAL_LLM_MODEL=qwen2.5:1.5b-instruct"
    exit 1
  fi
  read -r -p "Download $MODEL now with 'ollama pull'? [y/N] " ok
  [ "${ok:-N}" = "y" ] || [ "${ok:-N}" = "Y" ] || { echo "Not downloaded."; exit 1; }
  ollama pull "$MODEL"
fi
echo "Checking one real local inference request..."
cd "$(dirname "$0")/.."
.venv/bin/python -m adaptix.localdemo check --inference || true
