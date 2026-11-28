#!/usr/bin/env bash
# Scanner de Documentos - executa o programa no Linux (e no macOS), instalando o que faltar.
#
# Uso:   bash executar.sh
#
# O que este script faz, nesta ordem (e só o que for necessário):
#   1. confere o Python 3 e o Tkinter;
#   2. cria o ambiente virtual .venv;
#   3. instala as bibliotecas Python (requirements.txt);
#   4. confere o Tesseract OCR e, se faltar, instala pelo gerenciador de pacotes;
#   5. abre o programa.
# Não é preciso configurar variáveis de ambiente nem instalar o idioma português.

cd "$(dirname "$0")" || exit 1

info() { echo "==> $*"; }
erro() { echo; echo "[ERRO] $*" >&2; exit 1; }

# sudo só é necessário (e usado) se não formos o administrador.
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

# Descobre o gerenciador de pacotes do sistema.
if   command -v apt-get >/dev/null 2>&1; then GER="apt"
elif command -v dnf     >/dev/null 2>&1; then GER="dnf"
elif command -v pacman  >/dev/null 2>&1; then GER="pacman"
elif command -v brew    >/dev/null 2>&1; then GER="brew"
else GER=""; fi

# instalar_pacote <nome no apt> <nome no dnf> <nome no pacman> <nome no brew>
instalar_pacote() {
    case "$GER" in
        apt)    $SUDO apt-get update -qq && $SUDO apt-get install -y $1 ;;
        dnf)    $SUDO dnf install -y $2 ;;
        pacman) $SUDO pacman -S --noconfirm $3 ;;
        brew)   brew install $4 ;;
        *)      return 1 ;;
    esac
}

echo "=================================================="
echo "  Scanner de Documentos"
echo "=================================================="

# ---- 1) Python 3 e Tkinter ---------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    info "Python 3 não encontrado. Tentando instalar..."
    instalar_pacote python3 python3 python python3 || erro "Instale o Python 3 e execute novamente."
fi
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    info "Tkinter (a biblioteca das janelas) não encontrado. Tentando instalar..."
    instalar_pacote python3-tk python3-tkinter tk python-tk || true
    python3 -c "import tkinter" >/dev/null 2>&1 \
        || erro "Instale o Tkinter (Debian/Ubuntu: sudo apt install python3-tk) e execute novamente."
fi

# ---- 2) Ambiente virtual -------------------------------------------------------
if [ ! -x ".venv/bin/python" ]; then
    info "Criando o ambiente virtual (.venv)..."
    if ! python3 -m venv .venv >/dev/null 2>&1; then
        rm -rf .venv
        instalar_pacote python3-venv python3 python python3 || true   # no Debian/Ubuntu o venv é um pacote à parte
        python3 -m venv .venv || erro "Não foi possível criar o ambiente virtual (.venv)."
    fi
fi
VPY=".venv/bin/python"

# ---- 3) Bibliotecas Python (só na primeira vez ou quando requirements.txt mudar) ----
if ! cmp -s requirements.txt .venv/requirements.instalado.txt; then
    info "Instalando as bibliotecas Python (só na primeira vez; pode demorar alguns minutos)..."
    "$VPY" -m pip install --upgrade pip >/dev/null 2>&1
    "$VPY" -m pip install -r requirements.txt || erro "Falha ao instalar as bibliotecas Python."
    cp requirements.txt .venv/requirements.instalado.txt
fi
# O OpenCV precisa de duas bibliotecas gráficas do sistema; em instalações mínimas elas faltam.
if ! "$VPY" -c "import cv2" >/dev/null 2>&1; then
    info "O OpenCV precisa de bibliotecas gráficas do sistema. Tentando instalar..."
    instalar_pacote "libgl1 libglib2.0-0" mesa-libGL glib2 "" || true
    "$VPY" -c "import cv2" || erro "O OpenCV não pôde ser carregado. Veja a mensagem acima."
fi

# ---- 4) Tesseract OCR ---------------------------------------------------------------
if ! "$VPY" -m nucleo.verificar_ambiente --silencioso; then
    info "O Tesseract OCR não foi encontrado. Tentando instalar..."
    instalar_pacote tesseract-ocr tesseract tesseract tesseract || true
    if ! "$VPY" -m nucleo.verificar_ambiente --silencioso; then
        "$VPY" -m nucleo.verificar_ambiente   # mostra as instruções de instalação
        exit 1
    fi
fi

# ---- 5) Abrir o programa ---------------------------------------------------------------
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ] && [ "$(uname -s)" != "Darwin" ]; then
    echo "[AVISO] Nenhum ambiente gráfico detectado (DISPLAY vazio). Execute em uma sessão com interface."
fi
info "Abrindo o programa..."
exec "$VPY" main.py
