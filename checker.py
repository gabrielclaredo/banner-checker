import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import unquote
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, TrackingSettings, ClickTracking
from playwright.async_api import async_playwright

# ─── NOVO: imports da leitura de datas (OCR local, sem serviço externo) ──────
import base64
import io
import re
import unicodedata
from datetime import date, timedelta
os.environ.setdefault("OMP_THREAD_LIMIT", "1")   # tesseract com 1 thread: evita lentidão em paralelo
from collections import Counter
from PIL import Image, ImageOps, ImageFilter
import pytesseract

# ─── Configuração ────────────────────────────────────────────────────────────
EMAIL_DESTINO    = ["gabriellaredo@bemol.com.br"]
EMAIL_REMETENTE  = "gabriellaredo@bemol.com.br"
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")

# ─── NOVO: configuração do leitor de datas ───────────────────────────────────
AVISO_DIAS = 2   # avisa quando faltam até X dias pro banner vencer

# REPORT_COMPLETO = True  → e-mail chega SEMPRE, listando todos os banners (fase de avaliação)
# REPORT_COMPLETO = False → e-mail chega só quando houver problema
REPORT_COMPLETO = True

SITES = [
    {"nome": "Bemol",       "url": "https://www.bemol.com.br"},
    {"nome": "Bemol Farma", "url": "https://www.bemolfarma.com.br"},
]

SEMAFORO = asyncio.Semaphore(3)

URLS_IGNORADAS = [
    'app.contabemol.com.br',
    'emprestimocgi.bemol.com.br',
    'bit.ly',
    'bemolfarma.com.br/super/super%20cimed',
    'bemolfarma.com.br/super/super cimed',
    'bemolfarma.com.br/super/super_cimed',
]

# ─── Coleta de links ──────────────────────────────────────────────────────────

async def coletar_links_banners(page, url):
    print(f"  → Acessando {url}")

    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)

    for pct in [25, 50, 75, 100]:
        await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pct} / 100)")
        await page.wait_for_timeout(1000)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(1000)

    right_arrows = await page.query_selector_all('[class*="sliderRightArrow"]')
    print(f"  → {len(right_arrows)} carrosséis encontrados")

    for arrow in right_arrows:
        for _ in range(30):
            try:
                await arrow.click()
                await page.wait_for_timeout(200)
            except Exception:
                break

    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(2000)

    right_arrows = await page.query_selector_all('[class*="sliderRightArrow"]')
    for arrow in right_arrows[-2:]:
        for _ in range(20):
            try:
                await arrow.click()
                await page.wait_for_timeout(200)
            except Exception:
                break

    await page.wait_for_timeout(1000)

    # NOVO: além do href, cada item agora traz também "img" (endereço da imagem do banner)
    links = await page.evaluate("""
        () => {
            const pegarImg = (a) => {
                const imgs = [...a.querySelectorAll('img')];
                for (const img of imgs) {
                    const src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
                    if (src && !src.startsWith('data:')) return src;
                }
                return null;
            };
            const sections = document.querySelectorAll('[class*="vtex-slider-layout-0-x-sliderTrackContainer"]');
            const result = [];
            let carouselIdx = 1;
            sections.forEach((section) => {
                const anchors = section.querySelectorAll('[class*="imageElementLink"]');
                if (anchors.length === 0) return;
                anchors.forEach(a => {
                    const href = (a.getAttribute('href') || '').split('?')[0].trim();
                    if (href && href !== '/' && href !== '#') {
                        result.push({ href, section: 'Carrossel ' + carouselIdx, img: pegarImg(a) });
                    }
                });
                carouselIdx++;
            });
            const seen = new Set();
            return result.filter(item => {
                if (seen.has(item.href)) return false;
                seen.add(item.href);
                return true;
            });
        }
    """)

    cat_links = await page.evaluate("""
        () => {
            const pegarImg = (a) => {
                const imgs = [...a.querySelectorAll('img')];
                for (const img of imgs) {
                    const src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
                    if (src && !src.startsWith('data:')) return src;
                }
                return null;
            };
            const inCarousel = new Set(
                [...document.querySelectorAll('[class*="vtex-slider-layout-0-x-sliderTrackContainer"] [class*="imageElementLink"]')]
                .map(a => (a.getAttribute('href') || '').split('?')[0].trim())
            );
            const allBanners = document.querySelectorAll('[class*="imageElementLink"]');
            const result = [];
            const seen = new Set();
            const blocked = ['facebook','instagram','twitter','x.com','youtube',
                             'linkedin','apple.com','google.com','siteblindado',
                             'consumidor.gov','ebit','compreconfie'];
            allBanners.forEach(a => {
                const href = (a.getAttribute('href') || '').split('?')[0].trim();
                if (!href || href === '/' || href === '#') return;
                if (inCarousel.has(href)) return;
                if (seen.has(href)) return;
                if (blocked.some(b => href.includes(b))) return;
                seen.add(href);
                result.push({ href, section: 'Categoria', img: pegarImg(a) });
            });
            return result;
        }
    """)

    todos = links + cat_links

    if len(todos) == 0:
        print(f"  ⚠ ATENÇÃO: nenhum banner encontrado em {url}. Verificar seletores CSS.")

    return todos


# ─── NOVO: Leitura de datas na imagem ─────────────────────────────────────────
# Nada aqui levanta exceção pra fora: se qualquer coisa falhar, devolve None
# e a verificação de links segue exatamente como antes.

def _hoje():
    # DATA_SIMULADA (formato AAAA-MM-DD) serve só pra teste: finge que hoje é outro dia
    simulada = os.environ.get("DATA_SIMULADA", "").strip()
    if simulada:
        try:
            return date.fromisoformat(simulada)
        except ValueError:
            pass
    return datetime.now(ZoneInfo("America/Manaus")).date()


MESES = {
    'jan': 1, 'janeiro': 1, 'fev': 2, 'fevereiro': 2, 'mar': 3, 'marco': 3,
    'abr': 4, 'abril': 4, 'mai': 5, 'maio': 5, 'jun': 6, 'junho': 6,
    'jul': 7, 'julho': 7, 'ago': 8, 'agosto': 8, 'set': 9, 'setembro': 9,
    'out': 10, 'outubro': 10, 'nov': 11, 'novembro': 11, 'dez': 12, 'dezembro': 12,
}
MESES_RE = '|'.join(sorted(MESES, key=len, reverse=True))

# dd/mm ou dd/mm/aa(aa)  — exige contexto de data pra não confundir com preço/parcela
RE_NUMERICA = re.compile(r'(?<![\d,.])(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?(?![\d,.]|\s*x)')
# "11 de outubro", "11 out", "11/out"
RE_EXTENSO  = re.compile(r'(?<!\d)(\d{1,2})\s*(?:de\s+|/\s*)?(' + MESES_RE + r')\b(?:\s*(?:de\s+)?(\d{4}))?')
# "até 11", "de 5 a 12 de outubro" -> o "5" sem mês herda o mês do próximo
RE_INTERVALO = re.compile(r'\b(?:de\s+)?(\d{1,2})\s*(?:a|ate|-|–)\s*(\d{1,2})\s*(?:de\s+)?(' + MESES_RE + r')\b')

PALAVRAS_RELATIVAS = [
    'so hoje', 'somente hoje', 'apenas hoje', 'hoje apenas', 'valido hoje',
    'esta semana', 'essa semana', 'ultima semana', 'ultimos dias', 'ultimo dia',
    'termina hoje', 'acaba hoje', 'termina amanha', 'acaba amanha',
    'termina domingo', 'ate domingo', 'ate sexta', 'ate sabado', 'ate segunda',
    'ate terca', 'ate quarta', 'ate quinta', 'fim de semana', 'final de semana',
    'esse final de semana', 'este final de semana', '24 horas', '24h',
]


def _normalizar(texto):
    texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode()
    texto = texto.lower()
    # Confusões comuns do OCR dentro de datas: "3O/11" -> "30/11", "30/l1" -> "30/11"
    # "30/[1", "30/]1", "30/{1", "30/|1", "3[/11"... -> o OCR troca o "1" fino por um traço/colchete
    texto = re.sub(r'(\d\s?/)\s?[\[\]{}|!li](?=\d)', r'\g<1>1', texto)
    texto = re.sub(r'(?<=/\d)[\[\]{}|](?=\D|$)', '1', texto)
    texto = re.sub(r'(?<=\d)[\[\]{}|](?=\d|/)', '1', texto)
    texto = re.sub(r'(?<=[\d/])[o](?=[\d/])', '0', texto)
    texto = re.sub(r'(?<=[\d/])[li|](?=[\d/]|\b)', '1', texto)
    texto = re.sub(r'(?<=\d)[o](?=/)', '0', texto)
    # Espaço perdido no meio do mês: "30/1 1" -> "30/11"
    texto = re.sub(r'(\b\d{1,2})\s*/\s*(\d)\s(\d)(?!\d)',
                   lambda m: f"{m.group(1)}/{m.group(2)}{m.group(3)}" if int(m.group(2) + m.group(3)) <= 12 else m.group(0),
                   texto)
    # Barra perdida em intervalo: "06 a 3011" -> "06 a 30/11" (fonte com "/" fina demais)
    def _barra(m):
        ini, dia, mes = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= ini <= 31 and 1 <= dia <= 31 and 1 <= mes <= 12:
            return f"{m.group(1)} a {m.group(2)}/{m.group(3)}"
        return m.group(0)
    texto = re.sub(r'(?<![\d/,.])(\d{1,2})\s*a\s*([0-3]\d)([01]\d)(?![\d/,.%])', _barra, texto)
    texto = re.sub(r'[^\w/\-–:,.\s]', ' ', texto)
    return re.sub(r'\s+', ' ', texto)


OCR_TIMEOUT = 20                                   # segundos por leitura; passou disso, conta como falha
OCR_SIMULTANEOS = asyncio.Semaphore(max(1, os.cpu_count() or 1))


LARGURA_LEITURA = 2800   # a letra miúda (rodapé "válido até…") só é lida bem com a imagem nesse tamanho


def _abrir_cinza(img_bytes, largura=LARGURA_LEITURA):
    img = Image.open(io.BytesIO(img_bytes))
    img.seek(0)                                    # GIF animado: usa o 1º quadro
    if img.mode in ('RGBA', 'LA', 'P', 'PA'):
        # Fundo transparente vira BRANCO (convertido direto ficaria preto e esconderia texto escuro)
        rgba = img.convert('RGBA')
        fundo = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(fundo, rgba)
    img = img.convert('RGB')
    if img.width < largura * 0.85:
        fator = largura / img.width
        img = img.resize((int(img.width * fator), int(img.height * fator)), Image.LANCZOS)
    return ImageOps.grayscale(img)


def _ocr(imagem, psm):
    return pytesseract.image_to_string(imagem, lang='por', config=f'--psm {psm}', timeout=OCR_TIMEOUT)


def _faixas(cinza, n=4, ampliar=1.0):
    """Corta a imagem em faixas horizontais (com sobra entre elas).
    Necessário porque o OCR ignora letra pequena quando há um título grande na mesma imagem."""
    h = cinza.height
    passo = h / n
    for i in range(n):
        y0 = max(0, int(i * passo - passo / 3))
        y1 = min(h, int((i + 1) * passo + passo / 3))
        f = cinza.crop((0, y0, cinza.width, y1))
        if ampliar != 1.0:
            f = f.resize((int(f.width * ampliar), int(f.height * ampliar)), Image.LANCZOS)
        yield f


def ler_textos_ocr(img_bytes, lupa=False):
    """Devolve uma lista de textos, um por leitura.
    Normal: imagem inteira ampliada (normal, nítida e invertida, 2 modos) + 4 faixas nas duas polaridades.
    Lupa: imagem inteira ainda mais ampliada (usada pra confirmar antes de acusar 'vencido')."""
    cinza = _abrir_cinza(img_bytes, LARGURA_LEITURA * (1.5 if lupa else 1))
    nitida = cinza.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    leituras = [
        (cinza, 11), (cinza, 6), (nitida, 11), (nitida, 6),
        (ImageOps.invert(cinza), 11), (ImageOps.invert(nitida), 6),
    ]
    if not lupa:
        for f in _faixas(cinza):
            leituras.append((f, 6))
            leituras.append((ImageOps.invert(f), 6))

    textos, erros = [], []
    for imagem, psm in leituras:
        try:
            textos.append(_ocr(imagem, psm))
        except Exception as e:
            erros.append(e)
    if not textos:
        # Nenhuma leitura funcionou: o leitor está com problema (não é só imagem sem texto)
        raise RuntimeError(f"OCR não rodou: {erros[-1] if erros else 'erro desconhecido'}")
    return textos


def _juntar_digito_perdido(votos):
    """O OCR costuma PERDER o '1' fino (lê 30/1 em vez de 30/11), mas quase nunca inventa dígito.
    Se uma data com mês de 1 dígito (30/01) concorre com a mesma data com mês de 2 dígitos que
    começa igual (30/11, 30/10, 30/12) lida em 2+ leituras, os votos vão pra versão mais longa."""
    votos = Counter(votos)
    for curta in list(votos):
        dc = date.fromisoformat(curta)
        if dc.month != 1:
            continue
        for longa in list(votos):
            dl = date.fromisoformat(longa)
            if longa != curta and votos[longa] >= 2 and dl.day == dc.day and dl.month in (10, 11, 12) \
                    and abs(dl.year - dc.year) <= 1:
                votos[longa] += votos.pop(curta)
                break
    return votos


def decidir_data(textos):
    """Junta várias leituras da mesma imagem.
    - A data que aparece em MAIS leituras vence (um dígito lido errado numa leitura não decide sozinho).
    - Empate: fica a mais distante, pra evitar alarme falso.
    - Se a vencedora já passou, mas 2+ leituras viram uma data ainda válida, as leituras
      discordam: o banner vai pra 'conferir' (nem alarme falso, nem passa calado)."""
    resultados = [extrair_data(t) for t in textos]
    votos = Counter(r['data_fim'] for r in resultados if r.get('data_fim'))
    votos = _juntar_digito_perdido(votos)
    if votos:
        mais_votos = max(votos.values())
        escolhida = max(d for d, v in votos.items() if v == mais_votos)
        hoje = _hoje()
        if date.fromisoformat(escolhida) < hoje:
            validas = {d: v for d, v in votos.items() if date.fromisoformat(d) >= hoje and v >= 2}
            if validas:
                alternativa = max(validas, key=lambda d: (validas[d], d))
                fmt = lambda d: date.fromisoformat(d).strftime('%d/%m/%Y')
                return {"tem_data": True, "data_fim": None, "confianca": "baixa",
                        "texto_encontrado": f"leituras divergentes: {fmt(escolhida)} ou {fmt(alternativa)}",
                        "votos": f"{votos[escolhida]} x {validas[alternativa]}"}
        r = next(r for r in resultados if r.get('data_fim') == escolhida)
        return dict(r, votos=f"{votos[escolhida]}/{len(textos)}")
    relativos = [r for r in resultados if r.get('tem_data')]
    if relativos:
        return relativos[0]
    return {"tem_data": False, "data_fim": None, "texto_encontrado": None, "confianca": "alta"}


def _ano_para(dia, mes, ano_txt, hoje):
    if ano_txt:
        ano = int(ano_txt)
        if ano < 100:
            ano += 2000
        return ano
    # Sem ano escrito: testa ano passado, atual e próximo e fica com o mais perto de hoje.
    # Ex.: "até 24/12" lido em janeiro vira dezembro passado (vencido), não dezembro que vem.
    melhor = None
    for ano in (hoje.year - 1, hoje.year, hoje.year + 1):
        try:
            d = date(ano, mes, dia)
        except ValueError:
            continue
        if melhor is None or abs((d - hoje).days) < abs((date(melhor, mes, dia) - hoje).days):
            melhor = ano
    return melhor


PALAVRAS_INICIO = ('a partir', 'comeca', 'inicia', 'inicio')


RE_INICIO_DE_INTERVALO = re.compile(r'\s*(?:a|ate|-|–|e)\s+\d')


def _eh_data_de_inicio(t, pos, fim=None):
    """True se a data é de INÍCIO: vem depois de 'a partir de', 'começa', 'inicia'...
    ou é a primeira data de um intervalo ('de 16/10 a 30/11' -> 16/10 é início)."""
    antes = t[max(0, pos - 20):pos]
    if any(p in antes for p in PALAVRAS_INICIO):
        return True
    if fim is not None and RE_INICIO_DE_INTERVALO.match(t, fim):
        return True
    return False


def extrair_data(texto_bruto):
    """Devolve dict no mesmo formato do leitor anterior."""
    hoje = _hoje()
    t = _normalizar(texto_bruto)
    datas = []   # (date, trecho)

    for m in RE_INTERVALO.finditer(t):
        if _eh_data_de_inicio(t, m.start()):
            continue
        mes = MESES[m.group(3)]
        ano = _ano_para(int(m.group(2)), mes, None, hoje)
        if ano:
            try:
                datas.append((date(ano, mes, int(m.group(2))), m.group(0)))
            except ValueError:
                pass

    for m in RE_EXTENSO.finditer(t):
        if _eh_data_de_inicio(t, m.start(), m.end()):
            continue
        dia, mes = int(m.group(1)), MESES[m.group(2)]
        ano = _ano_para(dia, mes, m.group(3), hoje)
        if ano:
            try:
                datas.append((date(ano, mes, dia), m.group(0)))
            except ValueError:
                pass

    for m in RE_NUMERICA.finditer(t):
        dia, mes = int(m.group(1)), int(m.group(2))
        if not (1 <= dia <= 31 and 1 <= mes <= 12):
            continue
        if _eh_data_de_inicio(t, m.start(), m.end()):
            continue
        # Exige alguma pista de que é data (evita "10/12" de preço solto)
        contexto = t[max(0, m.start() - 25):m.end() + 5]
        if not re.search(r'ate|valid|de |a |promo|oferta|dia|/\d{2,4}', contexto):
            continue
        ano = _ano_para(dia, mes, m.group(3), hoje)
        if ano:
            try:
                datas.append((date(ano, mes, dia), m.group(0)))
            except ValueError:
                pass

    if datas:
        # Descarta datas absurdas (mais de 1 ano no passado ou 2 anos no futuro: ruído do OCR)
        datas = [d for d in datas if hoje - timedelta(days=365) <= d[0] <= hoje + timedelta(days=730)]
    if datas:
        fim, trecho = max(datas, key=lambda x: x[0])
        return {"tem_data": True, "data_fim": fim.isoformat(),
                "texto_encontrado": trecho.strip(), "confianca": "alta"}

    for p in PALAVRAS_RELATIVAS:
        if p in t:
            return {"tem_data": True, "data_fim": None, "texto_encontrado": p, "confianca": "baixa"}

    return {"tem_data": False, "data_fim": None, "texto_encontrado": None, "confianca": "alta"}


def _texto_limpo(texto, limite=220):
    """Junta as linhas lidas pelo OCR sem repetição, pra mostrar no report."""
    vistas, linhas = set(), []
    for linha in texto.splitlines():
        linha = re.sub(r'\s+', ' ', linha).strip()
        if sum(c.isalnum() for c in linha) < 3:
            continue
        chave = linha.lower()
        if chave in vistas:
            continue
        vistas.add(chave)
        linhas.append(linha)
    resultado = ' · '.join(linhas)
    return resultado[:limite] + ('…' if len(resultado) > limite else '')


def _abre_no_leitor(body):
    try:
        Image.open(io.BytesIO(body)).load()
        return True
    except Exception:
        return False


async def _desenhar_no_navegador(context, body, tipo):
    """Plano B pra formatos que o leitor não abre (SVG, AVIF...):
    o navegador desenha a imagem e devolvemos um print dela em PNG."""
    tipo = tipo or ('image/svg+xml' if body.lstrip()[:5] in (b'<svg ', b'<?xml') else 'image/png')
    data_uri = f"data:{tipo};base64,{base64.b64encode(body).decode()}"
    pg = await context.new_page()
    try:
        await pg.set_content(
            '<html><body style="margin:0;background:#fff">'
            f'<img id="b" src="{data_uri}" style="display:block;min-width:1400px;height:auto">'
            '</body></html>')
        await pg.wait_for_function("document.getElementById('b').complete", timeout=10000)
        return await pg.locator('#b').screenshot(type='png', timeout=10000)
    finally:
        await pg.close()


async def ler_data_banner(request, item, context=None):
    """Devolve (situacao, leitura). situacao: 'ok' | 'sem_imagem' | 'falhou'."""
    src = item.get('img')
    if not src:
        return 'sem_imagem', None
    try:
        async with SEMAFORO:
            resp = await request.get(src, timeout=30000)
            if not resp.ok:
                item['motivo_falha'] = f"imagem não baixou (HTTP {resp.status})"
                return 'falhou', None
            body = await resp.body()
            tipo = resp.headers.get('content-type', '').split(';')[0].strip()
        if not _abre_no_leitor(body):
            if context is None:
                item['motivo_falha'] = f"formato não suportado ({tipo or 'desconhecido'})"
                return 'falhou', None
            async with SEMAFORO:
                body = await _desenhar_no_navegador(context, body, tipo)
        async with OCR_SIMULTANEOS:
            textos = await asyncio.to_thread(ler_textos_ocr, body)
        leitura = decidir_data(textos)
        # Antes de acusar "vencido", confere de novo com a imagem ampliada
        if leitura.get('data_fim') and date.fromisoformat(leitura['data_fim']) < _hoje():
            try:
                async with OCR_SIMULTANEOS:
                    textos += await asyncio.to_thread(ler_textos_ocr, body, True)
                leitura = decidir_data(textos)
            except Exception:
                pass
        # Texto pro report: leitura da imagem inteira + só as linhas das faixas que têm número
        # (as faixas cortam títulos ao meio e geram lixo, que não ajuda a avaliar)
        inteiras = textos[:1]
        de_faixas = [l for t in textos[1:] for l in t.splitlines() if re.search(r'\d', l)]
        leitura['texto_lido'] = _texto_limpo('\n'.join(inteiras + de_faixas))
        if leitura.get('tem_data'):
            print(f"    📅 {item['href'][:50]} → {leitura}")
        return 'ok', leitura
    except Exception as e:
        print(f"    ⚠ Leitor de data falhou em {src[:60]}: {e}")
        item['motivo_falha'] = str(e)[:80]
        return 'falhou', None


def classificar_data(leitura):
    """Devolve (status, descricao). status: 'vencido' | 'vencendo' | 'revisar' | None."""
    try:
        if not leitura or not leitura.get('tem_data'):
            return None, ''
        texto = leitura.get('texto_encontrado') or ''
        if leitura.get('data_fim'):
            fim  = date.fromisoformat(leitura['data_fim'])
            hoje = _hoje()
            if fim < hoje:
                dias = (hoje - fim).days
                return 'vencido', f"Venceu em {fim.strftime('%d/%m')} ({dias} dia(s) atrás) · \"{texto}\""
            if fim <= hoje + timedelta(days=AVISO_DIAS):
                return 'vencendo', f"Vence em {fim.strftime('%d/%m')} · \"{texto}\""
            return None, ''
        if texto.startswith('leituras divergentes'):
            return 'revisar', f"Data incerta ({texto}), conferir no banner"
        return 'revisar', f"Prazo relativo, conferir manualmente · \"{texto}\""
    except Exception:
        return None, ''


# ─── Verificação de URL ───────────────────────────────────────────────────────

async def verificar_url(browser, href, base_url):
    if href.startswith('http'):
        full_url = href
    else:
        full_url = base_url.rstrip('/') + '/' + href.lstrip('/')

    full_url_decoded = unquote(full_url)

    if any(d in full_url for d in URLS_IGNORADAS) or any(d in full_url_decoded for d in URLS_IGNORADAS):
        return {
            "url": full_url,
            "status": "OK*",
            "title": "Link ignorado (verificação manual)",
            "ok": True,
        }

    async with SEMAFORO:
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()

        status = "ERR"
        title  = "Erro desconhecido"

        try:
            for tentativa in range(2):
                try:
                    response = await page.goto(full_url_decoded, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(2000)

                    status = response.status if response else 0
                    title  = await page.title()

                    is_error = status >= 400
                    if '404' in title or 'não encontrad' in title.lower() or 'page not found' in title.lower():
                        is_error = True

                    if is_error and tentativa == 0:
                        print(f"    ⚠ Erro na 1ª tentativa ({status}), tentando novamente...")
                        await page.wait_for_timeout(3000)
                        continue

                    return {
                        "url":    full_url,
                        "status": status,
                        "title":  title[:70],
                        "ok":     not is_error,
                    }

                except Exception as e:
                    title = str(e)[:60]
                    if tentativa == 0:
                        print(f"    ⚠ Exceção na 1ª tentativa, tentando novamente...")
                        await page.wait_for_timeout(3000)
                        continue

            return {
                "url":    full_url,
                "status": status,
                "title":  title[:70],
                "ok":     False,
            }

        finally:
            await context.close()


# ─── Verificação do site (paralela) ──────────────────────────────────────────

async def verificar_site(browser, site):
    print(f"\n{'='*50}")
    print(f"Verificando: {site['nome']} ({site['url']})")

    context = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    )
    page = await context.new_page()
    links = await coletar_links_banners(page, site['url'])

    # NOVO: lê as datas nas imagens antes de fechar o contexto (usa a mesma sessão pra baixar)
    print(f"  → Lendo datas nas imagens dos banners (OCR)...")
    leituras = await asyncio.gather(*[ler_data_banner(page.request, item, context) for item in links])
    for item, (situacao, leitura) in zip(links, leituras):
        item['leitura_situacao'] = situacao
        item['leitura'] = leitura

    await context.close()

    print(f"  → {len(links)} banners encontrados — verificando em paralelo...")

    async def verificar_com_log(item):
        print(f"  → Verificando: {item['href'][:70]}")
        resultado = await verificar_url(browser, item['href'], site['url'])
        resultado['section'] = item['section']
        # NOVO: anexa o resultado da leitura de data
        resultado['img'] = item.get('img')
        resultado['leitura_situacao'] = item.get('leitura_situacao', 'falhou')
        resultado['motivo_falha'] = item.get('motivo_falha', '')
        leitura = item.get('leitura') or {}
        trecho = leitura.get('texto_encontrado') or ''
        resultado['data_trecho'] = '' if trecho.startswith('leituras divergentes') else trecho
        resultado['data_fim']   = leitura.get('data_fim')
        resultado['data_status'], resultado['data_desc'] = classificar_data(item.get('leitura'))
        return resultado

    resultados = await asyncio.gather(*[verificar_com_log(item) for item in links])
    return list(resultados)


# ─── Geração do relatório HTML ────────────────────────────────────────────────

def gerar_html_report(todos_resultados, data_hora):

    total_ok   = sum(1 for _, res in todos_resultados for r in res if r['ok'])
    total_erro = sum(1 for _, res in todos_resultados for r in res if not r['ok'])
    total      = total_ok + total_erro

    # NOVO: contagem de datas
    total_vencidos = sum(1 for _, res in todos_resultados for r in res if r.get('data_status') == 'vencido')
    total_vencendo = sum(1 for _, res in todos_resultados for r in res if r.get('data_status') == 'vencendo')
    total_revisar  = sum(1 for _, res in todos_resultados for r in res if r.get('data_status') == 'revisar')
    nao_lidos      = [(s, r) for s, res in todos_resultados for r in res if r.get('leitura_situacao') != 'ok']

    def secao_badge(sec):
        if sec == 'Categoria':
            return '<span style="background:#EFF6FF;color:#1D4ED8;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;font-family:Montserrat,sans-serif">Categoria</span>'
        return f'<span style="background:#F0FDF4;color:#15803D;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;font-family:Montserrat,sans-serif">{sec}</span>'

    def status_badge(status, ok):
        if ok:
            return f'<span style="background:#F0FDF4;color:#15803D;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;font-family:Montserrat,sans-serif">✓ {status}</span>'
        return f'<span style="background:#FEF2F2;color:#DC2626;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;font-family:Montserrat,sans-serif">✕ {status}</span>'

    # NOVO: badge de data
    def data_badge(status):
        if status == 'vencido':
            return '<span style="background:#FEF2F2;color:#DC2626;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;font-family:Montserrat,sans-serif">✕ VENCIDO</span>'
        if status == 'vencendo':
            return '<span style="background:#FFFBEB;color:#B45309;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;font-family:Montserrat,sans-serif">⏳ VENCE EM BREVE</span>'
        return '<span style="background:#F1F5F9;color:#475569;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;font-family:Montserrat,sans-serif">? REVISAR</span>'

    # Faixa de status: verde (tudo certo), amarela (atenção), vermelha (problema)
    if total_erro or total_vencidos:
        partes = []
        if total_erro:
            partes.append(f"{total_erro} link(s) com problema")
        if total_vencidos:
            partes.append(f"{total_vencidos} banner(s) vencido(s)")
        st_icone, st_texto, st_fundo, st_borda, st_cor = '⚠️', ' · '.join(partes), '#FEF2F2', '#FECACA', '#DC2626'
    elif total_vencendo or total_revisar or nao_lidos:
        partes = []
        if total_vencendo:
            partes.append(f"{total_vencendo} banner(s) vencendo em breve")
        if total_revisar:
            partes.append(f"{total_revisar} pra conferir")
        if nao_lidos:
            partes.append(f"{len(nao_lidos)} sem data verificada")
        st_icone, st_texto, st_fundo, st_borda, st_cor = '🔎', ' · '.join(partes), '#FFFBEB', '#FDE68A', '#B45309'
    else:
        st_icone, st_texto, st_fundo, st_borda, st_cor = '✅', 'Tudo certo: nenhum link com problema e nenhum banner vencido', '#F0FDF4', '#BBF7D0', '#15803D'

    def card(valor, rotulo, cor_valor, alerta):
        borda = '#FECACA' if alerta else '#E2E8F0'
        cor = cor_valor if (alerta or cor_valor != '#DC2626') else '#1E293B'
        return (f'<div style="flex:1;background:white;border:1px solid {borda};border-radius:10px;padding:18px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.04)">'
                f'<div style="font-size:32px;font-weight:700;color:{cor}">{valor}</div>'
                f'<div style="font-size:12px;color:#64748B;font-weight:600;margin-top:2px;text-transform:uppercase;letter-spacing:0.5px">{rotulo}</div></div>')

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700&display=swap" rel="stylesheet">
</head>
<body style="font-family:Montserrat,sans-serif;background:#F8FAFC;margin:0;padding:24px;color:#1E293B">

  <div style="max-width:900px;margin:0 auto">

    <!-- Header -->
    <div style="background:#007EFF;border-radius:12px 12px 0 0;padding:24px 28px;display:flex;align-items:center;gap:12px">
      <div>
        <h1 style="margin:0;font-size:18px;font-weight:700;color:white;letter-spacing:-0.3px">📋 Report de Banners — Bemol</h1>
        <p style="margin:4px 0 0;font-size:12px;color:rgba(255,255,255,0.75);font-weight:500">{data_hora}</p>
      </div>
    </div>

    <!-- Banner de status -->
    <div style="background:{st_fundo};border:1px solid {st_borda};border-top:none;padding:14px 28px;display:flex;align-items:center;gap:10px">
      <span style="font-size:16px">{st_icone}</span>
      <span style="font-size:14px;font-weight:600;color:{st_cor}">{st_texto}</span>
      <span style="margin-left:auto;font-size:13px;color:#64748B;font-weight:500">{total_ok}/{total} links OK</span>
    </div>

    <!-- Cards de resumo -->
    <div style="display:flex;gap:12px;margin:16px 0">
      {card(total_ok, 'Links OK', '#15803D', False)}
      {card(total_erro, 'Links com problema', '#DC2626', total_erro > 0)}
      {card(total_vencidos, 'Datas vencidas', '#DC2626', total_vencidos > 0)}
      {card(total, 'Total verificado', '#1E293B', False)}
    </div>
"""

    # ─── NOVO: report completo (todos os banners) — fase de avaliação ───────
    if REPORT_COMPLETO:
        def situacao_data(r):
            sit = r.get('leitura_situacao')
            if sit == 'sem_imagem':
                return '<span style="color:#B45309;font-weight:600">Imagem não encontrada</span>'
            if sit != 'ok':
                motivo = f'<br><span style="color:#94A3B8;font-size:11px">{r.get("motivo_falha")}</span>' if r.get('motivo_falha') else ''
                return f'<span style="color:#B45309;font-weight:600">Falha na leitura</span>{motivo}'
            ds = r.get('data_status')
            if ds == 'vencido':
                return f'<span style="color:#DC2626;font-weight:700">{r["data_desc"].split(" · ")[0]}</span>'
            if ds == 'vencendo':
                return f'<span style="color:#B45309;font-weight:700">{r["data_desc"].split(" · ")[0]}</span>'
            if ds == 'revisar':
                if 'divergentes' in (r.get('data_desc') or ''):
                    return f'<span style="color:#B45309;font-weight:600">{r["data_desc"]}</span>'
                return '<span style="color:#475569;font-weight:600">Prazo relativo (revisar)</span>'
            if r.get('data_fim'):
                try:
                    return f'<span style="color:#15803D;font-weight:600">Válido até {date.fromisoformat(r["data_fim"]).strftime("%d/%m/%Y")}</span>'
                except Exception:
                    pass
            return '<span style="color:#64748B">Sem data</span>'

        def apto(r):
            if not r['ok'] or r.get('data_status') == 'vencido':
                return '<span style="background:#FEF2F2;color:#DC2626;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;white-space:nowrap">✕ NÃO APTO</span>'
            if r.get('data_status') in ('vencendo', 'revisar') or r.get('leitura_situacao') != 'ok':
                return '<span style="background:#FFFBEB;color:#B45309;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;white-space:nowrap">! CONFERIR</span>'
            return '<span style="background:#F0FDF4;color:#15803D;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;white-space:nowrap">✓ APTO</span>'

        th = 'padding:10px 12px;text-align:left;font-size:11px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #F1F5F9'
        td = 'padding:12px;vertical-align:top;font-size:12px;border-bottom:1px solid #F1F5F9'
        for site_nome, resultados in todos_resultados:
            html += f"""
    <div style="background:white;border:1px solid #E2E8F0;border-radius:10px;overflow:hidden;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.04)">
      <div style="padding:14px 20px;border-bottom:1px solid #F1F5F9">
        <span style="font-size:14px;font-weight:700;color:#1E293B">🧪 Todos os banners — {site_nome} ({len(resultados)})</span>
      </div>
      <table style="width:100%;border-collapse:collapse">
        <tr style="background:#F8FAFC">
          <th style="{th}">Banner</th>
          <th style="{th}">Data lida</th>
          <th style="{th}">Data</th>
          <th style="{th}">Link</th>
          <th style="{th}">Apto?</th>
        </tr>"""
            for r in resultados:
                thumb = (f'<img src="{r["img"]}" style="width:160px;max-height:90px;object-fit:contain;border-radius:6px;display:block;margin-bottom:6px">'
                         if r.get('img') else '<div style="color:#94A3B8;font-size:11px;margin-bottom:6px">(sem imagem)</div>')
                texto = r.get('data_trecho') or '<span style="color:#94A3B8">—</span>'
                link_status = (f'<span style="color:#15803D;font-weight:700">✓ {r["status"]}</span>' if r['ok']
                               else f'<span style="color:#DC2626;font-weight:700">✕ {r["status"]}</span>')
                html += f"""
        <tr>
          <td style="{td};width:170px">{thumb}{secao_badge(r['section'])}</td>
          <td style="{td};color:#334155;max-width:220px">{texto}</td>
          <td style="{td}">{situacao_data(r)}</td>
          <td style="{td};max-width:200px">{link_status}<br>
            <a href="{r['url']}" style="color:#007EFF;text-decoration:none;font-family:monospace;font-size:11px;word-break:break-all">{r['url'][:80]}</a></td>
          <td style="{td}">{apto(r)}</td>
        </tr>"""
            html += "\n      </table>\n    </div>"

    # ─── NOVO: aviso de banners que o leitor de datas não conseguiu ler ──────
    if nao_lidos:
        itens = "".join(
            f'<li style="margin:4px 0">{s} · {r["section"]} · '
            f'{"imagem não encontrada no site" if r.get("leitura_situacao") == "sem_imagem" else "falha ao ler a imagem"} · '
            f'<a href="{r["url"]}" style="color:#007EFF;text-decoration:none">{r["url"][:60]}</a></li>'
            for s, r in nao_lidos
        )
        html += f"""
    <div style="background:#FFFBEB;border:1px solid #FDE68A;border-radius:10px;padding:14px 20px;margin-bottom:16px;font-size:13px">
      <div style="font-weight:700;color:#B45309;margin-bottom:6px">⚠️ Datas não verificadas em {len(nao_lidos)} banner(s) — conferir manualmente</div>
      <ul style="margin:0;padding-left:18px;color:#1E293B">{itens}</ul>
    </div>
"""

    # ─── NOVO: seção de banners com data ──────────────────────────────────────
    com_data = [(site_nome, r) for site_nome, res in todos_resultados for r in res if r.get('data_status')]
    ordem = {'vencido': 0, 'vencendo': 1, 'revisar': 2}
    com_data.sort(key=lambda x: ordem.get(x[1]['data_status'], 9))

    if com_data:
        html += f"""
    <!-- Seção Datas -->
    <div style="background:white;border:1px solid #E2E8F0;border-radius:10px;overflow:hidden;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.04)">
      <div style="padding:14px 20px;border-bottom:1px solid #F1F5F9;display:flex;align-items:center;justify-content:space-between">
        <span style="font-size:14px;font-weight:700;color:#1E293B">📅 Banners com data de validade</span>
        <span style="font-size:12px;font-weight:600;color:#DC2626;background:#FEF2F2;padding:3px 10px;border-radius:20px">{total_vencidos} vencido(s) · {total_vencendo} vencendo · {total_revisar} revisar</span>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="background:#F8FAFC">
          <th style="padding:10px 16px;text-align:left;font-size:11px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #F1F5F9">Status</th>
          <th style="padding:10px 16px;text-align:left;font-size:11px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #F1F5F9">Banner</th>
          <th style="padding:10px 16px;text-align:left;font-size:11px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #F1F5F9">Detalhe</th>
          <th style="padding:10px 16px;text-align:left;font-size:11px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #F1F5F9">Site / Seção</th>
        </tr>
"""
        for site_nome, r in com_data:
            thumb = f'<img src="{r["img"]}" style="max-width:180px;max-height:80px;border-radius:6px;display:block;margin-bottom:6px">' if r.get('img') else ''
            html += f"""
        <tr style="border-bottom:1px solid #F8FAFC">
          <td style="padding:12px 16px;white-space:nowrap;vertical-align:top">{data_badge(r['data_status'])}</td>
          <td style="padding:12px 16px;vertical-align:top">
            {thumb}
            <a href="{r['url']}" style="color:#007EFF;text-decoration:none;font-family:monospace;font-size:11px">{r['url'][:60]}</a>
          </td>
          <td style="padding:12px 16px;color:#1E293B;font-weight:500;font-size:13px;vertical-align:top">{r['data_desc']}</td>
          <td style="padding:12px 16px;vertical-align:top">{site_nome}<br>{secao_badge(r['section'])}</td>
        </tr>"""

        html += "\n      </table>\n    </div>"

    for site_nome, resultados in todos_resultados:
        erros = [r for r in resultados if not r['ok']]
        if not erros:
            continue

        html += f"""
    <!-- Seção {site_nome} -->
    <div style="background:white;border:1px solid #E2E8F0;border-radius:10px;overflow:hidden;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.04)">
      <div style="padding:14px 20px;border-bottom:1px solid #F1F5F9;display:flex;align-items:center;justify-content:space-between">
        <span style="font-size:14px;font-weight:700;color:#1E293B">{site_nome}</span>
        <span style="font-size:12px;font-weight:600;color:#DC2626;background:#FEF2F2;padding:3px 10px;border-radius:20px">{len(erros)} banner(s) com problema</span>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="background:#F8FAFC">
          <th style="padding:10px 16px;text-align:left;font-size:11px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #F1F5F9">Status</th>
          <th style="padding:10px 16px;text-align:left;font-size:11px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #F1F5F9">Título</th>
          <th style="padding:10px 16px;text-align:left;font-size:11px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #F1F5F9">URL</th>
          <th style="padding:10px 16px;text-align:left;font-size:11px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid #F1F5F9">Seção</th>
        </tr>
"""
        for r in erros:
            html += f"""
        <tr style="border-bottom:1px solid #F8FAFC">
          <td style="padding:12px 16px;white-space:nowrap">{status_badge(r['status'], r['ok'])}</td>
          <td style="padding:12px 16px;color:#1E293B;font-weight:500;font-size:13px">{r['title']}</td>
          <td style="padding:12px 16px;font-family:monospace;font-size:11px">
            <a href="{r['url']}" style="color:#007EFF;text-decoration:none">{r['url'][:80]}</a>
          </td>
          <td style="padding:12px 16px">{secao_badge(r['section'])}</td>
        </tr>"""

        html += "\n      </table>\n    </div>"

    html += """
    <p style="margin-top:20px;color:#94A3B8;font-size:11px;text-align:center;font-weight:500">
      Gerado automaticamente · Bemol Banner Checker
    </p>

  </div>
</body>
</html>"""

    return html


# ─── Envio de e-mail via SendGrid ─────────────────────────────────────────────

def enviar_email(html, data_hora, total_erros, total_vencidos=0, total_vencendo=0, total_falhas=0, leitor_com_problema=False):
    # NOVO: assunto reflete também as datas
    partes = []
    if total_erros:
        partes.append(f"{total_erros} link(s) com problema")
    if total_vencidos:
        partes.append(f"{total_vencidos} banner(s) vencido(s)")
    if total_vencendo:
        partes.append(f"{total_vencendo} vencendo")
    if total_falhas:
        partes.append(f"leitor de datas falhou em {total_falhas}")
    if not partes:
        partes.append("leitor de datas não encontrou imagens" if leitor_com_problema else "nenhum problema")
    prefixo = "🧪 REPORT COMPLETO" if REPORT_COMPLETO else "⚠️ ATENÇÃO"
    assunto = f"{prefixo} — {' · '.join(partes)} · Bemol {data_hora}"

    message = Mail(
        from_email=EMAIL_REMETENTE,
        to_emails=EMAIL_DESTINO,
        subject=assunto,
        html_content=html,
    )

    message.tracking_settings = TrackingSettings(
        click_tracking=ClickTracking(enable=False, enable_text=False)
    )

    sg = SendGridAPIClient(SENDGRID_API_KEY)
    response = sg.send(message)
    print(f"\n✅ E-mail enviado para {EMAIL_DESTINO} (status {response.status_code})")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    data_hora = datetime.now(ZoneInfo("America/Manaus")).strftime("%d/%m/%Y às %H:%M")
    print(f"\n🔍 Iniciando verificação — {data_hora}")

    todos_resultados = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for site in SITES:
            resultados = await verificar_site(browser, site)
            todos_resultados.append((site['nome'], resultados))
        await browser.close()

    total_erros = sum(1 for _, res in todos_resultados for r in res if not r['ok'])

    # NOVO: contagem de datas
    total_vencidos = sum(1 for _, res in todos_resultados for r in res if r.get('data_status') == 'vencido')
    total_vencendo = sum(1 for _, res in todos_resultados for r in res if r.get('data_status') == 'vencendo')
    total_falhas   = sum(1 for _, res in todos_resultados for r in res if r.get('leitura_situacao') == 'falhou')
    total_banners  = sum(len(res) for _, res in todos_resultados)
    total_sem_img  = sum(1 for _, res in todos_resultados for r in res if r.get('leitura_situacao') == 'sem_imagem')
    # Se nenhum banner teve imagem encontrada, algo mudou no site: também avisa
    leitor_com_problema = total_falhas > 0 or (total_banners > 0 and total_sem_img == total_banners)

    html = gerar_html_report(todos_resultados, data_hora)

    with open("report_banners.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("📄 Relatório salvo em report_banners.html")

    if REPORT_COMPLETO or total_erros > 0 or total_vencidos > 0 or total_vencendo > 0 or leitor_com_problema:
        print(f"⚠️  {total_erros} link(s) com problema · {total_vencidos} vencido(s) · {total_vencendo} vencendo · {total_falhas} falha(s) de leitura — enviando e-mail...")
        if SENDGRID_API_KEY:
            enviar_email(html, data_hora, total_erros, total_vencidos, total_vencendo, total_falhas, leitor_com_problema)
        else:
            print("⚠️  Variável SENDGRID_API_KEY não configurada")
    else:
        print("✅ Tudo OK — nenhum e-mail enviado.")

    print(f"\n{'='*50}")
    print(f"Total com problema: {total_erros} links · {total_vencidos} datas vencidas")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    asyncio.run(main())
