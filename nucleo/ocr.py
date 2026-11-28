"""OCR (reconhecimento de texto) com o Tesseract.

Fluxo de `extrair_texto`:
    1. A imagem é preparada (`preparar_para_ocr`): margem cortada, ampliada,
       com a iluminação corrigida e uma borda branca.
    2. Cria-se uma segunda versão, realçada, útil para fundos coloridos.
    3. Várias leituras (combinando versão da imagem x modo de layout/PSM) rodam
       EM PARALELO em threads, cada uma chamando o Tesseract.
    4. Fica a leitura de melhor avaliação: alta confiança média, sem perder
       muito texto em relação às demais.

A "confiança" é uma estimativa de qualidade do Tesseract, não uma porcentagem
de acertos.
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import cv2
import numpy as np
import pytesseract

from nucleo.localizar_tesseract import configurar, instrucoes_de_instalacao

# Confiança mínima (0 a 100) para uma palavra contar como "confiável".
CONFIANCA_MINIMA = 40

# --oem 1: usa só o motor LSTM (rede neural), o mais preciso do Tesseract 4+.
# --dpi 300: informa a resolução; sem isso o Tesseract tenta adivinhar.
# preserve_interword_spaces: mantém o espaçamento entre palavras (útil em tabelas).
CONFIG_BASE = "--oem 1 --dpi 300 -c preserve_interword_spaces=1"

# Cada tentativa combina uma versão da imagem com um modo de layout (PSM):
# PSM 6 = trata a página como um bloco único de texto. Foi o melhor nas fotos
#         de folhas inteiras: o PSM 3 "picotava" linhas ao lado de logotipos.
# PSM 3 = detecta blocos e colunas sozinho (útil em layouts com colunas);
# PSM 11 = procura texto espalhado (bom para identidades, crachás e cartões).
# Todas rodam ao mesmo tempo e o programa fica com a de melhor avaliação.
TENTATIVAS = (
    ("cinza", 6),
    ("realce", 6),
    ("cinza", 3),
    ("cinza", 11),
)

# Cada Tesseract usa só 1 núcleo; assim as tentativas rodam em paralelo sem
# disputar o processador (precisa ser definido antes de chamar o Tesseract).
os.environ.setdefault("OMP_THREAD_LIMIT", "1")


@lru_cache(maxsize=None)
def _idiomas_instalados():
    """Idiomas que o Tesseract tem instalados (consultado uma vez e guardado em cache).

    É a primeira vez que o Tesseract é usado, então é aqui que ele é localizado
    (ver localizar_tesseract.py): o usuário não precisa configurar o PATH nem
    instalar o idioma português, que já vem na pasta `tessdata/` do projeto.
    """
    configurar()
    try:
        return frozenset(pytesseract.get_languages(config=""))
    except pytesseract.TesseractNotFoundError as erro:
        raise RuntimeError(instrucoes_de_instalacao()) from erro


def _escolher_idioma(idioma):
    """Mantém só os idiomas instalados. Ex.: 'por+eng' vira 'por' sem o 'eng'."""
    pedidos = [i for i in idioma.strip().split("+") if i]
    instalados = _idiomas_instalados()
    disponiveis = [i for i in pedidos if i in instalados]
    if not disponiveis:
        raise RuntimeError(
            f"Nenhum idioma de '{idioma}' está instalado no Tesseract. "
            "Instale o pacote de idioma (ex.: tesseract-ocr-por)."
        )
    return "+".join(disponiveis)


def _validar(imagem):
    """Recusa entradas que o OCR não consegue tratar, com mensagens claras."""
    if not isinstance(imagem, np.ndarray) or imagem.size == 0:
        raise ValueError("A imagem está vazia ou não é um array NumPy.")
    if imagem.dtype != np.uint8 or imagem.ndim not in (2, 3):
        raise ValueError("Use uma imagem uint8 em cinza, BGR ou BGRA.")
    if imagem.ndim == 3 and imagem.shape[2] not in (3, 4):
        raise ValueError("A imagem colorida deve ter 3 ou 4 canais.")
    if min(imagem.shape[:2]) < 3:
        raise ValueError("A imagem é pequena demais para reconhecer texto.")


def _remover_alpha(imagem):
    """Converte BGRA em BGR, pintando as áreas transparentes de branco."""
    if imagem.ndim == 3 and imagem.shape[2] == 4:
        alpha = imagem[:, :, 3:4].astype(np.float32) / 255
        return (imagem[:, :, :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
    return imagem


def _recortar_margem(imagem, proporcao=0.015):
    """Corta uma faixa fina das bordas.

    Depois da transformação de perspectiva, sobram restos do fundo ou da borda
    da folha, que o Tesseract costuma ler como caracteres falsos (|, l, _).
    """
    h, w = imagem.shape[:2]
    dy, dx = int(h * proporcao), int(w * proporcao)
    if h - 2 * dy < 20 or w - 2 * dx < 20:
        return imagem
    return imagem[dy:h - dy, dx:w - dx]


def _ampliar(cinza, alvo=2400, limite=4.0):
    """Amplia letras pequenas: até 4x, em direção a 2400 pixels no maior lado.

    O Tesseract lê melhor letras com uns 20 a 30 pixels de altura; imagens
    pequenas (como uma identidade de 400 px) precisam de mais ampliação.
    """
    escala = min(limite, alvo / max(cinza.shape))
    if escala > 1:
        cinza = cv2.resize(cinza, None, fx=escala, fy=escala,
                           interpolation=cv2.INTER_CUBIC)
    return cinza


def _corrigir_iluminacao(cinza):
    """Divide a imagem pelo "fundo" (versão muito borrada) para tirar sombras."""
    fundo = cv2.GaussianBlur(cinza, (0, 0), 25)
    h, w = fundo.shape
    interior = fundo[h // 10:h - h // 10, w // 10:w - w // 10]
    variacao = np.percentile(interior, 90) - np.percentile(interior, 10)
    if variacao <= 12:
        return cinza
    cinza = cv2.divide(cinza, np.maximum(fundo, 1), scale=255)
    return cv2.normalize(cinza, None, 0, 255, cv2.NORM_MINMAX)


def _versao_realce(bgr):
    """Versão alternativa para documentos com fundo colorido.

    Usa o maior valor entre os canais B, G e R de cada pixel (o canal V do HSV).
    Um fundo colorido (arco-íris, roxo, azul) fica claro nesse canal, enquanto
    o texto preto continua escuro. Depois aplica CLAHE (contraste local) e um
    leve aumento de nitidez.
    """
    if bgr.ndim == 3:
        canal = bgr.max(axis=2)
    else:
        canal = bgr.copy()
    canal = _corrigir_iluminacao(_ampliar(canal))
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    canal = clahe.apply(canal)
    borrada = cv2.GaussianBlur(canal, (0, 0), 1.5)
    return cv2.addWeighted(canal, 1.5, borrada, -0.5, 0)  # unsharp mask


def preparar_para_ocr(imagem, progresso=None):
    """Prepara a imagem principal em escala de cinza, ajustando a iluminação.

    progresso é opcional e recebe (mensagem, imagem) para mostrar as etapas.
    A detecção da folha e a perspectiva continuam no main.
    """
    avisar = progresso or (lambda mensagem, imagem: None)
    _validar(imagem)
    # Remove transparência e corta a faixa fina das bordas (restos do fundo).
    imagem = _recortar_margem(_remover_alpha(imagem))

    if imagem.ndim == 3:
        cinza = cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY)
    else:
        cinza = imagem.copy()

    # Amplia letras pequenas e avisa a interface do progresso.
    cinza = _ampliar(cinza)
    avisar("Melhorando imagem para OCR…", cinza)

    # Imagem de uma cor só (sem nada para ler): devolve página em branco.
    if cinza.max() == cinza.min():
        return np.full_like(cinza, 255)
    cinza = _corrigir_iluminacao(cinza)
    avisar("Melhorando imagem para OCR…", cinza)

    # Borda branca: o Tesseract lê pior quando o texto encosta na margem.
    return _adicionar_borda(cinza)


def _adicionar_borda(cinza):
    """Acrescenta 15 px de borda branca em volta da imagem."""
    return cv2.copyMakeBorder(cinza, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)


def extrair_texto(imagem, idioma="por+eng", progresso=None, *, retornar_detalhes=False):
    """Lê o documento testando versões da imagem e modos de leitura.

    idioma usa o formato do Tesseract: 'por', 'eng' ou 'por+eng' (padrão,
    porque documentos brasileiros costumam ter termos em inglês).

    Fica com a tentativa de melhor avaliação. Retorna texto por padrão;
    retornar_detalhes=True inclui a avaliação, a imagem lida e as caixas
    de cada palavra. Confiança é uma estimativa de qualidade, não uma
    porcentagem de acerto.
    """
    if not isinstance(idioma, str) or not idioma.strip():
        raise ValueError("Informe um idioma, como 'por' ou 'eng'.")
    avisar = progresso or (lambda mensagem, imagem: None)
    idioma = _escolher_idioma(idioma)

    # 1) Versão principal da imagem (cinza, com iluminação corrigida).
    versoes = {"cinza": preparar_para_ocr(imagem, progresso)}

    # Página em branco: não há o que ler, devolve resultado vazio sem chamar o Tesseract.
    if versoes["cinza"].max() == versoes["cinza"].min():
        avisar("OCR concluído.", versoes["cinza"])
        vazio = {"texto": "", "confianca_media": 0.0, "caracteres_total": 0,
                 "caracteres_confiaveis": 0, "palavras": [], "imagem": versoes["cinza"]}
        return vazio if retornar_detalhes else ""

    # 2) Versão "realce": só é gerada se alguma tentativa pedir (veja TENTATIVAS).
    if any(nome == "realce" for nome, _ in TENTATIVAS):
        original = _recortar_margem(_remover_alpha(imagem))
        versoes["realce"] = _adicionar_borda(_versao_realce(original))

    def ler(nome, psm):
        """Uma leitura: roda o Tesseract na versão `nome` com o modo de layout `psm`."""
        dados = pytesseract.image_to_data(
            versoes[nome], lang=idioma, config=f"{CONFIG_BASE} --psm {psm}",
            output_type=pytesseract.Output.DICT, timeout=60,
        )
        texto, confianca, cobertura, palavras = _avaliar_resultado(dados)
        return texto, confianca, cobertura, versoes[nome], palavras

    # Cada leitura é um processo separado do Tesseract, então threads bastam
    # para rodar todas ao mesmo tempo.
    # 3) Roda todas as leituras em paralelo e vai juntando os resultados
    #    conforme terminam. Uma leitura que estoura o tempo é só ignorada.
    tentativas = []
    ultima_falha = None
    avisar(f"Extraindo texto… (0 de {len(TENTATIVAS)} leituras)", versoes["cinza"])
    with ThreadPoolExecutor(max_workers=min(len(TENTATIVAS), os.cpu_count() or 1)) as grupo:
        futuros = [grupo.submit(ler, nome, psm) for nome, psm in TENTATIVAS]
        for pronta, futuro in enumerate(as_completed(futuros), 1):
            try:
                tentativas.append(futuro.result())
            except RuntimeError as erro:  # pytesseract usa RuntimeError no timeout
                ultima_falha = erro
            avisar(f"Extraindo texto… ({pronta} de {len(TENTATIVAS)} leituras)", None)

    if not tentativas:
        raise RuntimeError("O OCR excedeu o tempo limite em todas as tentativas.") from ultima_falha
    # 4) Escolha da melhor leitura. Favorece a confiança, mas penaliza leituras
    #    que acharam bem menos texto: nota = confiança x (cobertura relativa)^0,25.
    maior_cobertura = max(t[2] for t in tentativas) or 1
    melhor = max(tentativas, key=lambda t: t[1] * (t[2] / maior_cobertura) ** 0.25)
    avisar("OCR concluído.", melhor[3])
    if retornar_detalhes:
        return {
            "texto": melhor[0],
            "confianca_media": melhor[1],
            "caracteres_total": sum(c.isalnum() for c in melhor[0]),
            "caracteres_confiaveis": melhor[2],
            "palavras": melhor[4],
            "imagem": melhor[3],
        }
    return melhor[0]


def _avaliar_resultado(dados):
    """Monta o texto e calcula confiança, caracteres confiáveis e caixas."""
    linhas = {}
    confiancas = []
    palavras = []
    cobertura = 0
    for i, palavra in enumerate(dados["text"]):
        palavra = palavra.strip()
        if not palavra:
            continue
        # Agrupa as palavras nas linhas reconhecidas pelo Tesseract.
        linha = tuple(dados[campo][i] for campo in (
            "page_num", "block_num", "par_num", "line_num"
        ))
        linhas.setdefault(linha, []).append(palavra)
        if any(c.isalnum() for c in palavra):
            confianca = max(0.0, float(dados["conf"][i]))
            confiancas.append(confianca)
            # Caixa da palavra: usada pela interface para desenhar retângulos.
            palavras.append((dados["left"][i], dados["top"][i],
                             dados["width"][i], dados["height"][i], confianca))
            if confianca >= CONFIANCA_MINIMA:
                cobertura += sum(c.isalnum() for c in palavra)

    texto = "\n".join(" ".join(p) for p in linhas.values())
    media = float(np.mean(confiancas)) if confiancas else 0.0
    return texto, media, cobertura, palavras
