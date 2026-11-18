import asyncio
import base64
import html
import io
import json
import os
import re
import threading
import unicodedata
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

os.environ.setdefault("OMP_THREAD_LIMIT", "1")

from PIL import Image, ImageFilter, ImageOps
from playwright.async_api import async_playwright
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import ClickTracking, Mail, TrackingSettings

try:
    import tesserocr
except Exception:
    tesserocr = None

try:
    import pytesseract
except Exception:
    pytesseract = None


EMAIL_DESTINO = ["gabriellaredo@bemol.com.br"]
EMAIL_REMETENTE = "gabriellaredo@bemol.com.br"
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")

AVISO_DIAS = 2
REPORT_COMPLETO = True
LIMITE_DIAS_CARROSSEL_2 = 15
HISTORICO_ARQUIVO = "historico_banners.json"

SITES = [
    {"nome": "Bemol", "url": "https://www.bemol.com.br"},
    {"nome": "Bemol Farma", "url": "https://www.bemolfarma.com.br"},
]

URLS_IGNORADAS = [
    "app.contabemol.com.br",
    "emprestimocgi.bemol.com.br",
    "bit.ly",
    "bemolfarma.com.br/super/super%20cimed",
    "bemolfarma.com.br/super/super cimed",
    "bemolfarma.com.br/super/super_cimed",
]

SEMAFORO = asyncio.Semaphore(3)
OCR_SIMULTANEOS = asyncio.Semaphore(max(1, os.cpu_count() or 1))
OCR_TIMEOUT = 20
LARGURA_LEITURA = 2800


def _hoje():
    simulada = os.environ.get("DATA_SIMULADA", "").strip()
    if simulada:
        try:
            return date.fromisoformat(simulada)
        except ValueError:
            pass
    return datetime.now(ZoneInfo("America/Manaus")).date()


async def coletar_links_banners(page, url):
    print(f"  -> Acessando {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)

    for pct in (25, 50, 75, 100):
        await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pct} / 100)")
        await page.wait_for_timeout(1000)
    await page.evaluate("window.scrollTo(0, 0)")

    setas = await page.query_selector_all('[class*="sliderRightArrow"]')
    print(f"  -> {len(setas)} carrosseis encontrados")
    for seta in setas:
        for _ in range(30):
            try:
                await seta.click()
                await page.wait_for_timeout(200)
            except Exception:
                break

    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(2000)
    setas = await page.query_selector_all('[class*="sliderRightArrow"]')
    for seta in setas[-2:]:
        for _ in range(20):
            try:
                await seta.click()
                await page.wait_for_timeout(200)
            except Exception:
                break

    itens = await page.evaluate("""
        () => {
            const pegarImg = (a) => {
                for (const img of a.querySelectorAll('img')) {
                    const src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
                    if (src && !src.startsWith('data:')) return src.trim();
                }
                return null;
            };
            const bloqueados = ['facebook','instagram','twitter','x.com','youtube',
                'linkedin','apple.com','google.com','siteblindado','consumidor.gov',
                'ebit','compreconfie'];
            const resultado = [];
            let indice = 1;
            document.querySelectorAll('[class*="vtex-slider-layout-0-x-sliderTrackContainer"]')
                .forEach((secao) => {
                    const links = secao.querySelectorAll('[class*="imageElementLink"]');
                    if (!links.length) return;
                    links.forEach((a) => {
                        const href = (a.getAttribute('href') || '').trim();
                        if (href && href !== '/' && href !== '#') {
                            resultado.push({href, section: `Carrossel ${indice}`, img: pegarImg(a)});
                        }
                    });
                    indice++;
                });
            document.querySelectorAll('[class*="imageElementLink"]').forEach((a) => {
                if (a.closest('[class*="vtex-slider-layout-0-x-sliderTrackContainer"]')) return;
                const href = (a.getAttribute('href') || '').trim();
                if (!href || href === '/' || href === '#') return;
                if (bloqueados.some((valor) => href.includes(valor))) return;
                resultado.push({href, section: 'Categoria', img: pegarImg(a)});
            });
            const vistos = new Set();
            return resultado.filter((item) => {
                const chave = `${item.href}|${item.img || ''}`;
                if (vistos.has(chave)) return false;
                vistos.add(chave);
                return true;
            });
        }
    """)

    if not itens:
        print(f"  ATENCAO: nenhum banner encontrado em {url}. Verifique os seletores CSS.")
    return itens


MESES = {
    "jan": 1, "janeiro": 1, "fev": 2, "fevereiro": 2, "mar": 3, "marco": 3,
    "abr": 4, "abril": 4, "mai": 5, "maio": 5, "jun": 6, "junho": 6,
    "jul": 7, "julho": 7, "ago": 8, "agosto": 8, "set": 9, "setembro": 9,
    "out": 10, "outubro": 10, "nov": 11, "novembro": 11, "dez": 12, "dezembro": 12,
}
MESES_RE = "|".join(sorted(MESES, key=len, reverse=True))
RE_NUMERICA = re.compile(r"(?<![\d,.])(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?(?![\d,.]|\s*x)")
RE_EXTENSO = re.compile(r"(?<!\d)(\d{1,2})\s*(?:de\s+|/\s*)?(" + MESES_RE + r")\b(?:\s*(?:de\s+)?(\d{4}))?")
RE_INTERVALO = re.compile(r"\b(?:de\s+)?(\d{1,2})\s*(?:a|ate|-|–)\s*(\d{1,2})\s*(?:de\s+)?(" + MESES_RE + r")\b")
PALAVRAS_RELATIVAS = [
    "so hoje", "somente hoje", "apenas hoje", "valido hoje", "esta semana",
    "essa semana", "ultima semana", "ultimos dias", "ultimo dia", "termina hoje",
    "acaba hoje", "termina amanha", "acaba amanha", "termina domingo", "ate domingo",
    "ate sexta", "ate sabado", "ate segunda", "ate terca", "ate quarta", "ate quinta",
    "fim de semana", "final de semana", "24 horas", "24h",
]


def _normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    texto = re.sub(r"(\d\s?/)\s?[\[\]{}|!li](?=\d)", r"\g<1>1", texto)
    texto = re.sub(r"(?<=/\d)[\[\]{}|](?=\D|$)", "1", texto)
    texto = re.sub(r"(?<=\d)[\[\]{}|](?=\d|/)", "1", texto)
    texto = re.sub(r"(?<=[\d/])o(?=[\d/])", "0", texto)
    texto = re.sub(r"(?<=[\d/])[li|](?=[\d/]|\b)", "1", texto)
    texto = re.sub(r"[^\w/\-–:,.\s]", " ", texto)
    return re.sub(r"\s+", " ", texto)


def _ano_para(dia, mes, ano_txt, hoje):
    if ano_txt:
        ano = int(ano_txt)
        return ano + 2000 if ano < 100 else ano
    candidatos = []
    for ano in (hoje.year - 1, hoje.year, hoje.year + 1):
        try:
            candidatos.append(date(ano, mes, dia))
        except ValueError:
            pass
    return min(candidatos, key=lambda valor: abs((valor - hoje).days)).year if candidatos else None


def extrair_data(texto_bruto):
    hoje = _hoje()
    texto = _normalizar(texto_bruto)
    datas = []

    for match in RE_INTERVALO.finditer(texto):
        dia, mes = int(match.group(2)), MESES[match.group(3)]
        ano = _ano_para(dia, mes, None, hoje)
        try:
            datas.append((date(ano, mes, dia), match.group(0)))
        except (TypeError, ValueError):
            pass

    for match in RE_EXTENSO.finditer(texto):
        dia, mes = int(match.group(1)), MESES[match.group(2)]
        antes = texto[max(0, match.start() - 20):match.start()]
        if any(p in antes for p in ("a partir", "comeca", "inicia", "inicio")):
            continue
        ano = _ano_para(dia, mes, match.group(3), hoje)
        try:
            datas.append((date(ano, mes, dia), match.group(0)))
        except (TypeError, ValueError):
            pass

    for match in RE_NUMERICA.finditer(texto):
        dia, mes = int(match.group(1)), int(match.group(2))
        if not (1 <= dia <= 31 and 1 <= mes <= 12):
            continue
        antes = texto[max(0, match.start() - 30):match.start()]
        pista = re.search(r"\b(ate|valid\w*|vigen\w*|promoc\w*|oferta\w*|periodo|termin\w*|encerr\w*|acaba\w*)\b", antes)
        intervalo = re.search(r"\d{1,2}(?:\s*/\s*\d{1,2})?\s*(?:a|ate|-|–|e)\s*$", antes)
        if not (match.group(3) or pista or intervalo):
            continue
        ano = _ano_para(dia, mes, match.group(3), hoje)
        try:
            datas.append((date(ano, mes, dia), match.group(0)))
        except (TypeError, ValueError):
            pass

    datas = [item for item in datas if hoje - timedelta(days=365) <= item[0] <= hoje + timedelta(days=730)]
    if datas:
        fim, trecho = max(datas, key=lambda item: item[0])
        return {"tem_data": True, "data_fim": fim.isoformat(), "texto_encontrado": trecho.strip(), "confianca": "alta"}
    for palavra in PALAVRAS_RELATIVAS:
        if palavra in texto:
            return {"tem_data": True, "data_fim": None, "texto_encontrado": palavra, "confianca": "baixa"}
    return {"tem_data": False, "data_fim": None, "texto_encontrado": None, "confianca": "alta"}


TESSDATA_DIR = os.path.expanduser(os.environ.get("TESSDATA_DIR", "~/tessdata"))
TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/por.traineddata"
_tessdata_lock = threading.Lock()
_tess_local = threading.local()


def _garantir_tessdata():
    arquivo = os.path.join(TESSDATA_DIR, "por.traineddata")
    with _tessdata_lock:
        if os.path.exists(arquivo) and os.path.getsize(arquivo) > 1_000_000:
            return TESSDATA_DIR
        os.makedirs(TESSDATA_DIR, exist_ok=True)
        temporario = arquivo + ".tmp"
        with urllib.request.urlopen(TESSDATA_URL, timeout=60) as resposta, open(temporario, "wb") as destino:
            destino.write(resposta.read())
        os.replace(temporario, arquivo)
    return TESSDATA_DIR


def _ocr(imagem, psm):
    if tesserocr is not None:
        apis = getattr(_tess_local, "apis", None)
        if apis is None:
            apis = _tess_local.apis = {}
        if psm not in apis:
            apis[psm] = tesserocr.PyTessBaseAPI(path=_garantir_tessdata(), lang="por", psm=psm)
        apis[psm].SetImage(imagem)
        return apis[psm].GetUTF8Text()
    if pytesseract is not None:
        return pytesseract.image_to_string(imagem, lang="por", config=f"--psm {psm}", timeout=OCR_TIMEOUT)
    raise RuntimeError("leitor de texto nao instalado")


def _abrir_cinza(conteudo, largura=LARGURA_LEITURA):
    imagem = Image.open(io.BytesIO(conteudo))
    imagem.seek(0)
    if imagem.mode in ("RGBA", "LA", "P", "PA"):
        rgba = imagem.convert("RGBA")
        fundo = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        imagem = Image.alpha_composite(fundo, rgba)
    imagem = imagem.convert("RGB")
    if imagem.width < largura * 0.85:
        fator = largura / imagem.width
        imagem = imagem.resize((int(imagem.width * fator), int(imagem.height * fator)), Image.LANCZOS)
    return ImageOps.grayscale(imagem)


def ler_textos_ocr(conteudo, lupa=False):
    cinza = _abrir_cinza(conteudo, int(LARGURA_LEITURA * (1.5 if lupa else 1)))
    nitida = cinza.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    leituras = [(cinza, 11), (cinza, 6), (nitida, 11), (nitida, 6), (ImageOps.invert(cinza), 11)]
    if not lupa:
        passo = cinza.height / 4
        for indice in range(4):
            y0 = max(0, int(indice * passo - passo / 3))
            y1 = min(cinza.height, int((indice + 1) * passo + passo / 3))
            faixa = cinza.crop((0, y0, cinza.width, y1))
            leituras.extend(((faixa, 6), (ImageOps.invert(faixa), 6)))
    textos = []
    ultimo_erro = None
    for imagem, psm in leituras:
        try:
            textos.append(_ocr(imagem, psm))
        except Exception as erro:
            ultimo_erro = erro
    if not textos:
        raise RuntimeError(f"OCR nao rodou: {ultimo_erro or 'erro desconhecido'}")
    return textos


def decidir_data(textos):
    resultados = [extrair_data(texto) for texto in textos]
    votos = Counter(resultado["data_fim"] for resultado in resultados if resultado.get("data_fim"))
    votos = Counter({valor: quantidade for valor, quantidade in votos.items() if quantidade >= 2})
    if votos:
        maior = max(votos.values())
        escolhida = max(valor for valor, quantidade in votos.items() if quantidade == maior)
        resultado = next(item for item in resultados if item.get("data_fim") == escolhida)
        return dict(resultado, votos=f"{votos[escolhida]}/{len(textos)}")
    relativos = [item for item in resultados if item.get("tem_data")]
    return relativos[0] if relativos else {"tem_data": False, "data_fim": None, "texto_encontrado": None, "confianca": "alta"}


def _imagem_valida(conteudo):
    try:
        Image.open(io.BytesIO(conteudo)).load()
        return True
    except Exception:
        return False


async def _converter_imagem(context, conteudo, tipo):
    tipo = tipo or "image/png"
    uri = f"data:{tipo};base64,{base64.b64encode(conteudo).decode()}"
    pagina = await context.new_page()
    try:
        await pagina.set_content(f'<html><body style="margin:0;background:#fff"><img id="b" src="{uri}" style="display:block;min-width:1400px;height:auto"></body></html>')
        await pagina.wait_for_function("document.getElementById('b').complete", timeout=10000)
        return await pagina.locator("#b").screenshot(type="png", timeout=10000)
    finally:
        await pagina.close()


async def ler_data_banner(request, item, context):
    src = item.get("img")
    if not src:
        return "sem_imagem", None
    try:
        async with SEMAFORO:
            resposta = await request.get(src, timeout=30000)
            if not resposta.ok:
                item["motivo_falha"] = f"imagem nao baixou (HTTP {resposta.status})"
                return "falhou", None
            conteudo = await resposta.body()
            tipo = resposta.headers.get("content-type", "").split(";")[0].strip()
        if not _imagem_valida(conteudo):
            async with SEMAFORO:
                conteudo = await _converter_imagem(context, conteudo, tipo)
        async with OCR_SIMULTANEOS:
            textos = await asyncio.to_thread(ler_textos_ocr, conteudo)
        leitura = decidir_data(textos)
        if leitura.get("data_fim") and date.fromisoformat(leitura["data_fim"]) < _hoje():
            async with OCR_SIMULTANEOS:
                textos += await asyncio.to_thread(ler_textos_ocr, conteudo, True)
            leitura = decidir_data(textos)
        return "ok", leitura
    except Exception as erro:
        item["motivo_falha"] = str(erro)[:100]
        print(f"    Leitor de data falhou em {src[:60]}: {erro}")
        return "falhou", None


def classificar_data(leitura):
    if not leitura or not leitura.get("tem_data"):
        return None, ""
    texto = leitura.get("texto_encontrado") or ""
    if leitura.get("data_fim"):
        fim = date.fromisoformat(leitura["data_fim"])
        if fim < _hoje():
            return "vencido", f"Venceu em {fim.strftime('%d/%m/%Y')} ({(_hoje() - fim).days} dia(s) atras)"
        if fim <= _hoje() + timedelta(days=AVISO_DIAS):
            return "vencendo", f"Vence em {fim.strftime('%d/%m/%Y')}"
        return None, ""
    return "revisar", f"Prazo relativo, conferir manualmente: {texto}"


async def verificar_url(browser, href, base_url):
    url = href if href.startswith("http") else urljoin(base_url.rstrip("/") + "/", href.lstrip("/"))
    decodificada = unquote(url)
    if any(item in url or item in decodificada for item in URLS_IGNORADAS):
        return {"url": url, "status": "OK*", "title": "Link ignorado (verificacao manual)", "ok": True}

    async with SEMAFORO:
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        pagina = await context.new_page()
        try:
            ultimo_erro = "Erro desconhecido"
            for tentativa in range(2):
                try:
                    resposta = await pagina.goto(url, wait_until="domcontentloaded", timeout=20000)
                    await pagina.wait_for_timeout(2000)
                    status = resposta.status if resposta else 0
                    titulo = await pagina.title()
                    erro = status >= 400 or "404" in titulo or "nao encontrad" in titulo.lower() or "page not found" in titulo.lower()
                    if erro and tentativa == 0:
                        await pagina.wait_for_timeout(3000)
                        continue
                    return {"url": url, "status": status, "title": titulo[:70], "ok": not erro}
                except Exception as exc:
                    ultimo_erro = str(exc)[:70]
                    if tentativa == 0:
                        await pagina.wait_for_timeout(3000)
            return {"url": url, "status": "ERR", "title": ultimo_erro, "ok": False}
        finally:
            await context.close()


async def verificar_site(browser, site):
    print(f"\nVerificando: {site['nome']} ({site['url']})")
    context = await browser.new_context(viewport={"width": 1440, "height": 900})
    pagina = await context.new_page()
    itens = await coletar_links_banners(pagina, site["url"])
    leituras = await asyncio.gather(*(ler_data_banner(pagina.request, item, context) for item in itens))
    for item, (situacao, leitura) in zip(itens, leituras):
        item["leitura_situacao"] = situacao
        item["leitura"] = leitura
    await context.close()

    async def verificar(item):
        resultado = await verificar_url(browser, item["href"], site["url"])
        leitura = item.get("leitura") or {}
        resultado.update({
            "section": item["section"], "img": item.get("img"),
            "leitura_situacao": item.get("leitura_situacao", "falhou"),
            "motivo_falha": item.get("motivo_falha", ""),
            "data_trecho": leitura.get("texto_encontrado") or "",
            "data_fim": leitura.get("data_fim"),
        })
        resultado["data_status"], resultado["data_desc"] = classificar_data(leitura)
        return resultado

    return list(await asyncio.gather(*(verificar(item) for item in itens)))


def _chave_historico(site, resultado):
    imagem = resultado.get("img") or ""
    if imagem:
        partes = urlsplit(imagem)
        imagem = partes._replace(query="", fragment="").geturl()
    return json.dumps([site, resultado.get("section", ""), imagem or resultado.get("url", "")], ensure_ascii=False)


def atualizar_historico(todos_resultados):
    try:
        with open(HISTORICO_ARQUIVO, encoding="utf-8") as arquivo:
            anterior = json.load(arquivo)
        if not isinstance(anterior, dict):
            anterior = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        anterior = {}

    hoje = _hoje()
    atual = {}
    for site, resultados in todos_resultados:
        for resultado in resultados:
            chave = _chave_historico(site, resultado)
            try:
                primeira = date.fromisoformat(anterior.get(chave, {}).get("primeira_vez", ""))
                if primeira > hoje:
                    primeira = hoje
            except ValueError:
                primeira = hoje
            dias = max(0, (hoje - primeira).days)
            resultado["dias_no_ar"] = dias
            resultado["tempo_status"] = None
            resultado["tempo_desc"] = ""
            data_ativa = False
            try:
                data_ativa = bool(resultado.get("data_fim") and date.fromisoformat(resultado["data_fim"]) >= hoje)
            except ValueError:
                pass
            if resultado.get("section") == "Carrossel 2" and dias > LIMITE_DIAS_CARROSSEL_2 and not data_ativa:
                resultado["tempo_status"] = "antigo"
                resultado["tempo_desc"] = f"No ar há {dias} dias sem data ativa"
            atual[chave] = {"primeira_vez": primeira.isoformat(), "ultima_vez": hoje.isoformat(), "dias_no_ar": dias}

    temporario = HISTORICO_ARQUIVO + ".tmp"
    with open(temporario, "w", encoding="utf-8") as arquivo:
        json.dump(atual, arquivo, ensure_ascii=False, indent=2)
    os.replace(temporario, HISTORICO_ARQUIVO)


def gerar_html_report(todos_resultados, data_hora):
    todos = [(site, item) for site, resultados in todos_resultados for item in resultados]
    total = len(todos)
    vencidos = sum(item.get("data_status") == "vencido" for _, item in todos)
    vencendo = sum(item.get("data_status") == "vencendo" for _, item in todos)
    antigos = sum(item.get("tempo_status") == "antigo" for _, item in todos)
    revisar = sum(item.get("data_status") == "revisar" for _, item in todos)

    def e(valor):
        return html.escape(str(valor or ""), quote=True)

    def classificacao(item):
        if not item["ok"] or item.get("data_status") == "vencido" or item.get("tempo_status") == "antigo":
            return "problema"
        if item.get("data_status") in ("vencendo", "revisar") or item.get("leitura_situacao") != "ok":
            return "conferir"
        return "ok"

    total_ok = sum(classificacao(item) == "ok" for _, item in todos)
    total_problemas = sum(classificacao(item) == "problema" for _, item in todos)
    total_conferir = sum(classificacao(item) == "conferir" for _, item in todos)
    links_com_erro = sum(not item["ok"] for _, item in todos)

    if total_problemas:
        status_fundo, status_borda, status_cor, status_icone = "#FFF1F2", "#FECDD3", "#BE123C", "⚠️"
        partes = []
        if links_com_erro:
            partes.append(f"{links_com_erro} link(s) com problema")
        if vencidos:
            partes.append(f"{vencidos} banner(s) vencido(s)")
        if antigos:
            partes.append(f"{antigos} banner(s) antigo(s) sem data ativa")
        status_texto = " · ".join(partes)
    elif total_conferir:
        status_fundo, status_borda, status_cor, status_icone = "#FFFBEB", "#FDE68A", "#A16207", "🔎"
        status_texto = f"{total_conferir} banner(s) precisam de conferência"
    else:
        status_fundo, status_borda, status_cor, status_icone = "#F0FDF4", "#BBF7D0", "#15803D", "✅"
        status_texto = "Tudo certo: nenhum link com problema e nenhum banner vencido"

    def card(valor, rotulo, cor):
        return f"""
        <td class="summary-cell" style="width:25%;padding:0 5px">
          <div style="background:#FFFFFF;border:1px solid #DCE3EC;border-radius:11px;padding:19px 10px;text-align:center;box-shadow:0 1px 3px rgba(71,85,105,.06)">
            <div style="font-size:30px;line-height:34px;font-weight:700;color:{cor}">{valor}</div>
            <div style="margin-top:4px;font-size:11px;line-height:16px;font-weight:700;letter-spacing:.45px;text-transform:uppercase;color:#64748B">{rotulo}</div>
          </div>
        </td>"""

    def data_info(item):
        if item.get("tempo_status") == "antigo":
            return item["tempo_desc"], "#BE123C"
        if item.get("data_status") == "vencido":
            return item.get("data_desc") or "Data vencida", "#BE123C"
        if item.get("data_status") in ("vencendo", "revisar"):
            return item.get("data_desc") or "Conferir data", "#A16207"
        if item.get("data_fim"):
            valor = date.fromisoformat(item["data_fim"]).strftime("%d/%m/%Y")
            return f"Válido até {valor}", "#15803D"
        if item.get("leitura_situacao") != "ok":
            return item.get("motivo_falha") or "Data não verificada", "#64748B"
        return "Sem data", "#64748B"

    secoes = []
    for site, resultados in todos_resultados:
        linhas = []
        for item in resultados:
            classe = classificacao(item)
            if classe == "problema":
                apto, apto_cor, apto_fundo, apto_icone = "NÃO APTO", "#BE123C", "#FFF1F2", "✕"
            elif classe == "conferir":
                apto, apto_cor, apto_fundo, apto_icone = "CONFERIR", "#A16207", "#FFFBEB", "!"
            else:
                apto, apto_cor, apto_fundo, apto_icone = "APTO", "#15803D", "#F0FDF4", "✓"

            descricao_data, cor_data = data_info(item)
            if item.get("img"):
                miniatura = f'<img src="{e(item["img"])}" alt="Banner" style="display:block;width:160px;max-width:100%;height:76px;object-fit:contain;border-radius:7px;background:#F8FAFC">'
            else:
                miniatura = '<div style="width:160px;max-width:100%;height:54px;border-radius:7px;background:#F1F5F9;color:#94A3B8;text-align:center;line-height:54px;font-size:11px">Sem imagem</div>'

            secao_cor = "#15803D" if item.get("section") != "Categoria" else "#2563EB"
            secao_fundo = "#F0FDF4" if item.get("section") != "Categoria" else "#EFF6FF"
            link_cor = "#15803D" if item["ok"] else "#BE123C"
            link_icone = "✓" if item["ok"] else "✕"
            texto_lido = e(item.get("data_trecho")) or '<span style="color:#94A3B8">—</span>'

            linhas.append(f"""
              <tr>
                <td class="banner-cell" style="padding:12px 13px;border-bottom:1px solid #EEF2F7;vertical-align:top">
                  {miniatura}
                  <span style="display:inline-block;margin-top:7px;padding:3px 9px;border-radius:999px;background:{secao_fundo};color:{secao_cor};font-size:10px;font-weight:700">{e(item['section'])}</span>
                </td>
                <td style="padding:14px 13px;border-bottom:1px solid #EEF2F7;vertical-align:top;color:#475569;font-size:12px;line-height:18px">{texto_lido}</td>
                <td style="padding:14px 13px;border-bottom:1px solid #EEF2F7;vertical-align:top;color:{cor_data};font-size:12px;line-height:18px;font-weight:{'700' if cor_data != '#64748B' else '500'}">{e(descricao_data)}</td>
                <td style="padding:14px 13px;border-bottom:1px solid #EEF2F7;vertical-align:top;font-size:12px;line-height:17px">
                  <div style="color:{link_cor};font-weight:700">{link_icone} {e(item['status'])}</div>
                  <a href="{e(item['url'])}" style="display:block;margin-top:3px;color:#087EF5;text-decoration:none;word-break:break-all">{e(item['url'])}</a>
                </td>
                <td style="padding:14px 13px;border-bottom:1px solid #EEF2F7;vertical-align:top;white-space:nowrap">
                  <span style="display:inline-block;padding:5px 10px;border-radius:7px;background:{apto_fundo};color:{apto_cor};font-size:11px;font-weight:700">{apto_icone} {apto}</span>
                </td>
              </tr>""")

        secoes.append(f"""
          <div style="margin-top:16px;background:#FFFFFF;border:1px solid #DCE3EC;border-radius:11px;overflow:hidden;box-shadow:0 1px 3px rgba(71,85,105,.05)">
            <div style="padding:16px 20px;border-bottom:1px solid #E8EDF3;color:#27364A;font-size:14px;font-weight:700">🧪 Todos os banners — {e(site)} <span style="color:#64748B;font-weight:600">({len(resultados)})</span></div>
            <div class="table-wrap" style="overflow-x:auto">
              <table role="presentation" style="width:100%;min-width:780px;border-collapse:collapse">
                <thead><tr style="background:#F7F9FC">
                  <th>Banner</th><th>Data lida</th><th>Data</th><th>Link</th><th>Apto?</th>
                </tr></thead>
                <tbody>{''.join(linhas)}</tbody>
              </table>
            </div>
          </div>""")

    data_exibicao = e(data_hora).replace(" as ", " &agrave;s ")
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Report de Banners — Bemol</title>
  <style>
    body {{ margin:0; padding:24px 12px; background:#F4F7FA; color:#334155; font-family:Arial,Helvetica,sans-serif; }}
    th {{ padding:11px 13px; text-align:left; border-bottom:1px solid #E3E9F0; color:#64748B; font-size:10px; line-height:14px; font-weight:700; letter-spacing:.55px; text-transform:uppercase; }}
    @media only screen and (max-width:640px) {{
      body {{ padding:0; }}
      .container {{ border-radius:0 !important; }}
      .header {{ padding:22px 18px !important; }}
      .status-count {{ display:block !important; margin:8px 0 0 !important; }}
      .summary-cell {{ display:block !important; width:auto !important; padding:5px 0 !important; }}
      .summary-table, .summary-row {{ display:block !important; width:100% !important; }}
    }}
  </style>
</head>
<body>
  <div class="container" style="max-width:1000px;margin:0 auto">
    <div style="border-radius:12px 12px 0 0;overflow:hidden">
      <div class="header" style="background:#087EF5;padding:25px 28px;color:#FFFFFF">
        <div style="font-size:18px;line-height:24px;font-weight:700">📋 Report de Banners — Bemol</div>
        <div style="margin-top:4px;color:#DCEEFF;font-size:12px;line-height:18px;font-weight:500">{data_exibicao}</div>
      </div>
      <div style="padding:14px 28px;background:{status_fundo};border:1px solid {status_borda};border-top:0;color:{status_cor};font-size:13px;line-height:20px">
        <strong>{status_icone} {e(status_texto)}</strong>
        <span class="status-count" style="float:right;margin-left:20px;color:#64748B;font-weight:500">{total_ok}/{total} banners OK</span>
      </div>
    </div>

    <table class="summary-table" role="presentation" style="width:100%;border-collapse:separate;border-spacing:0;margin:11px -5px 0;width:calc(100% + 10px)">
      <tr class="summary-row">
        {card(total, 'Banners analisados', '#334155')}
        {card(total_ok, 'Banners OK', '#15803D')}
        {card(total_problemas, 'Com problema', '#BE123C' if total_problemas else '#334155')}
        {card(total_conferir, 'Para conferir', '#A16207' if total_conferir else '#334155')}
      </tr>
    </table>

    {''.join(secoes)}

    <div style="padding:22px 10px 8px;text-align:center;color:#94A3B8;font-size:11px;line-height:17px">Gerado automaticamente · Bemol Banner Checker</div>
  </div>
</body></html>"""


def enviar_email(html_report, data_hora, totais):
    partes = []
    for quantidade, descricao in totais:
        if quantidade:
            partes.append(f"{quantidade} {descricao}")
    if not partes:
        partes.append("nenhum problema")
    prefixo = "REPORT COMPLETO" if REPORT_COMPLETO else "ATENCAO"
    mensagem = Mail(
        from_email=EMAIL_REMETENTE,
        to_emails=EMAIL_DESTINO,
        subject=f"{prefixo} - {'; '.join(partes)} - Bemol {data_hora}",
        html_content=html_report,
    )
    mensagem.tracking_settings = TrackingSettings(click_tracking=ClickTracking(enable=False, enable_text=False))
    resposta = SendGridAPIClient(SENDGRID_API_KEY).send(mensagem)
    print(f"E-mail enviado (status {resposta.status_code})")


async def main():
    data_hora = datetime.now(ZoneInfo("America/Manaus")).strftime("%d/%m/%Y as %H:%M")
    print(f"Iniciando verificacao - {data_hora}")
    todos_resultados = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        for site in SITES:
            resultados = await verificar_site(browser, site)
            todos_resultados.append((site["nome"], resultados))
        await browser.close()

    atualizar_historico(todos_resultados)
    html_report = gerar_html_report(todos_resultados, data_hora)
    with open("report_banners.html", "w", encoding="utf-8") as arquivo:
        arquivo.write(html_report)

    itens = [item for _, resultados in todos_resultados for item in resultados]
    total_erros = sum(not item["ok"] for item in itens)
    total_vencidos = sum(item.get("data_status") == "vencido" for item in itens)
    total_vencendo = sum(item.get("data_status") == "vencendo" for item in itens)
    total_antigos = sum(item.get("tempo_status") == "antigo" for item in itens)
    total_falhas = sum(item.get("leitura_situacao") == "falhou" for item in itens)
    deve_enviar = REPORT_COMPLETO or any((total_erros, total_vencidos, total_vencendo, total_antigos, total_falhas))
    if deve_enviar and SENDGRID_API_KEY:
        enviar_email(html_report, data_hora, [
            (total_erros, "link(s) com problema"),
            (total_vencidos, "banner(s) vencido(s)"),
            (total_vencendo, "banner(s) vencendo"),
            (total_antigos, "banner(s) antigo(s) sem data ativa"),
            (total_falhas, "falha(s) no leitor"),
        ])
    elif deve_enviar:
        print("Variavel SENDGRID_API_KEY nao configurada")


if __name__ == "__main__":
    asyncio.run(main())
