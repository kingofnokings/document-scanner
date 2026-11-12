import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from queue import Queue, Empty
from threading import Thread

from ocr import extrair_texto, CONFIANCA_MINIMA

import cv2
import numpy as np
from PIL import Image, ImageTk

# Lado maior (em pixels) usado na detecção da folha. Reduzir a imagem deixa a
# detecção rápida e ignora detalhes pequenos (textura da mesa, letras). O
# recorte final é feito na imagem original, sem perder qualidade.
LADO_DETECCAO = 500

# Índice da câmera padrão. Se houver mais de uma câmera, teste 1, 2 etc.
INDICE_CAMERA = 0

# O documento precisa ocupar pelo menos 15% da foto. Isso evita escolher um
# retângulo pequeno, como a foto 3x4 dentro de uma identidade.
AREA_MINIMA_RELATIVA = 0.15

# Pelo menos metade do contorno precisa estar sobre bordas reais da imagem.
APOIO_MINIMO = 0.5


def order_points(pts):
    # Inicializa uma lista de coordenadas que serão ordenadas
    # na ordem: topo-esquerdo, topo-direito, baixo-direito, baixo-esquerdo
    rect = np.zeros((4, 2), dtype="float32")

    # O topo-esquerdo terá a menor soma, o baixo-direito terá a maior soma
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]

    # O topo-direito terá a menor diferença, o baixo-esquerdo terá a maior diferença
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]

    return rect


def four_point_transform(image, points):
    # rect recebe os quatro cantos ordenados
    rect = order_points(points)

    # Desempacotamento dos quatro cantos do retângulo
    top_left, top_right, bottom_right, bottom_left = rect

    # Cálculo das dimensões aproximadas do documento.
    width_a = np.linalg.norm(bottom_right - bottom_left)
    width_b = np.linalg.norm(top_right - top_left)

    height_a = np.linalg.norm(top_right - bottom_right)
    height_b = np.linalg.norm(top_left - bottom_left)

    max_width = int(max(width_a, width_b))
    max_height = int(max(height_a, height_b))

    if max_width < 2 or max_height < 2:
        return None

    # Definir os quatro cantos da imagem de destino.
    destination = np.array(
        [
            [0, 0],
            [max_width - 1, 0],
            [max_width - 1, max_height - 1],
            [0, max_height - 1]
        ],
        dtype="float32"
    )

    # Calcular a matriz de transformação projetiva.
    matrix = cv2.getPerspectiveTransform(rect, destination)

    # Aplicar a transformação à imagem original colorida.
    warped = cv2.warpPerspective(
        image,
        matrix,
        (max_width, max_height)
    )

    return warped


def _apagar_texto(canal):
    """Fechamento morfológico: o "branco" cobre as letras e a folha fica lisa.

    Assim sobram apenas as bordas grandes (folha x fundo), e não as milhares
    de bordas das letras.
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    return cv2.morphologyEx(canal, cv2.MORPH_CLOSE, kernel)


def _canny_automatico(canal):
    """Canny com limiares calculados a partir do brilho típico da imagem."""
    mediana = float(np.median(canal))
    return cv2.Canny(cv2.GaussianBlur(canal, (5, 5), 0),
                     0.15 * mediana, 0.3 * mediana + 30)


def _mascaras(image):
    """Gera várias imagens binárias; cada uma funciona melhor num tipo de foto.

    - cinza: brilho comum (bom para fundo escuro);
    - brancura: brilho menos saturação. Papel é claro e SEM cor, enquanto
      madeira, mesa e tecido têm cor. Bom para fundo claro e colorido.
    Para cada canal: Otsu (separa regiões) e Canny (encontra as bordas).
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    canais = {
        "cinza": _apagar_texto(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)),
        "brancura": _apagar_texto(cv2.subtract(hsv[:, :, 2], hsv[:, :, 1])),
    }
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    mascaras = []
    for canal in canais.values():
        _, otsu = cv2.threshold(cv2.GaussianBlur(canal, (5, 5), 0), 0, 255,
                                cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        mascaras.append(("otsu", cv2.morphologyEx(otsu, cv2.MORPH_OPEN, kernel), 0))
        bordas = _canny_automatico(canal)
        mascaras.append(("canny", cv2.dilate(bordas, np.ones((3, 3), np.uint8)), 0))
        # Versão "grossa": fecha falhas da borda causadas por sombras.
        # Depois o quadrilátero é afastado 5 px para compensar.
        mascaras.append(("canny", cv2.dilate(bordas, np.ones((11, 11), np.uint8)), 5))
    return mascaras, canais


def _quadrilatero(contorno):
    """Transforma um contorno em 4 cantos, ou None se não parecer uma folha.

    O casco convexo (convexHull) "passa por cima" de dedos, clipes e sombras
    que entram na borda. Depois aumentamos a tolerância do approxPolyDP até
    o polígono ter exatamente 4 vértices.
    """
    casco = cv2.convexHull(contorno)
    perimetro = cv2.arcLength(casco, True)
    for tolerancia in np.linspace(0.01, 0.08, 15):
        aprox = cv2.approxPolyDP(casco, tolerancia * perimetro, True)
        if len(aprox) == 4:
            return aprox.reshape(4, 2).astype(np.float32)
        if len(aprox) < 4:
            break
    return None


def _afastar(cantos, pixels):
    """Afasta cada canto do centro (compensa a borda engrossada pela dilatação)."""
    direcao = cantos - cantos.mean(axis=0)
    direcao /= np.linalg.norm(direcao, axis=1, keepdims=True)
    return (cantos + direcao * pixels * 1.41).astype(np.float32)


def _reta(p, q):
    """Reta no formato a·x + b·y = c que passa pelos pontos p e q."""
    a, b = q[1] - p[1], p[0] - q[0]
    return a, b, a * p[0] + b * p[1]


def _intersecao(r1, r2):
    a1, b1, c1 = r1
    a2, b2, c2 = r2
    det = a1 * b2 - a2 * b1
    if abs(det) < 1e-6:
        return None  # retas paralelas
    return np.array([(c1 * b2 - c2 * b1) / det, (a1 * c2 - a2 * c1) / det], np.float32)


def _encaixar_nas_retas(cantos, segmentos, altura, largura):
    """Ajusta cada lado do quadrilátero à reta real da borda da folha.

    Quando uma sombra "come" um canto, o quadrilátero sai torto. Aqui, para
    cada lado, procuramos o maior segmento de reta (Hough) quase paralelo e
    próximo dele. Os novos cantos são as interseções dessas retas.
    """
    if segmentos is None:
        return cantos
    # Ordena os cantos em volta do centro para percorrer os lados em sequência.
    rel = cantos - cantos.mean(axis=0)
    cantos = cantos[np.argsort(np.arctan2(rel[:, 1], rel[:, 0]))]
    menor_lado = min(altura, largura)

    retas = []
    for i in range(4):
        p1, p2 = cantos[i], cantos[(i + 1) % 4]
        lado = p2 - p1
        comprimento = np.linalg.norm(lado)
        angulo = np.arctan2(lado[1], lado[0])
        normal = np.array([-lado[1], lado[0]]) / comprimento
        melhor, melhor_comp = None, 0.3 * comprimento
        for x1, y1, x2, y2 in segmentos.reshape(-1, 4):
            seg = np.array([x2 - x1, y2 - y1], np.float32)
            diferenca = abs((np.arctan2(seg[1], seg[0]) - angulo + np.pi / 2) % np.pi
                            - np.pi / 2)
            meio = np.array([(x1 + x2) / 2, (y1 + y2) / 2])
            posicao = np.dot(meio - p1, lado) / comprimento ** 2
            if (diferenca <= np.radians(12)                      # quase paralelo
                    and abs(np.dot(meio - p1, normal)) <= 0.12 * menor_lado  # perto
                    and -0.1 < posicao < 1.1                     # ao lado do lado
                    and np.linalg.norm(seg) > melhor_comp):
                melhor, melhor_comp = (x1, y1, x2, y2), np.linalg.norm(seg)
        if melhor is None:
            retas.append(_reta(p1, p2))
        else:
            retas.append(_reta(np.array(melhor[:2], float), np.array(melhor[2:], float)))

    novos = [_intersecao(retas[i - 1], retas[i]) for i in range(4)]
    if any(p is None for p in novos):
        return cantos
    novos = np.array(novos, np.float32)
    folga = 0.03 * max(altura, largura)
    dentro = ((novos[:, 0] >= -folga) & (novos[:, 0] <= largura + folga)
              & (novos[:, 1] >= -folga) & (novos[:, 1] <= altura + folga)).all()
    if not dentro or not cv2.isContourConvex(novos.reshape(-1, 1, 2).astype(np.int32)):
        return cantos
    novos[:, 0] = novos[:, 0].clip(0, largura - 1)
    novos[:, 1] = novos[:, 1].clip(0, altura - 1)
    return novos


def _apoio_nas_bordas(cantos, bordas):
    """Fração do contorno do quadrilátero que passa sobre bordas reais (0 a 1).

    Um quadrilátero certo tem os 4 lados em cima da borda da folha. Um
    errado corta o meio do papel ou do fundo, onde não há borda.
    Trechos colados na margem da foto não contam (não dá para saber).
    """
    linha = np.zeros(bordas.shape, np.uint8)
    cv2.polylines(linha, [cantos.astype(np.int32)], True, 255, 1)
    m = 4
    linha[:m, :] = 0
    linha[-m:, :] = 0
    linha[:, :m] = 0
    linha[:, -m:] = 0
    total = cv2.countNonZero(linha)
    if total < 0.25 * cv2.arcLength(cantos.reshape(-1, 1, 2), True):
        return 0.0  # quase todo na margem: provavelmente é a própria foto
    return cv2.countNonZero(cv2.bitwise_and(linha, bordas)) / total


def _melhor_candidato(mascaras, bordas_finas, bordas_nota, h, w, apoio_minimo=None):
    """Testa os contornos de cada máscara e devolve o quadrilátero de maior nota."""
    area_foto = h * w
    segmentos = cv2.HoughLinesP(bordas_finas, 1, np.pi / 180, 40,
                                minLineLength=0.15 * min(h, w), maxLineGap=15)
    referencia = cv2.dilate(bordas_nota, np.ones((7, 7), np.uint8))

    apoio_minimo = APOIO_MINIMO if apoio_minimo is None else apoio_minimo
    melhor_nota, melhores_cantos = 0.0, None
    for tipo, mascara, afastar in mascaras:
        # Nas bordas do Canny, o contorno de DENTRO do retângulo é a folha,
        # mesmo que a linha esteja grudada em outras (tábua, outra folha).
        modo = cv2.RETR_LIST if tipo == "canny" else cv2.RETR_EXTERNAL
        contornos, _ = cv2.findContours(mascara, modo, cv2.CHAIN_APPROX_SIMPLE)
        contornos = sorted(contornos, key=lambda c: cv2.contourArea(cv2.convexHull(c)),
                           reverse=True)
        for contorno in contornos[:6]:
            if cv2.contourArea(cv2.convexHull(contorno)) < AREA_MINIMA_RELATIVA * area_foto:
                break
            cantos = _quadrilatero(contorno)
            if cantos is None:
                continue
            if afastar:
                cantos = _afastar(cantos, afastar)
            cantos = _encaixar_nas_retas(cantos, segmentos, h, w)
            fracao_area = cv2.contourArea(cantos) / area_foto
            if not AREA_MINIMA_RELATIVA <= fracao_area <= 0.98:
                continue
            apoio = _apoio_nas_bordas(cantos, referencia)
            nota = apoio * fracao_area ** 0.5
            if apoio >= apoio_minimo and nota > melhor_nota:
                melhor_nota, melhores_cantos = nota, cantos
    return melhores_cantos


def scan_detection(image):
    """Encontra os quatro cantos do documento na imagem.

    1. Reduz a foto e apaga o texto (só sobram as bordas grandes).
    2. Gera candidatos com Otsu e Canny em dois canais (cinza e brancura).
    3. Ajusta os lados de cada candidato às retas reais (Hough).
    4. Dá uma nota: quanto do contorno está sobre bordas reais e o tamanho.
    5. Se nada servir, tenta de novo com CLAHE (bordas fracas, fundo branco).
    Retorna (cantos, encontrou). Se nada for confiável, devolve os cantos da
    imagem inteira e encontrou = False.
    """
    altura, largura = image.shape[:2]
    cantos_imagem = np.array(
        [[0, 0], [largura - 1, 0], [largura - 1, altura - 1], [0, altura - 1]],
        dtype="float32",
    )

    escala = min(1.0, LADO_DETECCAO / max(altura, largura))
    pequena = cv2.resize(image, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)
    h, w = pequena.shape[:2]

    # 1ª passada: canais normais
    mascaras, canais = _mascaras(pequena)
    bordas_cinza = _canny_automatico(canais["cinza"])
    cantos = _melhor_candidato(
        mascaras, bordas_cinza, bordas_cinza | _canny_automatico(canais["brancura"]), h, w)

    # 2ª passada (último recurso): folha branca sobre fundo branco.
    # O CLAHE realça diferenças pequenas de brilho em cada região da foto e faz
    # a borda fraca da folha aparecer. Não é usado sempre porque também realça
    # texturas (madeira, piso), que confundiriam as fotos que já funcionam.
    if cantos is None:
        contraste = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(canais["cinza"])
        bordas = _canny_automatico(contraste)
        mascaras_contraste = [
            ("canny", cv2.dilate(bordas, np.ones((3, 3), np.uint8)), 0),
            ("canny", cv2.dilate(bordas, np.ones((11, 11), np.uint8)), 5),
        ]
        # Exige mais apoio nas bordas: é melhor não achar do que achar errado.
        cantos = _melhor_candidato(mascaras_contraste, bordas, bordas, h, w, apoio_minimo=0.75)

    if cantos is None:
        return cantos_imagem, False
    return cantos / escala, True


def desenhar_contorno(image, cantos):
    """Desenha o contorno detectado sobre a original (mostra onde a folha foi achada)."""
    saida = image.copy()
    espessura = max(2, max(image.shape[:2]) // 300)
    pontos = order_points(cantos).astype(np.int32)
    cv2.polylines(saida, [pontos], True, (86, 160, 33), espessura, cv2.LINE_AA)
    return saida


def efeito_scanner(imagem, lado=2200):
    """Deixa o documento com cara de escaneado: papel branco e texto preto nítido.

    1. Amplia até ~2200 px no maior lado, para as letras ficarem lisas.
    2. Estima o "fundo" do papel (sem o texto) e divide a imagem por ele.
       Isso remove sombras e diferenças de iluminação.
    3. Aplica nitidez (unsharp mask: imagem + diferença para a versão borrada).
    4. Curva de tons: tudo que é quase branco vira branco puro e o texto escurece,
       mas as bordas das letras continuam suaves (fica mais bonito que um
       threshold puro, que deixa as letras serrilhadas).
    O OCR continua lendo a imagem colorida original: nos testes, o preto e
    branco forte deixou letras pequenas "gordas" e piorou um pouco a leitura.
    """
    cinza = cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY) if imagem.ndim == 3 else imagem.copy()
    escala = lado / max(cinza.shape)
    if escala > 1:
        cinza = cv2.resize(cinza, None, fx=escala, fy=escala, interpolation=cv2.INTER_CUBIC)

    tamanho = max(15, (max(cinza.shape) // 60) | 1)  # maior que a grossura das letras
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (tamanho, tamanho))
    fundo = cv2.medianBlur(cv2.morphologyEx(cinza, cv2.MORPH_CLOSE, kernel), 21)
    plano = cv2.divide(cinza, fundo, scale=255)

    borrada = cv2.GaussianBlur(plano, (0, 0), 1.2)
    nitida = cv2.addWeighted(plano, 1.8, borrada, -0.8, 0)

    claro, escuro = 215.0, 95.0  # acima de "claro" vira branco; abaixo de "escuro", preto
    tons = np.clip((nitida.astype(np.float32) - escuro) / (claro - escuro), 0, 1)
    return (tons ** 1.3 * 255).astype(np.uint8)


def preprocessamento(imagem):
    """Etapas do pré-processamento: escala de cinza, blur gaussiano e threshold (Otsu)."""
    cinza = cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY)
    desfocada = cv2.GaussianBlur(cinza, (5, 5), 0)
    _, binaria = cv2.threshold(desfocada, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return cinza, desfocada, binaria


class ImagePreprocessorApp:
    # Cores da interface
    COR_FUNDO = "#F3F6F5"
    COR_BARRA = "#E9EFED"
    COR_CARTAO = "#FFFFFF"
    COR_BORDA = "#D5DEDC"
    COR_PRIMARIA = "#1FC6A0"
    COR_SECUNDARIA = "#17363F"
    COR_TEXTO = "#0F2A33"
    COR_SUAVE = "#5C6B69"
    COR_DESATIVADA = "#DDE5E3"
    COR_TEXTO_DESATIVADO = "#8A9896"

    # Abas: (chave, rótulo, cartões na ordem, mostrar texto?, mostrar botões de salvar?)
    MODOS = [
        ("pre", "Original + pré-processamento",
         ("original", "gray", "blur", "threshold", "warped"), False, False),
        ("digitalizada", "Original + digitalizada",
         ("original", "warped", "pb"), True, True),
        ("todos", "Todos os processos",
         ("original", "contorno", "gray", "blur", "threshold", "warped", "pb"), True, True),
    ]

    # Títulos dos cartões de imagem
    TITULOS = {
        "original": "ENTRADA",
        "contorno": "DETECÇÃO",
        "gray": "ESCALA DE CINZA",
        "blur": "GAUSSIAN BLUR",
        "threshold": "THRESHOLD",
        "warped": "DIGITALIZADA (COR)",
        "pb": "SCANNER P&B",
    }

    CARTOES_PROCESSO = ("contorno", "gray", "blur", "threshold", "warped", "pb")

    def __init__(self, root):
        self.root = root
        self.root.title("Scanner de Documentos")
        self.root.geometry("1280x820")
        self.root.minsize(1000, 700)
        self.root.configure(bg=self.COR_FUNDO)
        self.root.protocol("WM_DELETE_WINDOW", self.fechar)

        # Tema da barra de progresso (ttk) com as cores do app
        estilo = ttk.Style(self.root)
        estilo.theme_use("clam")
        estilo.configure("Horizontal.TProgressbar", background=self.COR_PRIMARIA,
                         troughcolor=self.COR_CARTAO, bordercolor=self.COR_CARTAO,
                         lightcolor=self.COR_PRIMARIA, darkcolor=self.COR_PRIMARIA)

        self.imagem = None
        self.nome_arquivo = ""
        self.cantos = None
        self.documento_encontrado = False
        self.warped = None
        self.scan_pb = None
        self.texto_ocr = ""      # resultado original do OCR (usado nas mensagens)
        self.originais = {}      # imagens PIL em tamanho real (para redimensionar)
        self.fotos = {}          # PhotoImage atual de cada cartão (precisa ficar referenciada)
        self.labels = {}
        self.areas = {}
        self.cartoes = {}
        self.botoes_modo = {}
        self.cores = {}          # cor original de cada botão (para reativar)
        self.agendados = {}
        self.processando = False
        self.fila = Queue()
        self.inicio_ocr = 0.0
        self.modo = "pre"

        # Estado da câmera ao vivo
        self.camera = None
        self.camera_after_id = None
        self.camera_frame = None
        self.camera_cantos = None

        self._montar_barra_lateral()
        self._montar_area_principal()
        self._selecionar_modo(self.modo)
        self._definir_estado_botoes()

    # layout
    def _montar_barra_lateral(self):
        barra = tk.Frame(self.root, bg=self.COR_BARRA, padx=20, pady=24, width=260)
        barra.pack(side="left", fill="y")
        barra.pack_propagate(False)

        tk.Label(barra, text="NOVO SCAN", bg=self.COR_BARRA, fg=self.COR_SECUNDARIA,
                 font=("Arial", 14, "bold")).pack(anchor="w")

        self.btn_carregar = self._botao(barra, "+  Escolher arquivo", self.carregar,
                                        self.COR_PRIMARIA, self.COR_TEXTO)
        self.btn_carregar.pack(fill="x", pady=(18, 4))

        self.btn_camera = self._botao(barra, "Câmera ao vivo", self.alternar_camera,
                                      self.COR_SECUNDARIA, "white")
        self.btn_camera.pack(fill="x", pady=4)

        self.btn_capturar = self._botao(barra, "Capturar quadro", self.capturar_quadro,
                                        self.COR_PRIMARIA, self.COR_TEXTO)
        self.btn_capturar.pack(fill="x", pady=4)

        self.btn_ajustar = self._botao(barra, "Ajustar cantos", self.ajustar_cantos,
                                       self.COR_SECUNDARIA, "white")
        self.btn_ajustar.pack(fill="x", pady=4)

        self.status_ocr = tk.StringVar(value="Escolha uma imagem ou inicie a câmera.")
        tk.Label(barra, textvariable=self.status_ocr, bg=self.COR_BARRA, fg=self.COR_TEXTO,
                 wraplength=200, justify="left", font=("Arial", 10)).pack(
                     fill="x", pady=(8, 8))

        # Começa escondida; é exibida só durante o processamento
        self.progresso_ocr = ttk.Progressbar(barra, mode="indeterminate")

        cartao = tk.Frame(barra, bg=self.COR_CARTAO, padx=12, pady=12,
                          highlightbackground=self.COR_BORDA, highlightthickness=1)
        cartao.pack(fill="x", pady=24)

        tk.Label(cartao, text="Avaliação do OCR", bg=self.COR_CARTAO,
                 fg=self.COR_SECUNDARIA, font=("Arial", 10, "bold")).pack(anchor="w")
        self.resumo_ocr = tk.StringVar(value="Processe a imagem para ver a avaliação.")
        tk.Label(cartao, textvariable=self.resumo_ocr, bg=self.COR_CARTAO,
                 fg=self.COR_TEXTO, wraplength=200, justify="left",
                 anchor="w").pack(fill="x", pady=(8, 8))
        tk.Label(cartao, text="Estimativa automática.",
                 bg=self.COR_CARTAO, fg=self.COR_SUAVE, wraplength=200,
                 justify="left", font=("Arial", 8)).pack(fill="x")

    def _montar_area_principal(self):
        principal = tk.Frame(self.root, bg=self.COR_FUNDO, padx=24, pady=20)
        principal.pack(side="left", fill="both", expand=True)

        principal.grid_columnconfigure(0, weight=1)
        principal.grid_rowconfigure(2, weight=4)   # imagens
        principal.grid_rowconfigure(4, weight=1)   # texto extraído

        tk.Label(principal, text="VISUALIZAÇÃO", bg=self.COR_FUNDO, fg=self.COR_SUAVE,
                 font=("Arial", 9, "bold")).grid(row=0, column=0, sticky="w")

        # Abas: escolhem quais etapas aparecem
        abas = tk.Frame(principal, bg=self.COR_FUNDO)
        abas.grid(row=1, column=0, sticky="w", pady=(6, 16))
        for chave, rotulo, _, _, _ in self.MODOS:
            botao = tk.Button(
                abas, text=rotulo, relief="flat", bd=0, font=("Arial", 10, "bold"),
                padx=16, pady=8, cursor="hand2",
                command=lambda c=chave: self._selecionar_modo(c))
            botao.pack(side="left", padx=(0, 8))
            self.botoes_modo[chave] = botao

        # Área das imagens (a grade é montada em _atualizar_layout)
        self.area_imagens = tk.Frame(principal, bg=self.COR_FUNDO)
        self.area_imagens.grid(row=2, column=0, sticky="nsew")
        for chave in self.TITULOS:
            self.cartoes[chave] = self._criar_cartao(self.area_imagens, self.TITULOS[chave], chave)

        # Ações abaixo das imagens
        acoes = tk.Frame(principal, bg=self.COR_FUNDO)
        acoes.grid(row=3, column=0, pady=18)

        self.btn_processar = self._botao(acoes, "Processar", self.processar,
                                         self.COR_PRIMARIA, self.COR_TEXTO, padx=30)
        self.btn_processar.pack(side="left", padx=6)

        self.frame_salvar = tk.Frame(acoes, bg=self.COR_FUNDO)
        self.frame_salvar.pack(side="left")

        self.btn_salvar = self._botao(self.frame_salvar, "Salvar imagem (cor)",
                                      lambda: self.salvar("cor"),
                                      self.COR_SECUNDARIA, "white")
        self.btn_salvar.pack(side="left", padx=6)

        self.btn_salvar_pb = self._botao(self.frame_salvar, "Salvar imagem (P&B)",
                                         lambda: self.salvar("pb"),
                                         self.COR_SECUNDARIA, "white")
        self.btn_salvar_pb.pack(side="left", padx=6)

        self.btn_salvar_texto = self._botao(self.frame_salvar, "Salvar texto (.txt)",
                                            self.salvar_texto,
                                            self.COR_SECUNDARIA, "white")
        self.btn_salvar_texto.pack(side="left", padx=6)

        # Texto extraído (aparece nas abas que o mostram) e editável
        self.quadro_texto = tk.LabelFrame(principal, text="Texto extraído", bg=self.COR_FUNDO,
                                          fg=self.COR_SECUNDARIA, font=("Arial", 10, "bold"),
                                          padx=8, pady=8)
        self.quadro_texto.grid(row=4, column=0, sticky="nsew")

        barra_rolagem = tk.Scrollbar(self.quadro_texto)
        barra_rolagem.pack(side="right", fill="y")
        self.caixa_texto = tk.Text(
            self.quadro_texto, wrap="word", height=4,
            bg=self.COR_CARTAO, fg=self.COR_TEXTO, relief="flat",
            padx=10, pady=10, undo=True, yscrollcommand=barra_rolagem.set)
        self.caixa_texto.pack(side="left", fill="both", expand=True)
        barra_rolagem.config(command=self.caixa_texto.yview)
        self.caixa_texto.bind("<KeyRelease>", lambda _e: self._definir_estado_botoes())

    def _criar_cartao(self, pai, titulo, chave):
        cartao = tk.Frame(pai, bg=self.COR_CARTAO,
                          highlightbackground=self.COR_BORDA, highlightthickness=1)

        tk.Label(cartao, text=titulo, bg=self.COR_CARTAO, fg=self.COR_SUAVE,
                 font=("Arial", 9, "bold")).pack(anchor="w", padx=10, pady=(8, 0))

        area = tk.Frame(cartao, bg=self.COR_CARTAO)
        area.pack(fill="both", expand=True, padx=8, pady=8)
        area.bind("<Configure>", lambda _e, c=chave: self._agendar_redimensionamento(c))
        self.areas[chave] = area

        label = tk.Label(area, text="Aguardando", bg=self.COR_CARTAO,
                         fg=self.COR_TEXTO_DESATIVADO)
        label.place(relx=0.5, rely=0.5, anchor="center")
        self.labels[chave] = label
        return cartao

    def _botao(self, pai, texto, comando, cor, cor_texto, padx=20):
        botao = tk.Button(
            pai, text=texto, command=comando, font=("Arial", 11, "bold"),
            bg=cor, fg=cor_texto, relief="flat", bd=0, padx=padx, pady=8,
            activebackground=cor, activeforeground=cor_texto,
            disabledforeground=self.COR_TEXTO_DESATIVADO)
        self.cores[botao] = (cor, cor_texto)
        return botao

    def _aplicar_estado(self, botao, ativo):
        if ativo:
            bg, fg = self.cores[botao]
            botao.config(state="normal", bg=bg, fg=fg, activebackground=bg, cursor="hand2")
        else:
            botao.config(state="disabled", bg=self.COR_DESATIVADA,
                         fg=self.COR_TEXTO_DESATIVADO,
                         disabledforeground=self.COR_TEXTO_DESATIVADO, cursor="arrow")

    def _definir_estado_botoes(self):
        ocupado = self.processando
        camera_ativa = self.camera is not None
        tem_imagem = self.imagem is not None and not camera_ativa

        self._aplicar_estado(self.btn_carregar, not ocupado)
        self._aplicar_estado(self.btn_camera, not ocupado)
        self.btn_camera.config(text="Fechar câmera" if camera_ativa else "Câmera ao vivo")
        self._aplicar_estado(self.btn_capturar, not ocupado and camera_ativa)
        self._aplicar_estado(self.btn_ajustar, not ocupado and tem_imagem)
        self._aplicar_estado(self.btn_processar, not ocupado and tem_imagem)
        self._aplicar_estado(self.btn_salvar, not ocupado and self.warped is not None)
        self._aplicar_estado(self.btn_salvar_pb, not ocupado and self.scan_pb is not None)
        self._aplicar_estado(self.btn_salvar_texto,
                             not ocupado and bool(self._texto_atual().strip()))

    def _iniciar_progresso(self):
        self.progresso_ocr.pack(fill="x")
        self.progresso_ocr.start(12)

    def _encerrar_progresso(self):
        self.progresso_ocr.stop()
        self.progresso_ocr.pack_forget()

    def _selecionar_modo(self, modo):
        self.modo = modo
        for chave, botao in self.botoes_modo.items():
            ativa = chave == modo
            botao.config(
                bg=self.COR_SECUNDARIA if ativa else self.COR_CARTAO,
                fg="white" if ativa else self.COR_SECUNDARIA,
                activebackground=self.COR_SECUNDARIA if ativa else self.COR_CARTAO,
                activeforeground="white" if ativa else self.COR_SECUNDARIA)
        self._atualizar_layout()

    def _config_modo(self):
        for chave, _, imagens, mostra_texto, mostra_salvar in self.MODOS:
            if chave == self.modo:
                return imagens, mostra_texto, mostra_salvar
        return ("original",), False, False

    def _atualizar_layout(self):
        imagens, mostra_texto, mostra_salvar = self._config_modo()

        for cartao in self.cartoes.values():
            cartao.grid_forget()
        for i in range(len(self.cartoes)):
            self.area_imagens.grid_columnconfigure(i, weight=0, uniform="")
            self.area_imagens.grid_rowconfigure(i, weight=0)

        # Até 3 cartões por linha; a grade cresce para baixo
        colunas = min(len(imagens), 3)
        linhas = (len(imagens) + colunas - 1) // colunas
        for c in range(colunas):
            self.area_imagens.grid_columnconfigure(c, weight=1, uniform="cartoes")
        for r in range(linhas):
            self.area_imagens.grid_rowconfigure(r, weight=1)

        for i, chave in enumerate(imagens):
            self.cartoes[chave].grid(row=i // colunas, column=i % colunas,
                                     padx=6, pady=6, sticky="nsew")

        if mostra_texto:
            self.quadro_texto.grid()
        else:
            self.quadro_texto.grid_remove()

        if mostra_salvar:
            self.frame_salvar.pack(side="left")
        else:
            self.frame_salvar.pack_forget()

        for chave in imagens:
            self._agendar_redimensionamento(chave)

    #câmera ao vivo
    def alternar_camera(self):
        if self.camera is not None:
            self.parar_camera()
        else:
            self.iniciar_camera()

    def iniciar_camera(self):
        if self.processando:
            return

        camera = cv2.VideoCapture(INDICE_CAMERA)
        if not camera.isOpened():
            camera.release()
            messagebox.showerror(
                "Câmera indisponível",
                "Não foi possível acessar a câmera. Verifique se ela está conectada, "
                "se as permissões estão habilitadas e se outro programa não está usando-a."
            )
            return

        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.camera = camera
        self.camera_frame = None
        self.camera_cantos = None

        # Ao entrar no modo câmera, descarta o resultado anterior
        self.imagem = None
        self.nome_arquivo = ""
        self._limpar_resultados()
        self._limpar_imagens(("original",), "Inicializando câmera…")

        self.status_ocr.set("Câmera iniciada. Posicione a folha no enquadramento.")
        self._definir_estado_botoes()
        self._atualizar_camera()

    def _atualizar_camera(self):
        self.camera_after_id = None
        if self.camera is None:
            return

        sucesso, frame = self.camera.read()
        if not sucesso or frame is None:
            self.parar_camera()
            messagebox.showerror("Falha na câmera",
                                 "A câmera foi aberta, mas não foi possível receber a imagem.")
            return

        self.camera_frame = frame.copy()
        try:
            cantos, encontrou = scan_detection(frame)
            previa = four_point_transform(frame, cantos) if encontrou else None
        except Exception as erro:
            self.parar_camera()
            messagebox.showerror("Erro no processamento da câmera",
                                 f"Não foi possível processar o quadro recebido:\n{erro}")
            return

        if encontrou and previa is not None:
            self.camera_cantos = cantos.copy()
            self.mostrar("original", desenhar_contorno(frame, cantos))
            self.mostrar("warped", previa)
            self.status_ocr.set("Documento detectado. Capture o quadro para digitalizar.")
        else:
            self.camera_cantos = None
            self.mostrar("original", frame)
            self._limpar_imagens(("warped",), "Documento não detectado")
            self.status_ocr.set("Aponte para uma folha com os quatro cantos visíveis.")

        # O intervalo é contado após o processamento, então quadros não se acumulam
        self.camera_after_id = self.root.after(40, self._atualizar_camera)

    def capturar_quadro(self):
        if self.camera is None or self.camera_frame is None:
            return

        frame = self.camera_frame.copy()
        cantos = self.camera_cantos.copy() if self.camera_cantos is not None else None
        self.parar_camera(atualizar_status=False)

        self.imagem = frame
        self.nome_arquivo = time.strftime("captura_%Y%m%d_%H%M%S.jpg")
        self._iniciar_processamento(cantos)

    def parar_camera(self, atualizar_status=True):
        if self.camera_after_id is not None:
            try:
                self.root.after_cancel(self.camera_after_id)
            except tk.TclError:
                pass
            self.camera_after_id = None

        camera = self.camera
        self.camera = None
        self.camera_frame = None
        self.camera_cantos = None
        if camera is not None:
            camera.release()

        if atualizar_status:
            self.status_ocr.set("Câmera encerrada. Escolha uma imagem ou inicie a câmera.")
        self._definir_estado_botoes()

    def fechar(self):
        self.parar_camera(atualizar_status=False)
        self.root.destroy()

    # ações
    def carregar(self):
        if self.processando:
            return
        if self.camera is not None:
            self.parar_camera(atualizar_status=False)

        caminho = filedialog.askopenfilename(
            filetypes=[("Imagens", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
                       ("Todos os arquivos", "*.*")]
        )
        if not caminho:
            return

        imagem = cv2.imread(caminho)
        if imagem is None:
            messagebox.showerror("Erro", "Não foi possível abrir essa imagem.")
            return

        self.imagem = imagem
        self.nome_arquivo = os.path.basename(caminho)
        self._limpar_resultados()
        self.mostrar("original", imagem)
        self.status_ocr.set("Imagem carregada. Clique em Processar.")
        self._definir_estado_botoes()

    def processar(self):
        if self.imagem is None or self.processando:
            return
        self._iniciar_processamento(None)

    def _limpar_resultados(self):
        self.cantos = None
        self.documento_encontrado = False
        self.warped = None
        self.scan_pb = None
        self.texto_ocr = ""
        self.resumo_ocr.set("Processe a imagem para ver a avaliação.")
        self.mostrar_texto("")
        self._limpar_imagens(self.CARTOES_PROCESSO, "Aguardando")

    def _iniciar_processamento(self, cantos_manuais=None):
        """Roda todas as etapas em segundo plano: pré-processamento, detecção,
        digitalização, efeito de scanner e OCR. Os resultados chegam pela fila."""
        if self.imagem is None or self.processando:
            return

        self._limpar_resultados()
        self.mostrar("original", self.imagem)
        self.processando = True
        self.status_ocr.set("Pré-processando a imagem…")
        self._iniciar_progresso()
        self._definir_estado_botoes()

        imagem = self.imagem.copy()
        manual = cantos_manuais is not None
        cantos_entrada = cantos_manuais.copy() if manual else None

        def trabalho():
            try:
                cinza, desfocada, binaria = preprocessamento(imagem)
                self.fila.put(("pre", (cinza, desfocada, binaria)))

                if manual:
                    cantos, encontrou = cantos_entrada, True
                else:
                    self.fila.put(("etapa", "Detectando o documento…"))
                    cantos, encontrou = scan_detection(imagem)
                contorno = desenhar_contorno(imagem, cantos) if encontrou else imagem
                self.fila.put(("deteccao", (cantos, encontrou, contorno)))

                warped = four_point_transform(imagem, cantos)
                if warped is None:
                    raise ValueError("Não foi possível aplicar a transformação de perspectiva.")
                self.fila.put(("digitalizada", (warped, efeito_scanner(warped))))

                resultado = extrair_texto(
                    warped,
                    progresso=lambda etapa, _img: self.fila.put(("etapa", etapa)),
                    retornar_detalhes=True,
                )
                self.fila.put(("resultado", resultado))
            except Exception as erro:
                self.fila.put(("erro", str(erro)))

        Thread(target=trabalho, daemon=True).start()
        self.root.after(80, self._acompanhar)

    def _acompanhar(self):
        # Só a thread principal mexe na interface
        try:
            while True:
                tipo, dados = self.fila.get_nowait()

                if tipo == "etapa":
                    self.status_ocr.set(dados)
                    continue

                if tipo == "pre":
                    for chave, imagem in zip(("gray", "blur", "threshold"), dados):
                        self.mostrar(chave, imagem)
                    continue

                if tipo == "deteccao":
                    cantos, encontrou, contorno = dados
                    self.cantos = cantos
                    self.documento_encontrado = encontrou
                    self.mostrar("contorno", contorno)
                    if not encontrou:
                        self.status_ocr.set("Documento não detectado: a imagem inteira foi "
                                            "usada. Use Ajustar cantos.")
                    continue

                if tipo == "digitalizada":
                    self.warped, self.scan_pb = dados
                    self.mostrar("warped", self.warped)
                    self.mostrar("pb", self.scan_pb)
                    continue

                # Daqui em diante o processamento terminou
                self.processando = False
                self._encerrar_progresso()

                if tipo == "erro":
                    self.status_ocr.set("Falha ao processar. Confira a mensagem.")
                    self.resumo_ocr.set("Avaliação indisponível.")
                    self._definir_estado_botoes()
                    messagebox.showerror("Erro", dados)
                    return

                self.texto_ocr = dados["texto"]
                self._mostrar_avaliacao(dados)
                self.mostrar_texto(self.texto_ocr)

                if not self.texto_ocr.strip():
                    self.status_ocr.set("Nenhum texto encontrado.")
                    messagebox.showinfo("OCR", "Nenhum texto foi encontrado.")
                elif self.documento_encontrado:
                    self.status_ocr.set("Pronto. Você pode corrigir o texto e salvar.")
                else:
                    self.status_ocr.set("Pronto, mas o documento não foi detectado com "
                                        "segurança. Confira com Ajustar cantos.")
                return
        except Empty:
            if self.processando:
                self.root.after(80, self._acompanhar)

    def _mostrar_avaliacao(self, resultado):
        confianca = f"{resultado['confianca_media']:.1f}".replace(".", ",")
        self.resumo_ocr.set(
            f"Letras e números extraídos: {resultado['caracteres_total']}\n\n"
            f"Em palavras com confiança ≥ {CONFIANCA_MINIMA}: "
            f"{resultado['caracteres_confiaveis']}\n\n"
            f"Confiança média das palavras: {confianca} / 100"
        )

    def ajustar_cantos(self):
        """Janela para arrastar os 4 cantos da folha. Sem detecção, usa a imagem inteira."""
        if self.imagem is None or self.processando or self.camera is not None:
            return

        janela = tk.Toplevel(self.root, bg=self.COR_CARTAO)
        janela.title("Ajustar cantos")
        janela.transient(self.root)
        tk.Label(janela, text="Arraste os círculos até os cantos da folha.",
                 bg=self.COR_CARTAO, fg=self.COR_SUAVE).pack(anchor="w", padx=16, pady=(12, 8))

        pil = Image.fromarray(self._para_rgb(self.imagem))
        escala = min(self.root.winfo_screenwidth() * 0.8 / pil.width,
                     self.root.winfo_screenheight() * 0.72 / pil.height, 1.0)
        pil = pil.resize((max(1, int(pil.width * escala)), max(1, int(pil.height * escala))),
                         Image.LANCZOS)
        foto = ImageTk.PhotoImage(pil)
        canvas = tk.Canvas(janela, width=pil.width, height=pil.height,
                           highlightthickness=0, bg="#111111", cursor="crosshair")
        canvas.pack(padx=16)
        canvas.create_image(0, 0, image=foto, anchor="nw")
        canvas.foto = foto

        if self.cantos is not None:
            base = self.cantos
        else:
            altura, largura = self.imagem.shape[:2]
            base = np.array([[0, 0], [largura - 1, 0],
                             [largura - 1, altura - 1], [0, altura - 1]], np.float32)

        pontos = [list(p) for p in order_points(base) * escala]
        raio = 9
        poligono = canvas.create_polygon(*[c for p in pontos for c in p],
                                         outline="#1FC6A0", fill="", width=2)
        alcas = [canvas.create_oval(0, 0, 0, 0, fill="white", outline="#1FC6A0", width=3)
                 for _ in pontos]
        arrastando = {"indice": None}

        def redesenhar():
            canvas.coords(poligono, *[c for p in pontos for c in p])
            for alca, (x, y) in zip(alcas, pontos):
                canvas.coords(alca, x - raio, y - raio, x + raio, y + raio)

        def pressionar(evento):
            distancias = [(evento.x - x) ** 2 + (evento.y - y) ** 2 for x, y in pontos]
            indice = int(np.argmin(distancias))
            arrastando["indice"] = indice if distancias[indice] < (raio * 4) ** 2 else None

        def mover(evento):
            indice = arrastando["indice"]
            if indice is not None:
                pontos[indice] = [min(max(evento.x, 0), pil.width - 1),
                                  min(max(evento.y, 0), pil.height - 1)]
                redesenhar()

        canvas.bind("<ButtonPress-1>", pressionar)
        canvas.bind("<B1-Motion>", mover)
        canvas.bind("<ButtonRelease-1>", lambda _e: arrastando.update(indice=None))
        redesenhar()

        def imagem_inteira():
            pontos[:] = [[0, 0], [pil.width - 1, 0], [pil.width - 1, pil.height - 1],
                         [0, pil.height - 1]]
            redesenhar()

        def aplicar():
            novos = np.array(pontos, np.float32)
            if not cv2.isContourConvex(order_points(novos).reshape(-1, 1, 2).astype(np.int32)):
                messagebox.showwarning("Cantos inválidos", "Os cantos se cruzaram. "
                                       "Arraste-os para formar um quadrilátero.", parent=janela)
                return
            janela.destroy()
            self._iniciar_processamento(novos / escala)

        botoes = tk.Frame(janela, bg=self.COR_CARTAO)
        botoes.pack(fill="x", padx=16, pady=12)
        self._botao(botoes, "Aplicar", aplicar, self.COR_PRIMARIA, self.COR_TEXTO).pack(
            side="right")
        self._botao(botoes, "Cancelar", janela.destroy, self.COR_DESATIVADA,
                    self.COR_TEXTO).pack(side="right", padx=8)
        self._botao(botoes, "Imagem inteira", imagem_inteira, self.COR_DESATIVADA,
                    self.COR_TEXTO).pack(side="left")
        janela.bind("<Escape>", lambda _e: janela.destroy())
        janela.bind("<Return>", lambda _e: aplicar())
        janela.grab_set()

    def mostrar_texto(self, texto):
        self.caixa_texto.delete("1.0", "end")
        self.caixa_texto.insert("1.0", texto)
        self._definir_estado_botoes()

    def _texto_atual(self):
        # Lê o conteúdo da caixa, já com as correções feitas pelo usuário
        return self.caixa_texto.get("1.0", "end-1c")

    def salvar_texto(self):
        texto = self._texto_atual()
        if not texto.strip():
            messagebox.showwarning("Aviso", "Não há texto para salvar.")
            return

        caminho = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Arquivo de texto", "*.txt")],
            initialfile="texto_extraido.txt",
        )
        if not caminho:
            return

        try:
            with open(caminho, "w", encoding="utf-8") as arquivo:
                arquivo.write(texto)
        except OSError as erro:
            messagebox.showerror("Erro", f"Não foi possível salvar o texto:\n{erro}")
            return

        messagebox.showinfo("Pronto", f"Texto salvo em:\n{caminho}")

    def salvar(self, versao="cor"):
        imagem = self.warped if versao == "cor" else self.scan_pb
        if imagem is None:
            messagebox.showwarning("Aviso", "Processe a imagem antes de salvar.")
            return

        caminho = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG", "*.png")],
            initialfile=f"scan_{'colorido' if versao == 'cor' else 'pb'}.png",
        )
        if not caminho:
            return

        Image.fromarray(self._para_rgb(imagem)).save(caminho, "PNG")
        messagebox.showinfo("Pronto", f"Salvo em:\n{caminho}")

    # ----------------------------------------------------------------- imagens
    @staticmethod
    def _para_rgb(array):
        if array.ndim == 3:
            return cv2.cvtColor(array, cv2.COLOR_BGR2RGB)
        return array

    def mostrar(self, chave, array):
        # Guarda a imagem em tamanho real, a miniatura é gerada conforme o espaço
        self.originais[chave] = Image.fromarray(self._para_rgb(array))
        self._redimensionar(chave)

    def _limpar_imagens(self, chaves, texto):
        for chave in chaves:
            self.originais.pop(chave, None)
            self.fotos.pop(chave, None)
            self.labels[chave].config(image="", text=texto)

    def _agendar_redimensionamento(self, chave):
        # Junta vários eventos de redimensionamento em um só para a janela não travar
        if chave not in self.originais:
            return
        if chave in self.agendados:
            self.root.after_cancel(self.agendados[chave])
        self.agendados[chave] = self.root.after(60, lambda: self._redimensionar(chave))

    def _redimensionar(self, chave):
        self.agendados.pop(chave, None)
        original = self.originais.get(chave)
        if original is None:
            return

        area = self.areas[chave]
        largura = area.winfo_width() - 8
        altura = area.winfo_height() - 8
        # Cartão escondido ainda não tem tamanho real: espera o próximo Configure
        if largura < 20 or altura < 20:
            return

        pil = original.copy()
        pil.thumbnail((largura, altura))
        self.fotos[chave] = ImageTk.PhotoImage(pil)
        self.labels[chave].config(image=self.fotos[chave], text="")


if __name__ == "__main__":
    root = tk.Tk()
    app = ImagePreprocessorApp(root)
    root.mainloop()