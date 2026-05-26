import asyncio
import smtplib
import os
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.async_api import async_playwright

# ─── Configuração ────────────────────────────────────────────────────────────
EMAIL_DESTINO = "gablaredox@gmail.com"
EMAIL_REMETENTE = os.environ.get("EMAIL_USER", "")
EMAIL_SENHA = os.environ.get("EMAIL_PASS", "")

SITES = [
    {"nome": "Bemol",       "url": "https://www.bemol.com.br"},
    {"nome": "Bemol Farma", "url": "https://www.bemolfarma.com.br"},
]

# ─── Coleta de links ──────────────────────────────────────────────────────────

async def coletar_links_banners(page, url):
    print(f"  → Acessando {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)

    # Clica nas setas do carrossel principal para carregar todos os slides
    right_arrows = await page.query_selector_all('[class*="sliderRightArrow"]')
    print(f"  → {len(right_arrows)} carrosséis encontrados")

    for arrow in right_arrows:
        for _ in range(30):
            try:
                await arrow.click()
                await page.wait_for_timeout(200)
            except Exception:
                break

    # Rola até o footer para lazy load
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(2000)

    # Clica nas setas do carrossel do footer
    right_arrows = await page.query_selector_all('[class*="sliderRightArrow"]')
    for arrow in right_arrows[-2:]:
        for _ in range(20):
            try:
                await arrow.click()
                await page.wait_for_timeout(200)
            except Exception:
                break

    await page.wait_for_timeout(1000)

    # Coleta links dos carrosséis
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
            // Remove duplicatas por href
            const seen = new Set();
            return result.filter(item => {
                if (seen.has(item.href)) return false;
                seen.add(item.href);
                return true;
            });
        }
    """)

    # Coleta banners de categoria (fora dos carrosséis)
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

    return links + cat_links


# ─── Verificação de URL ───────────────────────────────────────────────────────

async def verificar_url(page, href, base_url):
    if href.startswith('http'):
        full_url = href
    else:
        full_url = base_url.rstrip('/') + '/' + href.lstrip('/')

    # Domínios externos conhecidos que requerem autenticação ou redirecionam para app
    # Não é possível verificar automaticamente — marcamos como OK por padrão
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

    for tentativa in range(2):
        try:
            response = await page.goto(full_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)

            status = response.status if response else 0
            title = await page.title()

            is_error = status >= 400
            if '404' in title or 'não encontrad' in title.lower() or 'page not found' in title.lower():
                is_error = True

            # Se deu erro na primeira tentativa, espera 3s e tenta de novo
            if is_error and tentativa == 0:
                print(f"    ⚠ Erro na 1ª tentativa ({status}), tentando novamente...")
                await page.wait_for_timeout(3000)
                continue

            return {
                "url": full_url,
                "status": status,
                "title": title[:70],
                "ok": not is_error,
            }

        except Exception as e:
            if tentativa == 0:
                print(f"    ⚠ Exceção na 1ª tentativa, tentando novamente...")
                await page.wait_for_timeout(3000)
                continue
            return {
                "url": full_url,
                "status": "ERR",
                "title": str(e)[:60],
                "ok": False,
            }

    # Se chegou aqui, as duas tentativas falharam
    return {
        "url": full_url,
        "status": status if 'status' in dir() else "ERR",
        "title": title[:70] if 'title' in dir() else "Erro desconhecido",
        "ok": False,
    }


# ─── Verificação do site ──────────────────────────────────────────────────────

async def verificar_site(browser, site):
    print(f"\n{'='*50}")
    print(f"Verificando: {site['nome']} ({site['url']})")

    context = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    )
    page = await context.new_page()

    links = await coletar_links_banners(page, site['url'])
    print(f"  → {len(links)} banners encontrados")

    resultados = []
    for item in links:
        print(f"  → Verificando: {item['href'][:70]}")
        resultado = await verificar_url(page, item['href'], site['url'])
        resultado['section'] = item['section']
        resultados.append(resultado)
        await page.wait_for_timeout(300)

    await context.close()
    return resultados


# ─── Geração do relatório HTML ────────────────────────────────────────────────

def gerar_html_report(todos_resultados, data_hora):

    def secao_badge(sec):
        if sec == 'Categoria':
            return '<span style="background:#e3f2fd;color:#1565c0;padding:2px 8px;border-radius:10px;font-size:11px">Categoria</span>'
        else:
            return f'<span style="background:#e8f5e9;color:#2e7d32;padding:2px 8px;border-radius:10px;font-size:11px">{sec}</span>'

    total_ok    = sum(1 for _, res in todos_resultados for r in res if r['ok'])
    total_erro  = sum(1 for _, res in todos_resultados for r in res if not r['ok'])
    total       = total_ok + total_erro

    status_cor   = "#2e7d32" if total_erro == 0 else "#c62828"
    status_texto = "✅ Tudo OK!" if total_erro == 0 else f"⚠️ {total_erro} problema(s) encontrado(s)"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;max-width:900px;margin:0 auto;padding:20px;color:#333">

  <div style="background:#003087;color:white;padding:20px 24px;border-radius:8px 8px 0 0">
    <h1 style="margin:0;font-size:20px">📊 Report de Banners — Bemol</h1>
    <p style="margin:6px 0 0;opacity:0.8;font-size:13px">{data_hora}</p>
  </div>

  <div style="background:{status_cor};color:white;padding:14px 24px;font-size:16px;font-weight:bold">
    {status_texto} &nbsp;·&nbsp; {total_ok}/{total} banners OK
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
      <div style="color:#555;font-size:13px">Total</div>
    </div>
  </div>
"""

    for site_nome, resultados in todos_resultados:
        erros    = [r for r in resultados if not r['ok']]
        ok_count = len(resultados) - len(erros)

        html += f"""
  <h2 style="margin:24px 0 8px;font-size:16px;border-bottom:2px solid #003087;padding-bottom:6px">
    {site_nome} — {ok_count}/{len(resultados)} OK
  </h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <tr style="background:#f5f5f5">
      <th style="padding:8px 10px;text-align:left;border:1px solid #ddd">Status</th>
      <th style="padding:8px 10px;text-align:left;border:1px solid #ddd">Título</th>
      <th style="padding:8px 10px;text-align:left;border:1px solid #ddd">URL</th>
      <th style="padding:8px 10px;text-align:left;border:1px solid #ddd">Seção</th>
    </tr>
"""
        for r in resultados:
            bg           = "#ffffff" if r['ok'] else "#fff3f3"
            status_icon  = "✅" if r['ok'] else "❌"
            status_label = str(r['status'])
            html += f"""
    <tr style="background:{bg}">
      <td style="padding:7px 10px;border:1px solid #ddd;white-space:nowrap">{status_icon} {status_label}</td>
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


# ─── Envio de e-mail ──────────────────────────────────────────────────────────

def enviar_email(html, data_hora, total_erros):
    assunto = f"{'⚠️ ATENÇÃO' if total_erros > 0 else '✅ OK'} — Report Banners Bemol {data_hora}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"]    = EMAIL_REMETENTE
    msg["To"]      = EMAIL_DESTINO
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_REMETENTE, EMAIL_SENHA)
        server.sendmail(EMAIL_REMETENTE, EMAIL_DESTINO, msg.as_string())

    print(f"\n✅ E-mail enviado para {EMAIL_DESTINO}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    data_hora = datetime.now().strftime("%d/%m/%Y às %H:%M")
    print(f"\n🔍 Iniciando verificação — {data_hora}")

    todos_resultados = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for site in SITES:
            resultados = await verificar_site(browser, site)
            todos_resultados.append((site['nome'], resultados))
        await browser.close()

    html        = gerar_html_report(todos_resultados, data_hora)
    total_erros = sum(1 for _, res in todos_resultados for r in res if not r['ok'])

    with open("report_banners.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("📄 Relatório salvo em report_banners.html")

    if EMAIL_REMETENTE and EMAIL_SENHA:
        enviar_email(html, data_hora, total_erros)
    else:
        print("⚠️  Variáveis EMAIL_USER e EMAIL_PASS não configuradas")

    print(f"\n{'='*50}")
    print(f"Total com problema: {total_erros}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    asyncio.run(main())
