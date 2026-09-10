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

    full_url = unquote(full_url)

    DOMINIOS_EXTERNOS_OK = [
        'app.contabemol.com.br',
        'emprestimocgi.bemol.com.br',
        'bit.ly',
    ]
    if any(d in full_url for d in DOMINIOS_EXTERNOS_OK):
        return {
            "url": full_url,
            "status": "OK*",
            "title": "Link externo (verificação manual)",
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
                    response = await page.goto(full_url, wait_until="domcontentloaded", timeout=20000)
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

    def secao_badge(sec):
        if sec == 'Categoria':
            return '<span style="background:#e3f2fd;color:#1565c0;padding:2px 8px;border-radius:10px;font-size:11px">Categoria</span>'
        return f'<span style="background:#e8f5e9;color:#2e7d32;padding:2px 8px;border-radius:10px;font-size:11px">{sec}</span>'

    total_ok   = sum(1 for _, res in todos_resultados for r in res if r['ok'])
    total_erro = sum(1 for _, res in todos_resultados for r in res if not r['ok'])
    total      = total_ok + total_erro

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;max-width:900px;margin:0 auto;padding:20px;color:#333">

  <div style="background:#003087;color:white;padding:20px 24px;border-radius:8px 8px 0 0">
    <h1 style="margin:0;font-size:20px">📊 Report de Banners — Bemol</h1>
    <p style="margin:6px 0 0;opacity:0.8;font-size:13px">{data_hora}</p>
  </div>

  <div style="background:#c62828;color:white;padding:14px 24px;font-size:16px;font-weight:bold">
    ⚠️ {total_erro} problema(s) encontrado(s) &nbsp;·&nbsp; {total_ok}/{total} banners OK
  </div>

  <div style="display:flex;gap:12px;padding:16px 0">
    <div style="flex:1;background:#e8f5e9;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#2e7d32">{total_ok}</div>
      <div style="color:#555;font-size:13px">OK</div>
    </div>
    <div style="flex:1;background:#ffebee;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#c62828">{total_erro}</div>
      <div style="color:#555;font-size:13px">Com problema</div>
    </div>
    <div style="flex:1;background:#e3f2fd;border-radius:8px;padding:16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#1565c0">{total}</div>
      <div style="color:#555;font-size:13px">Total verificado</div>
    </div>
  </div>
"""

    for site_nome, resultados in todos_resultados:
        erros = [r for r in resultados if not r['ok']]
        if not erros:
            continue

        html += f"""
  <h2 style="margin:24px 0 8px;font-size:16px;border-bottom:2px solid #003087;padding-bottom:6px">
    {site_nome} — {len(erros)} banner(s) com problema
  </h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <tr style="background:#f5f5f5">
      <th style="padding:8px 10px;text-align:left;border:1px solid #ddd">Status</th>
      <th style="padding:8px 10px;text-align:left;border:1px solid #ddd">Título</th>
      <th style="padding:8px 10px;text-align:left;border:1px solid #ddd">URL</th>
      <th style="padding:8px 10px;text-align:left;border:1px solid #ddd">Seção</th>
    </tr>
"""
        for r in erros:
            html += f"""
    <tr style="background:#fff3f3">
      <td style="padding:7px 10px;border:1px solid #ddd;white-space:nowrap">❌ {r['status']}</td>
      <td style="padding:7px 10px;border:1px solid #ddd">{r['title']}</td>
      <td style="padding:7px 10px;border:1px solid #ddd;font-family:monospace;font-size:11px">
        <a href="{r['url']}" style="color:#003087">{r['url'][:80]}</a>
      </td>
      <td style="padding:7px 10px;border:1px solid #ddd">{secao_badge(r['section'])}</td>
    </tr>"""

        html += "\n  </table>"

    html += """
  <p style="margin-top:24px;color:#999;font-size:11px;text-align:center">
    Gerado automaticamente · Bemol Banner Checker
  </p>
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
