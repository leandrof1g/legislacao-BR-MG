"""Descobre onde cada município mineiro com mais de 30 mil habitantes publica sua legislação.

Para cada município, do mais populoso ao menos, testa:
  - SAPL (Interlegis) nos endereços usuais;
  - os sites prováveis da câmara e da prefeitura, e procura neles links e assinaturas de
    sistemas de legislação (SAPL, Leis Municipais, Siscam, Instar, Atende.net, Portal Fácil...);
  - a página do município no Leis Municipais (leismunicipais.com.br).

Resultado: `scripts/fontes_mg.json` (evidências brutas) e `municipais/MG/FONTES.md` (resumo), que
orientam quais coletores escrever e em que ordem.

    python3 -I scripts/descobrir_fontes_mg.py [--limite N]
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
import municipios_mg as mun  # noqa: E402

RAIZ = os.path.abspath(os.path.join(AQUI, ".."))
SAIDA_JSON = os.path.join(AQUI, "fontes_mg.json")
SAIDA_MD = os.path.join(RAIZ, "municipais", "MG", "FONTES.md")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

ASSINATURAS = {
    "sapl": r"\bsapl\b|Sistema de Apoio ao Processo Legislativo",
    "leismunicipais": r"leismunicipais\.com\.br",
    "siscam": r"siscam",
    "instar": r"instar\.com\.br|/portal/leis_decretos|portalinstar",
    "atende.net": r"atende\.net",
    "portalfacil": r"portalfacil|portal f[aá]cil",
    "plone/interlegis": r"Plone|interlegis",
    "betha": r"betha\.cloud|betha\.com\.br",
    "elotech": r"elotech",
    "govbr/smarapd": r"smarapd|govbr\.com\.br",
    "sintese": r"sintese\.net|camara\.mg\.gov\.br/legislacao",
    "diario-amm": r"diariomunicipal\.com\.br/amm-mg",
    "legislacaodigital": r"legislacaodigital",
    "cespro/ipm": r"cespro|ipm\.com\.br",
    "consultalei": r"consulta(lei|legislacao)",
}
PALAVRAS_LINK = re.compile(r"legisla|lei(s)?[-_/ ]|leis\b|norma|decreto|sapl|leismunicipais|atos[-_ ]oficiais", re.I)


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.geturl(), r.read(600_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:  # noqa: BLE001 — DNS, TLS, timeout
        return f"erro: {type(e).__name__}", url, ""


def candidatos(nome):
    s = mun.pasta_municipio(nome)
    j = s.replace("-", "")
    camara = [f"https://www.{j}.mg.leg.br", f"https://{j}.mg.leg.br", f"https://www.cm{j}.mg.gov.br",
              f"https://www.camara{j}.mg.gov.br", f"https://cm{j}.mg.gov.br", f"https://camara{j}.mg.gov.br",
              f"https://www.camarade{j}.mg.gov.br"]
    prefeitura = [f"https://www.{j}.mg.gov.br", f"https://{j}.mg.gov.br", f"https://www.pm{j}.mg.gov.br",
                  f"https://www.prefeitura{j}.mg.gov.br", f"https://prefeitura{j}.mg.gov.br"]
    sapl = [f"https://sapl.{j}.mg.leg.br", f"https://sapl.cm{j}.mg.gov.br", f"https://sapl.camara{j}.mg.gov.br",
            f"https://sapl.{j}.mg.gov.br"]
    return camara, prefeitura, sapl, f"https://leismunicipais.com.br/prefeitura/mg/{s}"


def links(base, html):
    saida = []
    for href, texto in re.findall(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        texto = re.sub(r"<[^>]+>|\s+", " ", texto).strip()
        if PALAVRAS_LINK.search(href) or PALAVRAS_LINK.search(texto):
            saida.append({"texto": texto[:80], "url": urllib.parse.urljoin(base, href)})
    vistos, unicos = set(), []
    for item in saida:
        if item["url"] not in vistos:
            vistos.add(item["url"])
            unicos.append(item)
    return unicos[:25]


def assinaturas(html):
    return sorted(k for k, rx in ASSINATURAS.items() if re.search(rx, html, re.I))


def sondar(item):
    codigo, nome, pop = item
    camara, prefeitura, sapl, lm = candidatos(nome)
    r = {"codigo_ibge": codigo, "nome": nome, "populacao": pop, "sapl": None, "camara": None, "prefeitura": None,
         "leismunicipais": None, "plataformas": [], "links": [], "tentativas": {}}
    for url in sapl:
        st, final, corpo = get(url + "/api/norma/normajuridica/?page_size=1")
        r["tentativas"][url] = st
        if st == 200 and '"results"' in corpo:
            r["sapl"] = urllib.parse.urlparse(final).netloc
            try:
                r["sapl_normas"] = json.loads(corpo)["pagination"]["total_entries"]
            except (KeyError, ValueError, TypeError):
                pass
            break
    for chave, lista in (("camara", camara), ("prefeitura", prefeitura)):
        for url in lista:
            st, final, corpo = get(url)
            r["tentativas"][url] = st
            if st == 200 and len(corpo) > 500:
                r[chave] = final
                r["plataformas"] += [f"{chave}:{a}" for a in assinaturas(corpo)]
                r["links"] += [{**l, "origem": chave} for l in links(final, corpo)]
                break
    st, final, corpo = get(lm)
    r["tentativas"][lm] = st
    if st == 200 and re.search(re.escape(nome.split()[0]), corpo, re.I):
        r["leismunicipais"] = final
    # SAPL citado em link de outro domínio (ex.: sapl.camaradeX.mg.gov.br)
    if not r["sapl"]:
        for l in r["links"]:
            host = urllib.parse.urlparse(l["url"]).netloc
            if "sapl" in host:
                st, final, corpo = get(f"https://{host}/api/norma/normajuridica/?page_size=1")
                if st == 200 and '"results"' in corpo:
                    r["sapl"] = host
                    break
    r["plataformas"] = sorted(set(r["plataformas"]))
    return r


def classificar(r):
    if r["sapl"]:
        return "SAPL"
    p = " ".join(r["plataformas"]) + " " + " ".join(l["url"] for l in r["links"])
    for chave, rotulo in (("leismunicipais", "Leis Municipais"), ("siscam", "Siscam"), ("instar", "Instar"),
                          ("atende.net", "Atende.net (IPM)"), ("portalfacil", "Portal Fácil"),
                          ("sintese", "Síntese"), ("betha", "Betha"), ("elotech", "Elotech")):
        if chave in p.lower():
            return rotulo
    if r["leismunicipais"]:
        return "Leis Municipais"
    if r["camara"] or r["prefeitura"]:
        return "site próprio"
    return "não encontrado"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=0)
    a = ap.parse_args()
    with open(mun.IBGE_MG, encoding="utf-8") as f:
        nomes = {m["codigo_ibge"]: m["nome"] for m in json.load(f)}
    pops = mun.populacoes()
    if not pops:
        sys.exit("sem dados de população do IBGE")
    alvo = sorted(((c, nomes.get(c, c), p) for c, p in pops.items() if p > mun.POP_MINIMA), key=lambda x: -x[2])
    if a.limite:
        alvo = alvo[: a.limite]
    print(f"{len(alvo)} municípios acima de {mun.POP_MINIMA} habitantes", flush=True)
    with ThreadPoolExecutor(12) as ex:
        resultados = list(ex.map(sondar, alvo))
    for r in resultados:
        r["classificacao"] = classificar(r)
        print(f"{r['nome']:<28} {r['populacao']:>9} {r['classificacao']:<16} {r['sapl'] or ''} {r['camara'] or ''}", flush=True)
    with open(SAIDA_JSON, "w", encoding="utf-8") as f:
        json.dump(resultados, f, ensure_ascii=False, indent=1)
    cont = {}
    for r in resultados:
        cont[r["classificacao"]] = cont.get(r["classificacao"], 0) + 1
    linhas = ["# Fontes de legislação municipal — Minas Gerais", "",
              f"Levantamento automático dos {len(resultados)} municípios com mais de 30 mil habitantes, do mais "
              "populoso ao menos. Gerado por `scripts/descobrir_fontes_mg.py`; evidências em `scripts/fontes_mg.json`.", "",
              "| Fonte | Municípios |", "| --- | ---: |"]
    linhas += [f"| {k} | {v} |" for k, v in sorted(cont.items(), key=lambda kv: -kv[1])]
    linhas += ["", "| # | Município | População | Fonte | Câmara | Prefeitura | Links de legislação |",
               "| ---: | --- | ---: | --- | --- | --- | --- |"]
    for i, r in enumerate(resultados, 1):
        lk = "; ".join(f"[{(l['texto'] or 'link')[:30]}]({l['url']})" for l in r["links"][:4])
        fonte = r["classificacao"] + (f" (`{r['sapl']}`)" if r["sapl"] else "")
        pop = f"{r['populacao']:,}".replace(",", ".")
        linhas.append(f"| {i} | {r['nome']} | {pop} | {fonte} | {r['camara'] or '—'} | "
                      f"{r['prefeitura'] or '—'} | {lk or '—'} |")
    os.makedirs(os.path.dirname(SAIDA_MD), exist_ok=True)
    with open(SAIDA_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")
    print(cont, flush=True)


if __name__ == "__main__":
    main()
