"""Atualização incremental do acervo: legislação federal, de Minas Gerais e de municípios mineiros.

Roda todo dia pelo GitHub Actions (.github/workflows/atualizar.yml) e faz, em cada esfera:

1. **Normas novas** — consulta o índice oficial e baixa o que ainda não existe no acervo.
2. **Normas alteradas** — quando a ementa de uma norma nova diz que ela altera outra
   ("Altera a Lei nº 14.130, de 19 de dezembro de 2001..."), a norma alterada é baixada de novo.
3. **Revalidação em rodízio** — a cada dia, 1/CICLO das normas com texto compilado é conferida de
   novo na fonte, de modo que o acervo inteiro é revisto a cada CICLO dias.
4. **Situação (MG)** — o índice da ALMG traz a situação de toda norma; mudanças (ex.: "Revogada")
   são aplicadas no cabeçalho sem baixar o texto.

Um arquivo só é regravado se o texto mudou de fato: diferenças só de formatação, de data de
coleta ou de hash de captura não geram commit.

Fontes: Senado/normas.leg.br (federal), ALMG Dados Abertos (MG) e API do SAPL das câmaras (municípios).

    python3 -I scripts/atualizar.py                       # tudo
    python3 -I scripts/atualizar.py --esferas mg --sem-revalidacao
    python3 -I scripts/atualizar.py --tempo-max 300       # para com folga antes do limite do Actions
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.join(AQUI, "estados"))

import baixar_federal as fed  # noqa: E402
import comum  # noqa: E402
import mg  # noqa: E402

RAIZ = os.path.abspath(os.path.join(AQUI, ".."))
HOJE = dt.date.today()
MESES = {m: i for i, m in enumerate(["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto",
                                     "setembro", "outubro", "novembro", "dezembro"], 1)}
# campos que mudam a cada coleta sem que a norma tenha mudado
VOLATEIS = re.compile(r"^(baixado_em|sha256_metadados_fonte|sha256_fonte|sha256_texto|fonte_metadados):.*$", re.M)

PRAZO = None
registro_lock = threading.Lock()
REGISTRO = {"novas": [], "alteradas": [], "situacao": [], "falhas": []}


class TempoEsgotado(Exception):
    pass


def checar_prazo():
    if PRAZO and time.monotonic() > PRAZO:
        raise TempoEsgotado()


def registrar(chave, caminho, extra=""):
    with registro_lock:
        REGISTRO[chave].append((os.path.relpath(caminho, RAIZ) if os.path.isabs(caminho) else caminho, extra))


def sem_acento(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def texto_essencial(conteudo):
    """O que importa para dizer se a norma mudou: cabeçalho sem campos voláteis e corpo só com letras e números."""
    partes = conteudo.split("\n---\n", 1)
    cab = VOLATEIS.sub("", partes[0]) if len(partes) == 2 else ""
    corpo = partes[1] if len(partes) == 2 else conteudo
    return re.sub(r"\s+", "", cab), re.sub(r"[\W_]+", "", corpo)


def reprocessar(md, executar, motivo):
    """Baixa de novo uma norma que já existe. Mantém o arquivo antigo se a coleta falhar ou se nada mudou."""
    if not os.path.exists(md):
        return "ausente"
    with open(md, encoding="utf-8") as f:
        antigo = f.read()
    bak = md + ".bak"
    os.replace(md, bak)
    try:
        status = executar()
    except TempoEsgotado:
        os.replace(bak, md)
        raise
    except Exception as e:  # noqa: BLE001
        status = f"erro: {e}"[:200]
    if status != "ok" or not os.path.exists(md):
        if os.path.exists(md):
            os.remove(md)
        os.replace(bak, md)
        return "falhou"
    os.remove(bak)
    with open(md, encoding="utf-8") as f:
        novo = f.read()
    if texto_essencial(novo) == texto_essencial(antigo):
        with open(md, "w", encoding="utf-8") as f:
            f.write(antigo)
        return "igual"
    registrar("alteradas", md, motivo)
    return "alterada"


def em_paralelo(itens, fn, workers=4, rotulo=""):
    cont = {}
    inicio = time.time()
    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(fn, it) for it in itens]
        try:
            for n, fut in enumerate(as_completed(futs), 1):
                try:
                    r = fut.result()
                except TempoEsgotado:
                    raise
                except Exception as e:  # noqa: BLE001
                    r = "erro"
                    registrar("falhas", rotulo, str(e)[:200])
                cont[r] = cont.get(r, 0) + 1
                if n % 200 == 0 or n == len(itens):
                    print(f"[{rotulo}] {n}/{len(itens)} {cont} {time.time() - inicio:.0f}s", flush=True)
        except TempoEsgotado:
            for f in futs:
                f.cancel()
            print(f"[{rotulo}] tempo esgotado; interrompendo", flush=True)
            raise
    return cont


def fatia(lista, ciclo, salto=0):
    """A parte de hoje no rodízio: itens cuja posição cai no dia do ciclo."""
    dia = (HOJE.toordinal() + salto) % ciclo
    return [x for i, x in enumerate(sorted(lista)) if i % ciclo == dia]


# ---------------------------------------------------------------- referências na ementa

REF = re.compile(
    r"(Lei Complementar|Lei Delegada|Decreto-Lei|Decreto|Lei)\s+(?:Federal\s+|Estadual\s+)?n[º°o.]*\s*([\d.]+)"
    r"(?:[,\s]+de\s+(\d{1,2})[º°o]?\s+de\s+([A-Za-zçÇ]+)\s+de\s+(\d{4}))?", re.I)


def referencias(ementa):
    """[(tipo, numero, data|None, federal_explicito)] das normas citadas como alteradas/regulamentadas."""
    if not ementa or not re.search(r"\b(altera|acrescenta|revoga|d[áa] nova reda[çc][ãa]o|modifica|regulamenta)", sem_acento(ementa)):
        return []
    saida = []
    for m in REF.finditer(ementa):
        tipo, num = m[1].lower(), m[2].replace(".", "")
        data = None
        if m[5] and sem_acento(m[4]) in MESES:
            try:
                data = dt.date(int(m[5]), MESES[sem_acento(m[4])], int(m[3]))
            except ValueError:
                data = None
        federal = "federal" in m[0].lower()
        saida.append((tipo, num, data, federal))
    return saida


# ---------------------------------------------------------------- federal

FED_PASTAS = {  # pasta -> código do Senado usado por baixar_federal.TIPOS
    "leis-ordinarias": "LEI-n", "leis-complementares": "LCP", "decretos-lei": "DEL", "decretos": "DEC-n",
    "leis-delegadas": "LDL",
}
FED_REF = {"lei": "leis-ordinarias", "lei complementar": "leis-complementares", "decreto-lei": "decretos-lei",
           "decreto": "decretos", "lei delegada": "leis-delegadas"}
SENADO_TIPOS = ["LEI", "LCP", "LDL", "EMC", "EMR", "DEL", "DEC", "DLG", "DPL", "MPV", "RSF", "RCN"]
LISTA = "https://legis.senado.leg.br/dadosabertos/legislacao/lista?tipo={}&ano={}"


def ler_cabecalho(md):
    with open(md, encoding="utf-8") as f:
        bruto = f.read(20000)
    partes = bruto.split("\n---\n", 1)
    cab = {}
    for linha in partes[0].splitlines()[1:]:
        if ":" in linha:
            k, v = linha.split(":", 1)
            v = v.strip()
            try:
                v = json.loads(v) if v.startswith('"') else v
            except json.JSONDecodeError:
                pass
            cab[k.strip()] = v
    return cab


def doc_de_arquivo(md):
    """Reconstrói o registro do índice do Senado a partir do cabeçalho de um .md federal."""
    pasta = os.path.relpath(md, fed.RAIZ).split(os.sep)[0]
    tipo = FED_PASTAS.get(pasta)
    if not tipo:
        return None
    cab = ler_cabecalho(md)
    data = str(cab.get("data") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data) or not cab.get("urn"):
        return None
    a, m, d = data.split("-")
    d = {"tipo": tipo, "dataassinatura": f"{d}/{m}/{a}", "numero": str(cab.get("numero") or ""), "id": "",
         "norma": os.path.basename(md)[:-3], "normaNome": cab.get("titulo") or "", "ementa": cab.get("ementa") or ""}
    seq = re.search(r";seq-sf-(\d+)$", cab["urn"])
    if seq:
        d["tipo"] = tipo.replace("-n", "-sn") if tipo.endswith("-n") else tipo
        d["sequencia"] = seq[1]
    try:
        if fed.montar(d)["md"] != md:
            return None
    except (KeyError, ValueError):
        return None
    return d


def federal_lista(tipo, ano):
    st, corpo = fed.http_get(LISTA.format(tipo, ano), "application/json")
    if st != 200 or not corpo.strip():
        return []
    resp = json.loads(corpo)
    docs = ((resp.get("ListaDocumento") or {}).get("documentos") or {})
    docs = docs.get("documento") if isinstance(docs, dict) else docs
    if not docs:
        return []
    return docs if isinstance(docs, list) else [docs]


def federal_processar(d):
    checar_prazo()
    status, _, extra = fed.processar(d)
    if status != "ok" and extra:
        registrar("falhas", fed.montar(d)["md"], extra.get("motivo") or str(extra.get("http") or status))
    return status


def federal(ciclo, revalidar, workers):
    fed.LIMITE = fed.Limitador(3)
    novos, referidos = [], {}
    for ano in sorted({HOJE.year - 1, HOJE.year}):
        for tipo in SENADO_TIPOS:
            checar_prazo()
            for d in federal_lista(tipo, ano):
                if d.get("tipo") not in fed.TIPOS:
                    continue
                if not os.path.exists(fed.montar(d)["md"]):
                    novos.append(d)
    print(f"[federal] {len(novos)} normas novas no índice do Senado", flush=True)

    def nova(d):
        r = federal_processar(d)
        if r == "ok":
            registrar("novas", fed.montar(d)["md"], d.get("ementa", "")[:160])
            for tipo, num, data, _ in referencias(d.get("ementa")):
                if data and tipo in FED_REF:
                    for alvo in glob.glob(os.path.join(fed.RAIZ, FED_REF[tipo], str(data.year),
                                                       f"*-{num}-{data.isoformat()}.md")):
                        with registro_lock:
                            referidos[alvo] = d["normaNome"]
        return r

    em_paralelo(novos, nova, workers, "federal:novas")

    def refazer(item):
        md, motivo = item
        checar_prazo()
        d = doc_de_arquivo(md)
        if not d:
            return "sem-registro"
        return reprocessar(md, lambda: federal_processar(d), motivo)

    print(f"[federal] {len(referidos)} normas alteradas por normas novas", flush=True)
    em_paralelo([(md, f"alterada por {n}") for md, n in referidos.items()], refazer, workers, "federal:alteradas")

    if revalidar:
        arquivos = []
        for pasta in FED_PASTAS:
            arquivos += glob.glob(os.path.join(fed.RAIZ, pasta, "*", "*.md"))
        hoje = [md for md in fatia(arquivos, ciclo) if md not in referidos]
        print(f"[federal] revalidando {len(hoje)} de {len(arquivos)} normas (ciclo de {ciclo} dias)", flush=True)
        em_paralelo([(md, "texto atualizado na fonte") for md in hoje], refazer, workers, "federal:revalidacao")


# ---------------------------------------------------------------- Minas Gerais

MG_REF = {"lei": "LEI", "lei complementar": "LCP", "decreto": "DEC", "lei delegada": "LDL", "decreto-lei": "DEL"}


def mg_md(r):
    pasta, _ = mg.TIPOS[r["Tipo"]]
    if pasta == "constituicao" and int(r["Ano"]) < 1988:
        pasta = "constituicoes-historicas"
    comp = f"-{r['Complemento']}" if r["Complemento"] else ""
    slug = f"{r['Tipo'].lower()}-{r['Numero']}{comp}-{r['Ano']}"
    return comum.caminhos(mg.UF, pasta, r["Ano"], slug)[0], slug


def mg_processar(r):
    checar_prazo()
    status, extra = mg.processar(r)
    if status not in ("ok", "pulado") and extra:
        registrar("falhas", mg_md(r)[0], extra.get("motivo") or str(extra.get("http") or status))
    return status


def mg_situacao(r, md):
    """Aplica no cabeçalho a situação que o índice da ALMG informa hoje."""
    situacao = (r.get("Situacao") or "").strip()
    if not situacao:
        return False
    revogada = {"revogada": True, "revogado": True, "vigente": False, "em vigor": False}.get(situacao.lower())
    with open(md, encoding="utf-8") as f:
        conteudo = f.read()
    cab, sep, corpo = conteudo.partition("\n---\n")
    atual = re.search(r'^situacao: "(.*)"$', cab, re.M)
    if not atual or atual[1].lower() == situacao.lower():
        return False
    cab = re.sub(r'^situacao: ".*"$', lambda _: f"situacao: {comum.yaml_str(situacao.capitalize())}", cab, count=1, flags=re.M)
    if revogada is not None:
        cab = re.sub(r"^revogada: .*$", f"revogada: {'true' if revogada else 'false'}", cab, count=1, flags=re.M)
    with open(md, "w", encoding="utf-8") as f:
        f.write(cab + sep + corpo)
    registrar("situacao", md, f"{atual[1]} → {situacao.capitalize()}")
    return True


def minas(ciclo, revalidar, workers):
    pasta_indice = os.path.join(comum.RAIZ_ESTADUAIS, mg.UF, "_indice")
    for ano in (HOJE.year - 1, HOJE.year):  # índices recentes sempre frescos
        arq = os.path.join(pasta_indice, f"{ano}.csv")
        if os.path.exists(arq):
            os.remove(arq)
    itens = mg.indice()
    print(f"[mg] {len(itens)} normas no índice da ALMG", flush=True)
    if mg.bloqueado.is_set() or not itens:
        registrar("falhas", "estaduais/MG", "índice da ALMG indisponível")
        return
    por_chave = {(r["Tipo"], r["Numero"], r["Ano"]): r for r in itens if not r["Complemento"]}

    novos, mudou_situacao = [], 0
    for r in itens:
        md, _ = mg_md(r)
        if not os.path.exists(md):
            novos.append(r)
        elif mg_situacao(r, md):
            mudou_situacao += 1
    print(f"[mg] {len(novos)} normas a baixar; situação atualizada em {mudou_situacao}", flush=True)

    referidos = {}

    def nova(r):
        st = mg_processar(r)
        if st == "ok":
            md, _ = mg_md(r)
            registrar("novas", md, (r.get("Ementa") or "")[:160])
            for tipo, num, data, federal_ in referencias(r.get("Ementa")):
                if federal_ or tipo not in MG_REF or not data:
                    continue
                alvo = por_chave.get((MG_REF[tipo], num, str(data.year)))
                if alvo and alvo["LinkTextoAtualizado"]:
                    with registro_lock:
                        referidos[mg_md(alvo)[0]] = (alvo, f"alterada por {mg.TIPOS[r['Tipo']][1]} nº {r['Numero']}/{r['Ano']}")
        return st

    em_paralelo(novos, nova, workers, "mg:novas")

    def refazer(item):
        md, (r, motivo) = item
        checar_prazo()
        _, slug = mg_md(r)
        cache = os.path.join(pasta_indice, "documentos", slug + ".xml.gz")
        if os.path.exists(cache):
            os.remove(cache)
        return reprocessar(md, lambda: mg_processar(r), motivo)

    print(f"[mg] {len(referidos)} normas alteradas por normas novas", flush=True)
    em_paralelo(list(referidos.items()), refazer, workers, "mg:alteradas")

    if revalidar:
        compiladas = {mg_md(r)[0]: r for r in itens if r["LinkTextoAtualizado"]}
        hoje = [md for md in fatia([m for m in compiladas if os.path.exists(m)], ciclo, salto=7) if md not in referidos]
        print(f"[mg] revalidando {len(hoje)} de {len(compiladas)} normas compiladas", flush=True)
        em_paralelo([(md, (compiladas[md], "texto atualizado na fonte")) for md in hoje], refazer, workers, "mg:revalidacao")


# ---------------------------------------------------------------- municípios (SAPL)

RAIZ_MUN = os.path.join(RAIZ, "municipais", "MG")
sapl_rede = comum.Rede(rps=2)


def camaras_sapl():
    """Câmaras com SAPL já presentes no acervo: host -> (pasta do município, nome, código IBGE)."""
    camaras, existentes = {}, set()
    config = os.path.join(AQUI, "camaras_sapl_mg.json")
    if os.path.exists(config):
        with open(config, encoding="utf-8") as f:
            for host, v in json.load(f).items():
                camaras[host] = (v["pasta"], v["municipio"], v.get("codigo_ibge", ""))
    for md in glob.glob(os.path.join(RAIZ_MUN, "*", "*", "*", "*.md")):
        m = re.search(r"-sapl-(\d+)\.md$", md)
        cab = ler_cabecalho(md)
        host = re.match(r"https?://([^/]+)/", cab.get("fonte") or "")
        if not host or not host[1].startswith("sapl."):
            continue
        if m:
            existentes.add((host[1], m[1]))
        pasta = os.path.relpath(md, RAIZ_MUN).split(os.sep)[0]
        camaras.setdefault(host[1], (pasta, cab.get("municipio") or pasta, cab.get("codigo_ibge") or ""))
    return camaras, existentes


def sapl_api(host, caminho):
    st, dados, _ = sapl_rede.get(f"https://{host}/api/{caminho}", "application/json", tentativas=2, timeout=60)
    if st != 200 or not dados:
        return None
    try:
        return json.loads(dados)
    except json.JSONDecodeError:
        return None


def sapl_normas(host, ano):
    tipos, normas, pag = {}, [], 1
    while pag <= 10:
        d = sapl_api(host, f"norma/tiponormajuridica/?page_size=100&page={pag}")
        if not d:
            break
        for t in d.get("results", []):
            tipos[t["id"]] = t.get("descricao") or t.get("__str__") or ""
        if not (d.get("pagination") or {}).get("next_page"):
            break
        pag += 1
    pag = 1
    while pag <= 20:
        checar_prazo()
        d = sapl_api(host, f"norma/normajuridica/?ano={ano}&page_size=100&page={pag}")
        if not d:
            break
        normas += [n for n in d.get("results", []) if str(n.get("ano")) == str(ano)]
        if not (d.get("pagination") or {}).get("next_page"):
            break
        pag += 1
    return tipos, normas


def sapl_gravar(host, camara, tipo_fonte, n):
    pasta_mun, municipio, ibge = camara
    url = (n.get("texto_integral") or "").replace("http://", "https://")
    if not url:
        return "sem_texto"
    st, bruto, ctype = sapl_rede.get(url, tentativas=2, timeout=120)
    if st != 200 or not bruto:
        registrar("falhas", f"municipais/MG/{pasta_mun}", f"SAPL {host} norma {n['id']}: HTTP {st}")
        return "sem_texto"
    try:
        corpo, motivo = comum.documento_para_md(bruto, url, ctype)
    except Exception as e:  # noqa: BLE001 — ex.: .doc sem conversor no Linux
        corpo, motivo = None, f"conversão falhou: {e}"[:200]
    if not corpo:
        registrar("falhas", f"municipais/MG/{pasta_mun}", f"SAPL {host} norma {n['id']}: {motivo}")
        return "sem_texto"
    pasta, tipo = comum.canonico(tipo_fonte)
    numero = str(n.get("numero") or "").strip()
    data = n.get("data") or ""
    try:
        data_br = dt.date.fromisoformat(data).strftime("%d/%m/%Y")
    except ValueError:
        data_br = ""
    titulo = f"{tipo} nº {numero}" + (f", de {data_br}" if data_br else f"/{n['ano']}")
    slug = f"{pasta}-{comum.slugify(numero.replace('.', '')) or 'sn'}-{n['ano']}-sapl-{n['id']}"
    md = os.path.join(RAIZ_MUN, pasta_mun, pasta, str(n["ano"]), slug + ".md")
    meta = [("titulo", titulo), ("tipo", tipo), ("tipo_na_fonte", tipo_fonte), ("numero", numero), ("data", data),
            ("ementa", " ".join((n.get("ementa") or "").split())), ("municipio", municipio), ("uf", "MG"),
            ("codigo_ibge", ibge), ("versao_texto", "texto integral anexado no SAPL municipal"),
            ("fonte", f"https://{host}/norma/{n['id']}"), ("fonte_texto", url),
            ("sha256_fonte", hashlib.sha256(bruto).hexdigest())]
    linhas = ["---"] + [f"{k}: {comum.yaml_str(v)}" for k, v in meta]
    linhas += [f"baixado_em: {comum.yaml_str(HOJE.isoformat())}", "---", "", f"# {titulo}", "", ""]
    os.makedirs(os.path.dirname(md), exist_ok=True)
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + corpo.strip() + "\n")
    registrar("novas", md, (n.get("ementa") or "")[:160])
    return "ok"


def municipios(workers):
    camaras, existentes = camaras_sapl()
    print(f"[municipios] {len(camaras)} câmaras com SAPL", flush=True)

    def camara(host):
        checar_prazo()
        cont = 0
        for ano in (HOJE.year - 1, HOJE.year):
            tipos, normas = sapl_normas(host, ano)
            for n in normas:
                if (host, str(n["id"])) in existentes:
                    continue
                checar_prazo()
                if sapl_gravar(host, camaras[host], tipos.get(n.get("tipo"), "Norma"), n) == "ok":
                    cont += 1
        return f"{cont} novas" if cont else "sem novidade"

    em_paralelo(sorted(camaras), camara, workers, "municipios")


# ---------------------------------------------------------------- relatório

def relatorio(inicio, interrompido):
    total = {k: len(v) for k, v in REGISTRO.items()}
    pasta = os.path.join(RAIZ, "atualizacoes", str(HOJE.year))
    os.makedirs(pasta, exist_ok=True)
    caminho = os.path.join(pasta, f"{HOJE.isoformat()}.md")
    linhas = [f"# Atualização de {HOJE.strftime('%d/%m/%Y')}", "",
              f"- Normas novas: **{total['novas']}**",
              f"- Normas com texto alterado: **{total['alteradas']}**",
              f"- Mudanças de situação (MG): **{total['situacao']}**",
              f"- Falhas de coleta: {total['falhas']}",
              f"- Duração: {(time.time() - inicio) / 60:.0f} min" + (" (interrompida pelo limite de tempo)" if interrompido else ""), ""]
    secoes = [("novas", "Normas novas"), ("alteradas", "Texto alterado"), ("situacao", "Situação alterada (MG)"),
              ("falhas", "Falhas de coleta")]
    for chave, titulo in secoes:
        itens = sorted(set(REGISTRO[chave]))
        if not itens:
            continue
        linhas += [f"## {titulo}", ""]
        for caminho_norma, extra in itens[:2000]:
            link = f"[`{caminho_norma}`](../../{caminho_norma})" if caminho_norma.endswith(".md") else f"`{caminho_norma}`"
            linhas.append(f"- {link}" + (f" — {extra}" if extra else ""))
        if len(itens) > 2000:
            linhas.append(f"- … e mais {len(itens) - 2000}")
        linhas.append("")
    houve_mudanca = total["novas"] or total["alteradas"] or total["situacao"]
    if houve_mudanca:
        with open(caminho, "w", encoding="utf-8") as f:
            f.write("\n".join(linhas))
    msg = (f"Atualização {HOJE.isoformat()}: {total['novas']} novas, {total['alteradas']} alteradas, "
           f"{total['situacao']} mudanças de situação")
    with open(os.path.join(RAIZ, ".mensagem_commit"), "w", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg, f"| falhas: {total['falhas']}", flush=True)


def main():
    global PRAZO
    ap = argparse.ArgumentParser()
    ap.add_argument("--esferas", default="federal,mg,municipios")
    ap.add_argument("--ciclo", type=int, default=30, help="dias para revalidar o acervo inteiro")
    ap.add_argument("--sem-revalidacao", action="store_true")
    ap.add_argument("--tempo-max", type=float, default=0, help="minutos; 0 = sem limite")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    inicio = time.time()
    if a.tempo_max:
        PRAZO = time.monotonic() + a.tempo_max * 60
    esferas = [e.strip() for e in a.esferas.split(",") if e.strip()]
    interrompido = False
    # municípios e MG primeiro: são rápidos; a revalidação federal, a mais longa, fica por último
    ordem = [("municipios", lambda: municipios(a.workers)),
             ("mg", lambda: minas(a.ciclo, not a.sem_revalidacao, a.workers)),
             ("federal", lambda: federal(a.ciclo, not a.sem_revalidacao, a.workers))]
    for nome, fn in ordem:
        if nome not in esferas:
            continue
        try:
            fn()
        except TempoEsgotado:
            interrompido = True
            break
        except Exception as e:  # noqa: BLE001 — uma fonte fora do ar não derruba as outras
            print(f"[{nome}] ERRO: {e}", flush=True)
            registrar("falhas", nome, str(e)[:300])
    relatorio(inicio, interrompido)
    for bak in glob.glob(os.path.join(RAIZ, "**", "*.md.bak"), recursive=True):
        os.replace(bak, bak[:-4])
    return 0


if __name__ == "__main__":
    sys.exit(main())
