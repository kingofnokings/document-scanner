"""Confere se o ambiente está pronto para o OCR.

Uso:  python -m nucleo.verificar_ambiente [--silencioso]

Código de saída (usado pelos scripts executar.bat / executar.sh):
    0  tudo certo
    2  Tesseract não encontrado
    3  Tesseract encontrado, mas faltam os idiomas (por, eng)
Com --silencioso, nada é impresso: só vale o código de saída.
"""

import sys

import pytesseract

from nucleo.localizar_tesseract import IDIOMAS_EMBUTIDOS, configurar, instrucoes_de_instalacao


def principal(silencioso=False):
    """Roda a verificação e devolve o código de saída."""
    def avisar(texto):
        if not silencioso:
            print(texto)

    executavel = configurar()
    if executavel is None:
        avisar(instrucoes_de_instalacao())
        return 2

    versao = str(pytesseract.get_tesseract_version())
    idiomas = set(pytesseract.get_languages(config=""))
    faltando = [i for i in IDIOMAS_EMBUTIDOS if i not in idiomas]
    if faltando:
        avisar(f"Tesseract {versao} encontrado em {executavel}, "
               f"mas faltam os idiomas: {', '.join(faltando)}.")
        return 3

    avisar(f"Tesseract {versao} encontrado em {executavel}")
    avisar(f"Idiomas disponíveis: {', '.join(i for i in IDIOMAS_EMBUTIDOS)} (OK)")
    return 0


if __name__ == "__main__":
    raise SystemExit(principal("--silencioso" in sys.argv))
