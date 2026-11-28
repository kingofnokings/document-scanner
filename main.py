"""Ponto de entrada do Scanner de Documentos.

Execute com:  python main.py
"""

import gc
import sys
import tkinter as tk
from multiprocessing import freeze_support

from interface.janela import AplicativoScanner


def main():
    """Cria a janela do Tkinter e entrega o controle ao aplicativo.

    Com `--verificar`, apenas confere se o Tesseract e os idiomas estão
    disponíveis e encerra (útil para diagnosticar a instalação).
    """
    freeze_support()  # necessário no executável do Windows (PyInstaller); inofensivo fora dele
    if "--verificar" in sys.argv:
        from nucleo.verificar_ambiente import principal
        raise SystemExit(principal())

    raiz = tk.Tk()
    aplicativo = AplicativoScanner(raiz)

    # O Tkinter exige que a janela (o interpretador Tcl) seja liberada pela MESMA
    # thread que a criou. Este programa tem threads de segundo plano (câmera, OCR);
    # se o coletor de lixo de uma delas recolhesse a janela, o Python abortaria com
    # "Tcl_AsyncDelete: async handler deleted by the wrong thread". `gc.freeze()` tira
    # a janela e seus componentes da coleta automática: eles são liberados
    # normalmente quando o programa encerra, na thread principal.
    gc.freeze()

    raiz.mainloop()
    del aplicativo


if __name__ == "__main__":
    main()
