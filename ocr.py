import cv2
import numpy as np
import pytesseract


def preparar_para_ocr(imagem, progresso=None):
    """Prepara somente uma imagem em escala de cinza, ajustando a iluminação.

    progresso é opcional e recebe (mensagem, imagem) para mostrar as etapas.
    A detecção da folha e a perspectiva continuam no main.
    """
    avisar = progresso or (lambda mensagem, imagem: None)
    if not isinstance(imagem, np.ndarray) or imagem.size == 0:
        raise ValueError("A imagem está vazia ou não é um array NumPy.")
    if imagem.dtype != np.uint8 or imagem.ndim not in (2, 3):
        raise ValueError("Use uma imagem uint8 em cinza, BGR ou BGRA.")
    if min(imagem.shape[:2]) < 3:
        raise ValueError("A imagem é pequena demais para reconhecer texto.")

    # Cinza simplifica a leitura. A imagem recebida nunca é alterada.
    if imagem.ndim == 3:
        if imagem.shape[2] not in (3, 4):
            raise ValueError("A imagem colorida deve ter 3 ou 4 canais.")
        if imagem.shape[2] == 4:
            alpha = imagem[:, :, 3:4].astype(np.float32) / 255
            imagem = (imagem[:, :, :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
        cinza = cv2.cvtColor(imagem, cv2.COLOR_BGR2GRAY)
    else:
        cinza = imagem.copy()

    # Ampliação moderada para letras pequenas: até 3x, em direção a 1800 pixels.
    escala = min(3.0, 1800.0 / max(cinza.shape))
    if escala > 1:
        cinza = cv2.resize(cinza, None, fx=escala, fy=escala,
                           interpolation=cv2.INTER_CUBIC)
    avisar("Melhorando imagem para OCR…", cinza)

    # Estima variações de iluminação no interior da folha.
    if cinza.max() == cinza.min():
        branca = np.full_like(cinza, 255)
        return branca
    fundo = cv2.GaussianBlur(cinza, (0, 0), 25)
    h, w = fundo.shape
    interior = fundo[h // 10:h - h // 10, w // 10:w - w // 10]
    variacao = np.percentile(interior, 90) - np.percentile(interior, 10)
    if variacao > 12:
        cinza = cv2.divide(cinza, np.maximum(fundo, 1), scale=255)
        cinza = cv2.normalize(cinza, None, 0, 255, cv2.NORM_MINMAX)
        avisar("Melhorando imagem para OCR…", cinza)
    else:
        avisar("Melhorando imagem para OCR…", cinza)

    # Mantém os tons de cinza: nenhuma versão binarizada é criada para o OCR.
    return cv2.copyMakeBorder(
        cinza, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255
    )


def extrair_texto(imagem, idioma="por", progresso=None, *, retornar_detalhes=False):
    """Lê sempre a mesma imagem em cinza, com até dois modos de leitura.

    Retorna texto por padrão; retornar_detalhes=True inclui a avaliação.
    Confiança é uma estimativa de qualidade,
    não uma porcentagem de acerto nem uma identificação do tipo de documento.
    """
    if not isinstance(idioma, str) or not idioma.strip():
        raise ValueError("Informe um idioma, como 'por' ou 'eng'.")
    avisar = progresso or (lambda mensagem, imagem: None)
    cinza = preparar_para_ocr(imagem, progresso)
    if cinza.max() == cinza.min():
        avisar("OCR concluído.", cinza)
        if retornar_detalhes:
            return {"texto": "", "confianca_media": 0.0,
                    "caracteres_total": 0, "caracteres_confiaveis": 0}
        return ""

    tentativas = []
    ultima_falha = None
    for numero, psm in enumerate((3, 6), 1):
        versao = cinza  # As duas tentativas recebem a mesma imagem em cinza.
        etapa = "Extraindo texto…"
        avisar(etapa, versao)
        try:
            dados = pytesseract.image_to_data(
                versao, lang=idioma.strip(), config=f"--psm {psm}",
                output_type=pytesseract.Output.DICT, timeout=20,
            )
        except pytesseract.TesseractError:
            raise
        except RuntimeError as erro:
            ultima_falha = erro
            continue
        texto, confianca, cobertura = _avaliar_resultado(dados)
        tentativas.append((texto, confianca, cobertura, versao, numero))
        # Uma leitura aparentemente boa evita a segunda execução do Tesseract.
        if numero == 1 and confianca >= 80 and cobertura >= 30:
            break

    if not tentativas:
        raise RuntimeError("O OCR excedeu o tempo limite nas duas tentativas.") from ultima_falha
    maior_cobertura = max(t[2] for t in tentativas) or 1
    melhor = max(tentativas, key=lambda t: t[1] * (t[2] / maior_cobertura) ** 0.25)
    avisar("OCR concluído.", melhor[3])
    if retornar_detalhes:
        # Conta apenas letras e números, como já faz a avaliação da cobertura.
        return {
            "texto": melhor[0],
            "confianca_media": melhor[1],
            "caracteres_total": sum(c.isalnum() for c in melhor[0]),
            "caracteres_confiaveis": melhor[2],
        }
    return melhor[0]


def _avaliar_resultado(dados):
    """Monta o texto e calcula confiança e quantidade de caracteres confiáveis."""
    linhas = {}
    confiancas = []
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
            if confianca >= 40:
                cobertura += sum(c.isalnum() for c in palavra)

    texto = "\n".join(" ".join(palavras) for palavras in linhas.values())
    media = float(np.mean(confiancas)) if confiancas else 0.0
    return texto, media, cobertura
