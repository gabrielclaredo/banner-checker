import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import unquote
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, TrackingSettings, ClickTracking
from playwright.async_api import async_playwright

# ─── Configuração ────────────────────────────────────────────────────────────
EMAIL_DESTINO    = ["gabriellaredo@bemol.com.br", "marketingbol@bemol.com.br"]
EMAIL_REMETENTE  = "gabriellaredo@bemol.com.br"
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")

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

    links = await page.evaluate("""
        () => {
            const sections = document.querySelectorAll('[class*="vtex-slider-layout-0-x-sliderTrackContainer"]');
            const result = [];
            let carouselIdx = 1;
            sections.forEach((section) => {
                const anchors = section.querySelectorAll('[class*="imageElementLink"]');
                if (anchors.length === 0) return;
                anchors.forEach(a => {
                    const href = (a.getAttribute('href') || '').split('?')[0].trim();
                    if (href && href !== '/' && href !== '#') {
                        result.push({ href, section: 'Carrossel ' + carouselIdx });
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
                result.push({ href, section: 'Categoria' });
            });
            return result;
        }
    """)

    todos = links + cat_links

    if len(todos) == 0:
        print(f"  ⚠ ATENÇÃO: nenhum banner encontrado em {url}. Verificar seletores CSS.")

    return todos


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
    await context.close()

    print(f"  → {len(links)} banners encontrados — verificando em paralelo...")

    async def verificar_com_log(item):
        print(f"  → Verificando: {item['href'][:70]}")
        resultado = await verificar_url(browser, item['href'], site['url'])
        resultado['section'] = item['section']
        return resultado

    resultados = await asyncio.gather(*[verificar_com_log(item) for item in links])
    return list(resultados)


# ─── Geração do relatório HTML ────────────────────────────────────────────────

def gerar_html_report(todos_resultados, data_hora):

    total_ok   = sum(1 for _, res in todos_resultados for r in res if r['ok'])
    total_erro = sum(1 for _, res in todos_resultados for r in res if not r['ok'])
    total      = total_ok + total_erro

    def secao_badge(sec):
        if sec == 'Categoria':
            return '<span style="background:#EFF6FF;color:#1D4ED8;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;font-family:Montserrat,sans-serif">Categoria</span>'
        return f'<span style="background:#F0FDF4;color:#15803D;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;font-family:Montserrat,sans-serif">{sec}</span>'

    def status_badge(status, ok):
        if ok:
            return f'<span style="background:#F0FDF4;color:#15803D;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;font-family:Montserrat,sans-serif">✓ {status}</span>'
        return f'<span style="background:#FEF2F2;color:#DC2626;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700;font-family:Montserrat,sans-serif">✕ {status}</span>'

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
    <div style="background:#FEF2F2;border:1px solid #FECACA;border-top:none;padding:14px 28px;display:flex;align-items:center;gap:10px">
      <span style="font-size:16px">⚠️</span>
      <span style="font-size:14px;font-weight:600;color:#DC2626">{total_erro} problema(s) encontrado(s)</span>
      <span style="margin-left:auto;font-size:13px;color:#64748B;font-weight:500">{total_ok}/{total} banners OK</span>
    </div>

    <!-- Cards de resumo -->
    <div style="display:flex;gap:12px;margin:16px 0">
      <div style="flex:1;background:white;border:1px solid #E2E8F0;border-radius:10px;padding:18px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.04)">
        <div style="font-size:32px;font-weight:700;color:#15803D">{total_ok}</div>
        <div style="font-size:12px;color:#64748B;font-weight:600;margin-top:2px;text-transform:uppercase;letter-spacing:0.5px">OK</div>
      </div>
      <div style="flex:1;background:white;border:1px solid #FECACA;border-radius:10px;padding:18px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.04)">
        <div style="font-size:32px;font-weight:700;color:#DC2626">{total_erro}</div>
        <div style="font-size:12px;color:#64748B;font-weight:600;margin-top:2px;text-transform:uppercase;letter-spacing:0.5px">Com problema</div>
      </div>
      <div style="flex:1;background:white;border:1px solid #E2E8F0;border-radius:10px;padding:18px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.04)">
        <div style="font-size:32px;font-weight:700;color:#1E293B">{total}</div>
        <div style="font-size:12px;color:#64748B;font-weight:600;margin-top:2px;text-transform:uppercase;letter-spacing:0.5px">Total verificado</div>
      </div>
    </div>
"""

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

def enviar_email(html, data_hora, total_erros):
    assunto = f"⚠️ ATENÇÃO — {total_erros} banner(s) com problema · Bemol {data_hora}"

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

    html = gerar_html_report(todos_resultados, data_hora)

    with open("report_banners.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("📄 Relatório salvo em report_banners.html")

    if total_erros > 0:
        print(f"⚠️  {total_erros} problema(s) encontrado(s) — enviando e-mail...")
        if SENDGRID_API_KEY:
            enviar_email(html, data_hora, total_erros)
        else:
            print("⚠️  Variável SENDGRID_API_KEY não configurada")
    else:
        print("✅ Tudo OK — nenhum e-mail enviado.")

    print(f"\n{'='*50}")
    print(f"Total com problema: {total_erros}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    asyncio.run(main())
