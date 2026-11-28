"""Gera o executável do programa para o sistema operacional ATUAL.

Uso (na raiz do projeto):
    python -m pip install pyinstaller
    python ferramentas/empacotar.py

O resultado fica em `dist/ScannerDocumentos/` (uma pasta pronta para ser
compactada). O PyInstaller não faz "build cruzado": para gerar o .exe do
Windows é preciso rodar no Windows. É por isso que o repositório tem um workflow
do GitHub (.github/workflows/executavel-windows.yml) que faz isso sozinho.

O que vai dentro do executável:
    - o programa e as bibliotecas Python (OpenCV, NumPy, Pillow...);
    - `tessdata/`: os idiomas português e inglês do OCR;
    - no WINDOWS: também o Tesseract (executável + DLLs), copiado da instalação
      do computador que está gerando o pacote. Assim, quem recebe o .exe NÃO
      precisa instalar nada.
    - no LINUX o Tesseract NÃO é embutido (ele depende de bibliotecas do
      sistema); lá, use o executar.sh, que instala o Tesseract pelo apt.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from nucleo.localizar_tesseract import localizar_executavel  # noqa: E402

NOME = "ScannerDocumentos"
WINDOWS = sys.platform.startswith("win")


def _pasta_do_tesseract_no_windows():
    """Pasta da instalação real do Tesseract (a que contém tesseract.exe e as DLLs).

    Não usamos o primeiro resultado de `localizar_executavel()` porque, com o
    Chocolatey, ele pode ser um "atalho" (shim) sem as DLLs ao lado.
    """
    candidatas = [os.environ.get("TESSERACT_DIR", ""),
                  os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tesseract-OCR"),
                  os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Tesseract-OCR"),
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR")]
    for pasta in candidatas:
        if pasta and (Path(pasta) / "tesseract.exe").is_file():
            return Path(pasta)
    encontrado = localizar_executavel()
    if encontrado:
        return Path(encontrado).parent
    sys.exit("Tesseract não encontrado. Instale-o (winget install --id UB-Mannheim.TesseractOCR -e) "
             "ou defina TESSERACT_DIR com a pasta onde ele está.")


def preparar_tesseract_portatil():
    """Copia tesseract.exe e as DLLs (sem os idiomas) para build/tesseract. Só no Windows."""
    origem = _pasta_do_tesseract_no_windows()
    destino = RAIZ / "build" / "tesseract"
    shutil.rmtree(destino, ignore_errors=True)
    destino.mkdir(parents=True)
    for arquivo in origem.iterdir():
        if arquivo.suffix.lower() in (".exe", ".dll"):
            shutil.copy2(arquivo, destino)
    print(f"Tesseract copiado de {origem} ({len(list(destino.iterdir()))} arquivos)")
    return destino


def principal():
    separador = os.pathsep  # ";" no Windows e ":" no Linux, como o PyInstaller exige
    comando = [
        sys.executable, "-m", "PyInstaller", str(RAIZ / "main.py"),
        "--name", NOME, "--onedir", "--noconfirm", "--clean",
        "--distpath", str(RAIZ / "dist"),
        "--workpath", str(RAIZ / "build" / "pyinstaller"),
        "--specpath", str(RAIZ / "build"),
        "--add-data", f"{RAIZ / 'tessdata'}{separador}tessdata",
        "--hidden-import", "PIL._tkinter_finder",   # o Pillow carrega este módulo por nome
    ]
    if WINDOWS:
        comando += ["--add-data", f"{preparar_tesseract_portatil()}{separador}tesseract",
                    "--windowed"]                    # sem janela preta de console ao lado

    print("Executando:", " ".join(comando))
    subprocess.run(comando, check=True, cwd=RAIZ)

    # Um arquivinho de instruções ao lado do executável.
    pasta = RAIZ / "dist" / NOME
    executavel = f"{NOME}.exe" if WINDOWS else NOME
    (pasta / "LEIA-ME.txt").write_text(
        f"Scanner de Documentos\n\nPara abrir o programa, execute {executavel}.\n"
        "Não é preciso instalar Python nem configurar nada"
        + (".\n" if WINDOWS else ", mas o Tesseract OCR precisa estar instalado:\n"
           "  sudo apt install tesseract-ocr\n"),
        encoding="utf-8")
    print(f"\nPronto: {pasta}")


if __name__ == "__main__":
    principal()
