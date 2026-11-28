"""Encontra o Tesseract e os dados de idioma SEM exigir configuração do usuário.

O pytesseract só "conversa" com o Tesseract; o programa em si precisa estar
instalado. Normalmente isso obrigaria o usuário a:
    1. instalar o Tesseract;
    2. colocá-lo no PATH (variável de ambiente);
    3. instalar o pacote de idioma português.

Este módulo elimina os passos 2 e 3:
    - o EXECUTÁVEL é procurado automaticamente (pasta do projeto, PATH e os
      lugares onde cada sistema costuma instalá-lo);
    - os DADOS DE IDIOMA (português e inglês) já vêm dentro do projeto, na pasta
      `tessdata/`, e são usados no lugar dos do sistema.

Ordem de busca do executável (a primeira que existir vence):
    1. variável TESSERACT_CMD (para quem quiser forçar um caminho);
    2. pasta `tesseract/` do projeto (versão "portátil", usada no executável);
    3. o PATH do sistema;
    4. locais de instalação conhecidos (Windows, macOS, Linux).
"""

import os
import shutil
import sys
from pathlib import Path

# Idiomas que o programa usa (ver IDIOMA_PADRAO em ocr.py) e que vêm no projeto.
IDIOMAS_EMBUTIDOS = ("por", "eng")


def raiz_do_projeto():
    """Pasta onde ficam `tessdata/` e `tesseract/`.

    No código-fonte é a raiz do projeto; no executável empacotado pelo
    PyInstaller é a pasta temporária onde ele extrai os arquivos (`_MEIPASS`).
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def _nome_do_executavel():
    return "tesseract.exe" if sys.platform.startswith("win") else "tesseract"


def _locais_conhecidos():
    """Lugares onde cada sistema costuma instalar o Tesseract."""
    if sys.platform.startswith("win"):
        pastas = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                  os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
        locais = [Path(p) / "Tesseract-OCR" / "tesseract.exe" for p in pastas if p]
        locais.append(Path(r"C:\ProgramData\chocolatey\bin\tesseract.exe"))      # Chocolatey
        locais.append(Path.home() / "scoop" / "apps" / "tesseract" / "current" / "tesseract.exe")
        return locais
    if sys.platform == "darwin":
        return [Path("/opt/homebrew/bin/tesseract"),     # Homebrew (Apple Silicon)
                Path("/usr/local/bin/tesseract"),        # Homebrew (Intel)
                Path("/opt/local/bin/tesseract")]        # MacPorts
    return [Path("/usr/bin/tesseract"), Path("/usr/local/bin/tesseract"),
            Path("/snap/bin/tesseract")]


def _e_executavel(caminho):
    return caminho.is_file() and os.access(caminho, os.X_OK)


def localizar_executavel():
    """Caminho do executável do Tesseract, ou None se não for encontrado."""
    # 1) Caminho forçado pelo usuário.
    forcado = os.environ.get("TESSERACT_CMD")
    if forcado and _e_executavel(Path(forcado)):
        return str(forcado)

    # 2) Versão portátil dentro do projeto (ou do executável empacotado).
    portatil = raiz_do_projeto() / "tesseract" / _nome_do_executavel()
    if _e_executavel(portatil):
        return str(portatil)

    # 3) PATH do sistema.
    no_path = shutil.which("tesseract")
    if no_path:
        return no_path

    # 4) Locais de instalação conhecidos (dispensa configurar o PATH).
    for local in _locais_conhecidos():
        if _e_executavel(local):
            return str(local)
    return None


def localizar_tessdata():
    """Pasta `tessdata/` do projeto, se tiver todos os idiomas embutidos; senão None.

    Com None, o Tesseract usa os dados de idioma do próprio sistema.
    """
    pasta = raiz_do_projeto() / "tessdata"
    if all((pasta / f"{idioma}.traineddata").is_file() for idioma in IDIOMAS_EMBUTIDOS):
        return pasta
    return None


def configurar():
    """Aponta o pytesseract para o Tesseract e para os idiomas do projeto.

    Pode ser chamada várias vezes. Retorna o caminho do executável, ou None se o
    Tesseract não foi encontrado (nesse caso nada é alterado).
    """
    import pytesseract

    executavel = localizar_executavel()
    if executavel is None:
        return None
    pytesseract.pytesseract.tesseract_cmd = executavel

    # TESSDATA_PREFIX é a variável que o próprio Tesseract lê para achar os
    # idiomas. Definimos aqui, só para este programa: o usuário não precisa mexer.
    tessdata = localizar_tessdata()
    if tessdata is not None:
        os.environ["TESSDATA_PREFIX"] = str(tessdata)
    return executavel


def instrucoes_de_instalacao(plataforma=None):
    """Texto com o jeito mais simples de instalar o Tesseract no sistema."""
    plataforma = plataforma or sys.platform
    if plataforma.startswith("win"):
        como = ("  winget install --id UB-Mannheim.TesseractOCR -e\n"
                "ou baixe o instalador em https://github.com/UB-Mannheim/tesseract/wiki")
    elif plataforma == "darwin":
        como = "  brew install tesseract"
    else:
        como = ("  sudo apt install tesseract-ocr        (Debian, Ubuntu, Xubuntu)\n"
                "  sudo dnf install tesseract            (Fedora)\n"
                "  sudo pacman -S tesseract              (Arch)")
    return ("O Tesseract OCR não foi encontrado neste computador.\n"
            "Instale-o com:\n" + como + "\n"
            "Não é preciso configurar o PATH nem instalar o idioma português: o "
            "programa encontra o Tesseract sozinho e já traz os idiomas.\n"
            "Dica: os arquivos executar.bat (Windows) e executar.sh (Linux/macOS) "
            "fazem essa instalação por você.")
