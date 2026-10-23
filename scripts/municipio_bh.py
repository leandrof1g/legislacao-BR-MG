"""Belo Horizonte — legislação municipal pelo Sistema de Informação Legislativa (SIL) da CMBH.

A consulta pública da Câmara Municipal (www.cmbh.mg.gov.br/atividade-legislativa/pesquisar-legislacao)
reúne leis, decretos do Executivo, emendas à Lei Orgânica e resoluções, com o texto atualizado
em .doc, os anexos e o histórico de alterações. Não há API documentada: o coletor usa os mesmos
pedidos que a página faz (pesquisar.php para listar, proxy.php para detalhar).

- Carga inicial: por tipo e ano, do mais recente ao mais antigo, retomável (estado em
  scripts/estado_bh.json).
- Depois: normas do ano corrente e do anterior, todos os dias.
- Revalidação em rodízio: a cada dia, 1/CICLO das normas é detalhada de novo; se o documento
  principal mudou (a CMBH publica um novo .doc consolidado quando a norma é alterada), o texto é
  baixado de novo.
"""
import datetime as dt
import glob
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

import comum
import municipios_mg as mun

RAIZ = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PASTA = os.path.join(RAIZ, "municipais", "MG", "belo-horizonte")
ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estado_bh.json")
BASE = "https://www.cmbh.mg.gov.br/sites/all/modules/consulta_normas"
PAGINA = "https://www.cmbh.mg.gov.br/atividade-legislativa/pesquisar-legislacao"
MUNICIPIO, IBGE = "Belo Horizonte", "3106200"
# atos de natureza normativa geral; atos internos da Câmara (portaria, ato do presidente,
# instrução de serviço, deliberação) ficam de fora
TIPOS = {
    "Lei Orgânica": "2c907f7619968b1a011a0179819f12aa",
    "Emenda à Lei Orgânica": "2c907f78064b443701064b444bb80006",
    "Lei": "2c907f78064b443701064b444c020008",
    "Decreto": "2c907f7809dba21a0109df3553190007",
    "Decreto-Lei": "2c907f7613f91fa6011445bc0eff0004",
    "Decreto Legislativo": "2c907f766c440a9f016c52c24feb004c",
    "Resolução": "2c907f78064b443701064b444c130009",
}
ANO_INICIAL = 1897  # fundação da capital
CICLO = 30
rede = comum.Rede(rps=2, headers={"X-Requested-With": "XMLHttpRequest",
                                  "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
HOJE = dt.date.today()


class TempoEsgotado(Exception):
    pass


def post(script, campos):
    corpo = urllib.parse.urlencode(campos).encode()
    req = urllib.request.Request(f"{BASE}/{script}", data=corpo, headers=rede.headers)
    ultimo = None
    for n in range(4):
        rede.limite.esperar()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            ultimo = e
            time.sleep(3 * (n + 1) ** 2)
    raise RuntimeError(f"CMBH {script}: {ultimo}")


def campos(**extra):
    base = {"metodo": "", "paginaRequerida": "1", "urlNorma": "", "idNorma": "", "buscarEmendas": "",
            "buscaViaUrl": "", "mobile": "0", "tipoDeNorma": "", "numero": "[número]", "ano": "[ano]",
            "assunto": "", "assunto2": "", "autoria": "", "tema": ""}
    base.update({k: str(v) for k, v in extra.items()})
    return base


def listar(tipo, ano, prazo=None):
    """[(id, url_servico, tipo, numero, ano, ementa)] de um tipo num ano, percorrendo todas as páginas."""
    itens, pagina, total = [], 1, None
    while True:
        if prazo and time.monotonic() > prazo:
            raise TempoEsgotado()
        s = post("pesquisar.php", campos(paginaRequerida=pagina, tipoDeNorma=TIPOS[tipo], ano=ano))
        if total is None:
            m = re.search(r"de um total de (\d+) itens", s)
            total = int(m[1]) if m else 0
        novos = 0
        for caminho, titulo, resto in re.findall(
                r"<span data-caminho='([^']+)' class='detalhar_normas[^']*'>(.*?)</span>(.*?)</li>", s, re.S):
            m = re.match(r"(.+?)\s*-\s*([\w.\-]+)/(\d{4})$", html.unescape(titulo).strip())
            if not m:
                continue
            em = re.search(r"Ementa:\s*</strong>(.*?)</p>", resto, re.S)
            ident = re.search(r"id=([0-9a-f]+)", caminho)
            itens.append({"id": ident[1] if ident else caminho, "caminho": caminho, "tipo": tipo,
                          "numero": m[2], "ano": m[3],
                          "ementa": " ".join(html.unescape(re.sub(r"<[^>]+>", " ", em[1])).split()) if em else ""})
            novos += 1
        if not novos or len(itens) >= total:
            return itens
        pagina += 1


def detalhar(caminho):
    s = post("proxy.php", campos(urlNorma=caminho, numero="", ano=""))
    docs = [(html.unescape(n).strip(), html.unescape(u)) for n, u in re.findall(
        r"<li><span>(.*?)</span><a href=\"([^\"]+documentoDaNorma[^\"]+)\"", s)]
    meta = {}
    for rotulo, valor in re.findall(r"<strong>\s*([^<:]+?):?\s*</strong>\s*(.*?)</(?:li|p)>", s, re.S):
        valor = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", valor)).split())
        meta.setdefault(rotulo.strip().rstrip(":"), valor)
    url = re.search(r"<span id='novaUrl'[^>]*>([^<]+)</span>", s)
    return docs, meta, (url[1] if url else PAGINA)


def documento_principal(docs):
    if not docs:
        return None
    for criterio in (lambda n: "atual" in n.lower() and "anexo" not in n.lower(),
                     lambda n: "anexo" not in n.lower()):
        for nome, url in docs:
            if criterio(nome):
                return nome, url
    return docs[0]


def caminho_md(item):
    pasta, _ = comum.canonico(item["tipo"])
    num = re.sub(r"[^\w-]+", "", item["numero"].replace(".", ""))
    return os.path.join(PASTA, pasta, item["ano"], f"{pasta}-{num}-{item['ano']}-cmbh.md")


def data_iso(valor):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", valor or "")
    return f"{m[3]}-{m[2]}-{m[1]}" if m else ""


def id_doc(url):
    m = re.search(r"idDocDaNorma=([0-9a-f]+)", url or "")
    return m[1] if m else ""


def gravar(item, registrar, motivo=None):
    docs, meta, url_publica = detalhar(item["caminho"])
    principal = documento_principal(docs)
    md = caminho_md(item)
    if not principal:
        registrar("falhas", md, "CMBH: norma sem documento de texto")
        return "sem_texto"
    nome_doc, url_doc = principal
    st, bruto, ctype = rede.get(url_doc, tentativas=3, timeout=300)
    if st != 200 or not bruto:
        registrar("falhas", md, f"CMBH: documento HTTP {st}")
        return "sem_texto"
    corpo, motivo_falha = mun.converter(bruto, nome_doc, ctype)
    if not corpo:
        registrar("falhas", md, f"CMBH: {motivo_falha}")
        return "sem_texto"
    _, tipo = comum.canonico(item["tipo"])
    data = data_iso(meta.get("Data da promulgação") or meta.get("Data"))
    titulo = f"{tipo} nº {item['numero']}" + (f", de {meta.get('Data da promulgação')}" if data else f"/{item['ano']}")
    cab = [("titulo", titulo), ("tipo", tipo), ("tipo_na_fonte", item["tipo"]), ("numero", item["numero"]),
           ("data", data), ("data_publicacao", data_iso(meta.get("Data da publicação"))),
           ("ementa", meta.get("Ementa") or item["ementa"]), ("municipio", MUNICIPIO), ("uf", "MG"),
           ("codigo_ibge", IBGE), ("id_norma", item["id"]),
           ("versao_texto", "atualizada (CMBH)" if "atual" in nome_doc.lower() else "publicada pela CMBH"),
           ("fonte", url_publica), ("fonte_texto", url_doc), ("id_documento", id_doc(url_doc)),
           ("sha256_fonte", hashlib.sha256(bruto).hexdigest())]
    extras = []
    anexos = [(n, u) for n, u in docs if (n, u) != principal]
    if anexos:
        extras += ["", "## Anexos e outros documentos", ""] + [f"- [{n}]({u})" for n, u in anexos]
    if meta.get("Observações"):
        extras += ["", "## Observações da CMBH", "", meta["Observações"]]
    linhas = ["---"] + [f"{k}: {comum.yaml_str(v)}" for k, v in cab]
    linhas += [f"baixado_em: {comum.yaml_str(HOJE.isoformat())}", "---", "", f"# {titulo}", "", ""]
    existia = os.path.exists(md)
    os.makedirs(os.path.dirname(md), exist_ok=True)
    with open(md + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + corpo.strip() + "\n" + "\n".join(extras) + ("\n" if extras else ""))
    os.replace(md + ".tmp", md)
    if existia and motivo:
        registrar("alteradas", md, motivo)
    elif not existia:
        registrar("novas", md, f"{MUNICIPIO}: {item['ementa'][:150]}")
    return "ok"


def carregar_estado():
    if os.path.exists(ESTADO):
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    return {"concluidos": {}}


def salvar_estado(estado):
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def cabecalho(md):
    with open(md, encoding="utf-8") as f:
        topo = f.read(6000)
    return {k: json.loads(v) if v.startswith('"') else v
            for k, v in re.findall(r"^(\w+): (.*)$", topo.split("\n---\n", 1)[0], re.M)}


def coletar(registrar, tempo_max=0):
    prazo = time.monotonic() + tempo_max * 60 if tempo_max else None
    estado = carregar_estado()
    try:
        # 1) novidades: ano corrente e anterior, sempre
        for tipo in TIPOS:
            for ano in (HOJE.year, HOJE.year - 1):
                for item in listar(tipo, ano, prazo):
                    if not os.path.exists(caminho_md(item)):
                        if prazo and time.monotonic() > prazo:
                            raise TempoEsgotado()
                        gravar(item, registrar)
        # 2) revalidação em rodízio do que já existe
        arquivos = sorted(glob.glob(os.path.join(PASTA, "*", "*", "*-cmbh.md")))
        hoje = [a for i, a in enumerate(arquivos) if i % CICLO == HOJE.toordinal() % CICLO]
        for md in hoje:
            if prazo and time.monotonic() > prazo:
                raise TempoEsgotado()
            cab = cabecalho(md)
            servico = "https://cmbhsilint.cmbh.mg.gov.br/silinternet/servico/norma?id="
            ident = cab.get("id_norma")
            if not ident:
                continue
            docs, _, _ = detalhar(servico + ident)
            principal = documento_principal(docs)
            if principal and id_doc(principal[1]) != cab.get("id_documento"):
                item = {"id": ident, "caminho": servico + ident, "tipo": cab.get("tipo_na_fonte", ""),
                        "numero": cab.get("numero", ""), "ano": os.path.basename(os.path.dirname(md)),
                        "ementa": cab.get("ementa", "")}
                gravar(item, registrar, motivo="novo texto consolidado publicado pela CMBH")
        # 3) carga inicial: do ano mais recente ao mais antigo
        for ano in range(HOJE.year - 2, ANO_INICIAL - 1, -1):
            for tipo in TIPOS:
                chave = f"{tipo}/{ano}"
                if estado["concluidos"].get(chave):
                    continue
                for item in listar(tipo, ano, prazo):
                    if os.path.exists(caminho_md(item)):
                        continue
                    if prazo and time.monotonic() > prazo:
                        raise TempoEsgotado()
                    gravar(item, registrar)
                estado["concluidos"][chave] = HOJE.isoformat()
                salvar_estado(estado)
    except TempoEsgotado:
        print("[bh] tempo esgotado; a coleta continua na próxima execução", flush=True)
    finally:
        salvar_estado(estado)
