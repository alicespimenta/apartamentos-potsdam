#!/usr/bin/env python3
"""Le Wunderflats e Kleinanzeigen, aplica os filtros de config.json e gera docs/index.html.

So usa a biblioteca padrao do Python. As URLs seguem o robots.txt de cada site:
filtros que o robots.txt proibe na URL (preco no Kleinanzeigen, pessoas e datas no
Wunderflats) sao aplicados aqui, depois da leitura.
"""
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent
ESTADO = RAIZ / "data" / "imoveis.json"
SAIDA = RAIZ / "docs" / "index.html"
USER_AGENT = "Mozilla/5.0 (compatible; busca-apartamento-pessoal/1.0)"
KA_BASE = "https://www.kleinanzeigen.de"
WF_BASE = "https://wunderflats.com"
KA_EXCLUIR = re.compile(
    r"wg[- ]?zimmer|\bwg\b(?![- ]?(geeignet|tauglich))|gesuch|\bsuche\b|tauschwohnung|wohnungstausch|wohnungsswap",
    re.I,
)
KA_SEM_COZINHA = re.compile(r"(ohne|keine)\s+(einbauk|ebk)", re.I)
KA_COM_COZINHA = re.compile(r"einbauk(ü|ue)che|\bebk\b", re.I)


class SiteBloqueado(Exception):
    pass


def baixar(url, pausa):
    time.sleep(pausa)
    pedido = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "de,en;q=0.8"})
    try:
        with urllib.request.urlopen(pedido, timeout=30) as resposta:
            return resposta.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as erro:
        raise SiteBloqueado(f"HTTP {erro.code} em {url}") from erro
    except urllib.error.URLError as erro:
        raise SiteBloqueado(f"sem conexao com {url}: {erro.reason}") from erro


def numero_de(texto):
    """'1.350' -> 1350.0 e '62,90' -> 62.9 (formato alemao)."""
    if not texto:
        return None
    try:
        return float(texto.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def texto_limpo(trecho):
    return html.unescape(re.sub(r"<[^>]+>", " ", trecho or "")).strip()


# ---------- Kleinanzeigen ----------

def ka_ler_cartoes(pagina):
    cartoes = []
    for bloco in pagina.split('<article class="flex justify-between p-medium" data-adid="')[1:]:
        ad_id = bloco.split('"', 1)[0]
        link = re.search(r'data-href="(/s-anzeige/[^"]+)"', bloco)
        if not link:
            continue
        titulo = re.search(r'"title":"((?:[^"\\]|\\.)*)"', bloco) or re.search(r'aria-label="([^"]+)"', bloco)
        descricao = re.search(r'"description":"((?:[^"\\]|\\.)*)"', bloco)
        preco = re.search(r"text-title3[^>]*>\s*([\d.]+)\s*€", bloco)
        medidas = re.search(r'font-strong text-onSurfaceSubdued">([^<]*)</p>', bloco)
        medidas = medidas.group(1) if medidas else ""
        area = re.search(r"([\d.,]+)\s*m²", medidas)
        quartos = re.search(r"([\d.,]+)\s*Zi", medidas)
        local = re.search(r"\b(144[6-8]\d)\s+([^<\n]+)", bloco)
        foto = re.search(r'<img[^>]+src="(https://img\.kleinanzeigen\.de[^"]+)"', bloco)
        cartoes.append({
            "id": f"ka-{ad_id}",
            "site": "Kleinanzeigen",
            "titulo": html.unescape(titulo.group(1)) if titulo else "Sem título",
            "resumo": html.unescape(descricao.group(1)) if descricao else "",
            "url": KA_BASE + link.group(1),
            "preco": numero_de(preco.group(1)) if preco else None,
            "area": numero_de(area.group(1)) if area else None,
            "quartos": numero_de(quartos.group(1)) if quartos else None,
            "local": f"{local.group(1)} {texto_limpo(local.group(2))}" if local else "",
            "foto": re.sub(r"rule=\$_\d+", "rule=$_59", html.unescape(foto.group(1))) if foto else "",
        })
    return cartoes


def ka_coletar(config, conhecidos, pausa):
    regras = config["kleinanzeigen"]
    base = regras["busca"]
    cartoes = {}
    for numero in range(1, regras["paginas"] + 1):
        url = base if numero == 1 else base.replace("/c203", f"/seite:{numero}/c203")
        for cartao in ka_ler_cartoes(baixar(url, pausa)):
            cartoes[cartao["id"]] = cartao
    if not cartoes:
        raise SiteBloqueado("nenhum anúncio na página: o site pode ter mudado ou bloqueado a leitura")

    aprovados, abertos = [], 0
    for cartao in cartoes.values():
        if not cartao["local"] or KA_EXCLUIR.search(cartao["titulo"]):
            continue
        if cartao["preco"] is None or cartao["preco"] > regras["preco_maximo"]:
            continue
        if cartao["area"] is not None and cartao["area"] < config["area_minima_m2"]:
            continue
        salvo = conhecidos.get(cartao["id"])
        if salvo and salvo.get("cozinha"):
            cartao["cozinha"] = salvo["cozinha"]
        elif abertos < config["maximo_anuncios_abertos_por_site"]:
            abertos += 1
            pagina = baixar(cartao["url"], pausa)
            # So a descricao do proprio anuncio: o resto da pagina cita Einbaukueche em outros anuncios.
            achado = re.search(r'<p id="viewad-description-text"[^>]*>(.*?)</p>', pagina, re.S)
            texto = cartao["titulo"] + " " + (texto_limpo(achado.group(1)) if achado else "")
            if 'checktag">Einbauküche' in pagina:
                cartao["cozinha"] = "confirmada"
            elif KA_SEM_COZINHA.search(texto):
                cartao["cozinha"] = "sem"
            elif KA_COM_COZINHA.search(texto):
                cartao["cozinha"] = "citada"
            else:
                cartao["cozinha"] = "sem"
        else:
            continue  # fica para a proxima execucao
        cartao["wbs"] = bool(re.search(r"\bWBS\b", cartao["titulo"] + " " + cartao["resumo"]))
        aprovados.append(cartao)
    return aprovados, len(cartoes)


# ---------- Wunderflats ----------

def wf_ler_pagina(pagina):
    achado = re.search(r'<script id="data-hydrant" type="application/json"[^>]*>(.*?)</script>', pagina, re.S)
    if not achado:
        raise SiteBloqueado("dados da busca não encontrados: o site pode ter mudado ou bloqueado a leitura")
    return json.loads(achado.group(1))["pageData"]["listingResults"]


def wf_link(pagina, item):
    for chave in (item.get("_id"), item.get("groupId")):
        if chave:
            achado = re.search(r'/en/furnished-apartment/[^"?\s]*' + re.escape(chave), pagina)
            if achado:
                return WF_BASE + achado.group(0)
    return ""


def wf_coletar(config, conhecidos, pausa):
    regras = config["wunderflats"]
    pagina = baixar(regras["busca"], pausa)
    resultado = wf_ler_pagina(pagina)
    itens = [(item, pagina) for item in resultado.get("items", [])]
    total = resultado.get("total", len(itens))
    numero = 2
    while len(itens) < total and numero <= 3:
        pagina = baixar(f'{regras["busca"]}&page={numero}', pausa)
        itens += [(item, pagina) for item in wf_ler_pagina(pagina).get("items", [])]
        numero += 1

    aprovados, abertos = [], 0
    for item, origem in itens:
        endereco = item.get("address") or {}
        preco = (item.get("price") or 0) / 100
        area = item.get("area")
        if endereco.get("city") != config["cidade"] or not preco or preco > regras["preco_maximo"]:
            continue
        if (item.get("accommodates") or 0) < regras["pessoas_minimo"]:
            continue
        if area is not None and area < config["area_minima_m2"]:
            continue
        url = wf_link(origem, item)
        if not url:
            continue
        identificador = f'wf-{item.get("_id")}'
        salvo = conhecidos.get(identificador)
        if salvo and salvo.get("cozinha"):
            cozinha = salvo["cozinha"]
        elif abertos < config["maximo_anuncios_abertos_por_site"]:
            abertos += 1
            detalhe = baixar(url, pausa)
            cozinha = "confirmada" if 'AmenitiesList-categoryTitle">Kitchen' in detalhe else "verificar"
        else:
            continue
        titulo = item.get("title") or {}
        fotos = item.get("images") or []
        disponivel = (item.get("availableFrom") or "")[:10]
        aprovados.append({
            "id": identificador,
            "site": "Wunderflats",
            "titulo": titulo.get("en") or titulo.get("de") or "Sem título",
            "resumo": f"Disponível desde {disponivel}" if disponivel else "",
            "url": url,
            "preco": preco,
            "area": area,
            "quartos": item.get("rooms"),
            "local": " ".join(filter(None, [endereco.get("street"), endereco.get("city")])),
            "foto": ((fotos[0].get("urls") or {}).get("thumbnail") or "") if fotos else "",
            "cozinha": cozinha,
            "wbs": False,
        })
    return aprovados, len(itens)


# ---------- Estado ----------

def carregar_estado():
    if ESTADO.exists():
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    return {"imoveis": {}, "execucoes": 0}


def atualizar_estado(estado, site, aprovados, hoje):
    imoveis = estado["imoveis"]
    vistos = set()
    for novo in aprovados:
        vistos.add(novo["id"])
        antigo = imoveis.get(novo["id"], {})
        novo["primeira_vez"] = antigo.get("primeira_vez", hoje)
        novo["ultima_vez"] = hoje
        novo["preco_anterior"] = antigo.get("preco") if antigo.get("preco") not in (None, novo["preco"]) else antigo.get("preco_anterior")
        novo["ativo"] = novo.get("cozinha") != "sem"
        imoveis[novo["id"]] = novo
    for chave, imovel in imoveis.items():
        if imovel["site"] == site and chave not in vistos:
            imovel["ativo"] = False


# ---------- HTML ----------

def moeda(valor):
    return f"{valor:,.0f} €".replace(",", ".")


def cartao_html(imovel, novo):
    e = lambda valor: html.escape(str(valor or ""), quote=True)
    url = imovel["url"] if imovel["url"].startswith("https://") else "#"
    foto = imovel.get("foto") or ""
    imagem = f'<img src="{e(foto)}" alt="" loading="lazy">' if foto.startswith("https://") else '<div class="sem-foto">Sem foto</div>'
    fatos = []
    if imovel.get("area"):
        fatos.append(f'{imovel["area"]:g} m²'.replace(".", ","))
    else:
        fatos.append("m² não informado")
    if imovel.get("quartos"):
        fatos.append(f'{imovel["quartos"]:g} cômodos'.replace(".", ","))
    etiquetas = []
    if novo:
        etiquetas.append('<span class="tag novo">Novo</span>')
    cozinha = {"confirmada": "Einbauküche", "citada": "Einbauküche citada no texto", "verificar": "Confira a cozinha"}
    etiquetas.append(f'<span class="tag">{cozinha.get(imovel.get("cozinha"), "Confira a cozinha")}</span>')
    if imovel.get("wbs"):
        etiquetas.append('<span class="tag alerta">Pede WBS</span>')
    if imovel.get("preco_anterior"):
        etiquetas.append(f'<span class="tag">Antes {moeda(imovel["preco_anterior"])}</span>')
    return f"""<a class="cartao" href="{e(url)}" target="_blank" rel="noopener noreferrer">
  <div class="foto">{imagem}</div>
  <div class="corpo">
    <div class="linha"><strong class="preco">{moeda(imovel["preco"])}</strong><span class="site">{e(imovel["site"])}</span></div>
    <h3>{e(imovel["titulo"])}</h3>
    <p class="fatos">{e(" · ".join(fatos))}</p>
    <p class="local">{e(imovel.get("local"))}</p>
    <div class="tags">{"".join(etiquetas)}</div>
    <p class="data">Visto pela primeira vez em {e(imovel["primeira_vez"])}</p>
  </div>
</a>"""


def gerar_html(estado, config, situacao, hoje, agora):
    ativos = [i for i in estado["imoveis"].values() if i.get("ativo")]
    ativos.sort(key=lambda i: (i["primeira_vez"], -i["preco"]), reverse=True)
    novos = [i for i in ativos if i["primeira_vez"] == hoje]
    demais = [i for i in ativos if i["primeira_vez"] != hoje]
    linhas = "".join(
        f'<li class="{"erro" if s["erro"] else "ok"}"><strong>{html.escape(nome)}</strong>: '
        + (html.escape(s["erro"]) if s["erro"] else f'{s["lidos"]} anúncios lidos, {s["aprovados"]} nos filtros')
        + "</li>"
        for nome, s in situacao.items()
    )
    manuais = "".join(
        f'<li><a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(nome)}</a>: abrir a busca manualmente</li>'
        for nome, url in config.get("links_manuais", {}).items()
    )
    grade = lambda lista, novo: "".join(cartao_html(i, novo) for i in lista)
    secao_novos = f'<div class="grade">{grade(novos, True)}</div>' if novos else '<p class="vazio">Nenhum apartamento novo hoje.</p>'
    secao_demais = f'<h2>Ainda disponíveis ({len(demais)})</h2><div class="grade">{grade(demais, False)}</div>' if demais else ""
    filtros = (
        f'{html.escape(config["cidade"])} · {config["area_minima_m2"]} m² ou mais · com Einbauküche · '
        f'Wunderflats até {moeda(config["wunderflats"]["preco_maximo"])} · '
        f'Kleinanzeigen até {moeda(config["kleinanzeigen"]["preco_maximo"])}'
    )
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Apartamentos em Potsdam</title>
<style>
:root {{ --fundo:#f6f4ef; --cartao:#fff; --texto:#22211e; --suave:#6b675e; --linha:#e3dfd5; --destaque:#1f6f5c; --destaque-texto:#fff; --alerta:#a4461c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fundo:#191a18; --cartao:#242522; --texto:#efece4; --suave:#a8a498; --linha:#3a3b36; --destaque:#5cc3a6; --destaque-texto:#10231e; --alerta:#f0a27c; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--fundo); color:var(--texto); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1120px; margin:0 auto; padding:32px 16px 64px; }}
h1 {{ font-size:32px; line-height:1.15; margin:0 0 8px; }}
h2 {{ font-size:21px; margin:40px 0 16px; }}
h3 {{ font-size:16px; line-height:1.3; margin:6px 0 4px; }}
p {{ margin:0; }}
.sub, .fatos, .local, .data, .site, .vazio {{ color:var(--suave); }}
.sub {{ margin-bottom:4px; }}
.fatos, .local {{ font-size:14px; }}
.data, .site {{ font-size:13px; }}
.grade {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(270px,1fr)); gap:16px; }}
.cartao {{ display:flex; flex-direction:column; overflow:hidden; color:inherit; text-decoration:none; background:var(--cartao); border:1px solid var(--linha); border-radius:10px; }}
.cartao:hover, .cartao:focus-visible {{ border-color:var(--destaque); }}
.foto {{ position:relative; overflow:hidden; aspect-ratio:3/2; background:var(--linha); }}
.foto img {{ position:absolute; inset:0; display:block; width:100%; height:100%; object-fit:cover; }}
.sem-foto {{ display:grid; place-items:center; height:100%; color:var(--suave); font-size:14px; }}
.corpo {{ display:flex; flex:1; flex-direction:column; gap:2px; padding:14px; }}
.linha {{ display:flex; justify-content:space-between; align-items:baseline; gap:8px; }}
.preco {{ font-size:21px; }}
.tags {{ display:flex; flex-wrap:wrap; gap:6px; margin:10px 0; }}
.tag {{ padding:2px 8px; border:1px solid var(--linha); border-radius:99px; font-size:12.5px; }}
.tag.novo {{ background:var(--destaque); border-color:var(--destaque); color:var(--destaque-texto); font-weight:600; }}
.tag.alerta {{ border-color:var(--alerta); color:var(--alerta); }}
.data {{ margin-top:auto; }}
ul {{ margin:0; padding-left:20px; }}
li.erro {{ color:var(--alerta); }}
footer {{ margin-top:48px; padding-top:16px; border-top:1px solid var(--linha); font-size:14px; color:var(--suave); }}
footer a {{ color:var(--destaque); }}
</style>
</head>
<body>
<main>
<h1>Apartamentos em Potsdam</h1>
<p class="sub">{filtros}</p>
<p class="sub">Atualizado em {html.escape(agora)}</p>
<h2>Novos hoje ({len(novos)})</h2>
{secao_novos}
{secao_demais}
<footer>
<ul>{linhas}{manuais}</ul>
</footer>
</main>
</body>
</html>
"""


def main():
    config = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
    momento = datetime.now(ZoneInfo(config["fuso_horario"]))
    hoje, agora = momento.strftime("%Y-%m-%d"), momento.strftime("%d/%m/%Y às %H:%M")
    pausa = config["pausa_entre_paginas_segundos"]
    estado = carregar_estado()
    situacao = {}
    for site, coletar in (("Wunderflats", wf_coletar), ("Kleinanzeigen", ka_coletar)):
        try:
            aprovados, lidos = coletar(config, estado["imoveis"], pausa)
            atualizar_estado(estado, site, aprovados, hoje)
            validos = sum(1 for a in aprovados if a.get("cozinha") != "sem")
            situacao[site] = {"erro": "", "lidos": lidos, "aprovados": validos}
        except (SiteBloqueado, KeyError, ValueError) as erro:
            situacao[site] = {"erro": f"leitura falhou ({erro}); os anúncios anteriores foram mantidos", "lidos": 0, "aprovados": 0}
        print(site, situacao[site])
    estado["execucoes"] += 1
    ESTADO.parent.mkdir(exist_ok=True)
    SAIDA.parent.mkdir(exist_ok=True)
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    SAIDA.write_text(gerar_html(estado, config, situacao, hoje, agora), encoding="utf-8")
    return 1 if all(s["erro"] for s in situacao.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
