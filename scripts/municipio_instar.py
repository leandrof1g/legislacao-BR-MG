"""Municípios que publicam a legislação no portal Instar (/portal/leis_decretos).

O Instar é usado por prefeituras e câmaras: a listagem traz 50 atos por página, do mais recente ao
mais antigo, com número, categoria, data, situação, ementa e o link de download. Quando o ato tem
página própria (/portal/leis_decretos/<id>/), o texto vem em HTML (às vezes compilado); senão, do
arquivo baixado (PDF, convertido com pdftotext ou OCR).

- Carga inicial: percorre a listagem inteira, página a página, retomável (estado em
  scripts/estado_instar.json).
- Todo dia: lê as primeiras páginas até achar uma página só com atos já conhecidos.
- Todo dia 1º: relê a listagem inteira para atualizar a situação (em vigor, revogada...) e baixar
  de novo os atos cuja página foi atualizada.
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import time
import urllib.parse

import comum
import municipios_mg as mun

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ_MUN = os.path.join(os.path.abspath(os.path.join(AQUI, "..")), "municipais", "MG")
ESTADO = os.path.join(AQUI, "estado_instar.json")
HOJE = dt.date.today()
# (host, município, código IBGE, órgão): municípios com unidade do CBMMG que usam o Instar
SITES = [
    ("www.betim.mg.gov.br", "Betim", "3106705", "prefeitura"),
    ("www.passos.mg.gov.br", "Passos", "3147907", "prefeitura"),
    ("www.paracatu.mg.gov.br", "Paracatu", "3147006", "prefeitura"),
    ("www.salinas.mg.gov.br", "Salinas", "3157005", "prefeitura"),
    ("www.camarasalinas.mg.gov.br", "Salinas", "3157005", "camara"),
]
# atos normativos gerais; portarias, editais e afins ficam de fora
CATEGORIAS = re.compile(r"^(lei|decreto|emenda|resolu|regimento|c[óo]digo|constitui)", re.I)
LISTA = "https://{host}/portal/leis_decretos/{pagina}/0/0/0/0/0/0/0/0/0/0/0/0/0/0/0/0/0/A/data-decrescente/simples"
_redes = {}


class TempoEsgotado(Exception):
    pass


def rede(host):
    if host not in _redes:
        _redes[host] = comum.Rede(rps=1.5)
    return _redes[host]


def get(host, url, **k):
    st, dados, ctype = rede(host).get(url, tentativas=3, timeout=180, **k)
    # downloads do Instar respondem com meta refresh para o arquivo real
    if st == 200 and dados[:200].lstrip().lower().startswith(b"<meta http-equiv=\"refresh\""):
        alvo = re.search(rb"url=([^\"'>]+)", dados)
        if alvo:
            return get(host, urllib.parse.urljoin(url, alvo[1].decode().strip()), **k)
    return st, dados, ctype, url


def limpo(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def listar_pagina(host, pagina):
    st, dados, _, _ = get(host, LISTA.format(host=host, pagina=pagina))
    if st != 200:
        raise RuntimeError(f"Instar {host} página {pagina}: HTTP {st}")
    s = dados.decode("utf-8", "replace")
    total = re.search(r'sw_qtde_resultados">\s*([\d.]+)', s)
    itens = []
    for bloco in s.split('<div class="leg_norma_listagem')[1:]:
        bloco = re.split(r"<!--\s*(?:Info listagem|Pagina[çc][ãa]o)|class=\"sw_paginacao", bloco)[0]
        ident = (re.search(r'data-lei="(\d+)"', bloco) or re.search(r'href="/portal/leis_decretos/(\d+)/"', bloco)
                 or re.search(r'/portal/download/legislacao/([^/"]+)/', bloco))
        numero = re.search(r'leg_numero_norma"><span>\s*N?º?\s*([^<]+)</span>', bloco)
        categoria = re.search(r'leg_categoria_norma">\s*([^<]+?)\s*<', bloco)
        if not categoria:
            continue
        if not ident:  # ato sem página nem arquivo: identifica por categoria, número e data
            chave = limpo(categoria[1]) + (numero[1] if numero else "") + bloco[:2000]
            ident = [None, "h" + hashlib.sha1(chave.encode()).hexdigest()[:10]]
        campos = dict((limpo(k).rstrip(":"), limpo(v)) for k, v in re.findall(
            r"<span[^>]*>\s*(Data|Situação|Autoria|Publicação)\s*:\s*</span>(.*?)</div>", bloco, re.S))
        if not campos.get("Data"):
            d = re.search(r"Data:\s*(?:</[^>]+>\s*)*(?:<[^>]+>\s*)*(\d{2}/\d{2}/\d{4})", bloco)
            campos["Data"] = d[1] if d else ""
        sit = re.search(r"Situação:.*?<[^>]*>\s*([^<]{3,60}?)\s*</", bloco, re.S)
        ementa = re.findall(r'<div class="leg_ementa[^"]*">(.*?)</div>', bloco, re.S)
        if not ementa:
            textos = [limpo(t) for t in re.findall(r">([^<>]{40,})<", bloco)]
            ementa = [t for t in textos if not t.startswith("Publicado")][:1]
        pub = re.search(r"Publicado[^<]+", bloco)
        links = re.findall(r'href="(/portal/download/legislacao/[^"]+)"', bloco)
        pagina_ato = re.search(r'href="(/portal/leis_decretos/\d+/)"', bloco)
        itens.append({
            "id": ident[1], "numero": limpo(numero[1]) if numero else "", "categoria": limpo(categoria[1]),
            "data": campos.get("Data", ""), "situacao": limpo(sit[1]) if sit else "",
            "ementa": limpo(ementa[0]) if ementa else "", "publicacao": limpo(pub[0]) if pub else "",
            "downloads": list(dict.fromkeys(links)), "pagina": pagina_ato[1] if pagina_ato else "",
        })
    return itens, int(total[1].replace(".", "")) if total else 0


def caminho_md(site, item):
    host, nome, _, orgao = site
    pasta, _ = comum.canonico(item["categoria"])
    m = re.search(r"/(\d{4})$", item["data"])
    ano = m[1] if m else "sem-ano"
    num = re.sub(r"[^\w-]+", "-", item["numero"].replace(".", "")).strip("-") or "sn"
    sigla = "cm" if orgao == "camara" else "pm"
    return os.path.join(RAIZ_MUN, mun.pasta_municipio(nome), pasta, ano, f"{pasta}-{num}-instar-{sigla}{item['id']}.md")


def texto_pagina(host, caminho):
    """Texto HTML do ato na página própria do Instar e a data de atualização informada."""
    st, dados, _, url = get(host, f"https://{host}{caminho}")
    if st != 200:
        return None, None, None
    s = dados.decode("utf-8", "replace")
    ini = s.find('id="conteudo_norma"')
    fim = s.find("<!-- Fim texto do ato -->", ini)
    if ini < 0 or fim < 0:
        return None, None, None
    trecho = s[s.find(">", ini) + 1:fim]
    atual = re.search(r"Atualizado em:\s*(\d{2}/\d{2}/\d{4})", s)
    return trecho, (atual[1] if atual else ""), url


def gravar(site, item, registrar, motivo=None):
    host, nome, ibge, orgao = site
    md = caminho_md(site, item)
    corpo, fonte_texto, bruto, atualizado = None, "", b"", ""
    if item["pagina"]:
        trecho, atualizado, url = texto_pagina(host, item["pagina"])
        if trecho and len(re.sub(r"\W", "", limpo(trecho))) >= 80:
            corpo = comum.html_para_md(trecho)
            fonte_texto, bruto = url, trecho.encode()
    if not corpo and item["downloads"]:
        st, bruto, ctype, url = get(host, f"https://{host}{item['downloads'][0]}")
        if st == 200 and bruto:
            corpo, motivo_falha = mun.converter(bruto, url, ctype)
            fonte_texto = url
            if not corpo:
                registrar("falhas", md, f"Instar {host}: {motivo_falha}")
                return "sem_texto"
    if not corpo:
        registrar("falhas", md, f"Instar {host}: ato sem texto disponível")
        return "sem_texto"
    _, tipo = comum.canonico(item["categoria"])
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", item["data"])
    data = f"{m[3]}-{m[2]}-{m[1]}" if m else ""
    titulo = f"{tipo} nº {item['numero']}" + (f", de {item['data']}" if data else "")
    pagina_publica = f"https://{host}{item['pagina']}" if item["pagina"] else LISTA.format(host=host, pagina=1)
    cab = [("titulo", titulo), ("tipo", tipo), ("tipo_na_fonte", item["categoria"]), ("numero", item["numero"]),
           ("data", data), ("ementa", item["ementa"]), ("situacao", item["situacao"]),
           ("revogada", True if "revogada totalmente" in item["situacao"].lower() else None),
           ("publicacao", item["publicacao"]), ("municipio", nome), ("uf", "MG"), ("codigo_ibge", ibge),
           ("orgao", "Câmara Municipal" if orgao == "camara" else "Prefeitura Municipal"),
           ("versao_texto", "página do ato no portal (Instar)" if fonte_texto.endswith("/") else "arquivo publicado"),
           ("fonte", pagina_publica), ("fonte_texto", fonte_texto), ("id_instar", item["id"]),
           ("atualizado_na_fonte", atualizado or ""), ("sha256_fonte", hashlib.sha256(bruto).hexdigest())]
    linhas = ["---"]
    for k, v in cab:
        if v is None:
            linhas.append(f"{k}: null")
        elif isinstance(v, bool):
            linhas.append(f"{k}: {'true' if v else 'false'}")
        else:
            linhas.append(f"{k}: {comum.yaml_str(v)}")
    linhas += [f"baixado_em: {comum.yaml_str(HOJE.isoformat())}", "---", "", f"# {titulo}", "", ""]
    anexos = [f"- [Anexo {i}](https://{host}{u})" for i, u in enumerate(item["downloads"][1:], 1)]
    extras = (["", "## Anexos", ""] + anexos) if anexos else []
    existia = os.path.exists(md)
    os.makedirs(os.path.dirname(md), exist_ok=True)
    with open(md + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + mun.ajustar_md(corpo).strip() + "\n" + "\n".join(extras) + ("\n" if extras else ""))
    os.replace(md + ".tmp", md)
    if existia and motivo:
        registrar("alteradas", md, motivo)
    elif not existia:
        registrar("novas", md, f"{nome}: {item['ementa'][:150]}")
    return "ok"


def cabecalho(md):
    with open(md, encoding="utf-8") as f:
        topo = f.read(6000).split("\n---\n", 1)[0]
    return {k: (json.loads(v) if v.startswith('"') else v) for k, v in re.findall(r"^(\w+): (.*)$", topo, re.M)}


def atualizar_situacao(md, item, registrar):
    cab = cabecalho(md)
    if not item["situacao"] or cab.get("situacao") == item["situacao"]:
        return
    with open(md, encoding="utf-8") as f:
        conteudo = f.read()
    topo, sep, corpo = conteudo.partition("\n---\n")
    topo = re.sub(r"^situacao: .*$", lambda _: f"situacao: {comum.yaml_str(item['situacao'])}", topo, count=1, flags=re.M)
    revogada = "true" if "revogada totalmente" in item["situacao"].lower() else "null"
    topo = re.sub(r"^revogada: .*$", f"revogada: {revogada}", topo, count=1, flags=re.M)
    with open(md, "w", encoding="utf-8") as f:
        f.write(topo + sep + corpo)
    registrar("situacao", md, f"{cab.get('situacao') or '—'} → {item['situacao']}")


def coletar(registrar, tempo_max=0):
    prazo = time.monotonic() + tempo_max * 60 if tempo_max else None
    estado = {}
    if os.path.exists(ESTADO):
        with open(ESTADO, encoding="utf-8") as f:
            estado = json.load(f)

    def checar():
        if prazo and time.monotonic() > prazo:
            raise TempoEsgotado()

    try:
        for site in SITES:
            host = site[0]
            e = estado.setdefault(host, {"carga_completa": False, "proxima_pagina": 1, "varredura": ""})
            varrer_tudo = not e["carga_completa"] or (HOJE.day == 1 and e.get("varredura") != HOJE.isoformat())
            pagina = e["proxima_pagina"] if not e["carga_completa"] else 1
            try:
                while True:
                    checar()
                    itens, total = listar_pagina(host, pagina)
                    if not itens:
                        break
                    conhecidos = 0
                    for item in itens:
                        if not CATEGORIAS.search(item["categoria"]):
                            conhecidos += 1
                            continue
                        md = caminho_md(site, item)
                        if os.path.exists(md):
                            conhecidos += 1
                            atualizar_situacao(md, item, registrar)
                            continue
                        checar()
                        gravar(site, item, registrar)
                    if not e["carga_completa"]:
                        e["proxima_pagina"] = pagina + 1
                    if not varrer_tudo and conhecidos == len(itens):
                        break
                    if pagina * 50 >= total:
                        e["carga_completa"] = True
                        e["proxima_pagina"] = 1
                        if varrer_tudo:
                            e["varredura"] = HOJE.isoformat()
                        break
                    pagina += 1
            except RuntimeError as erro:
                registrar("falhas", f"municipais/MG/{mun.pasta_municipio(site[1])}", str(erro)[:200])
    except TempoEsgotado:
        print("[instar] tempo esgotado; a coleta continua na próxima execução", flush=True)
    finally:
        with open(ESTADO, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
