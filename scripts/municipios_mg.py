"""Legislação dos municípios mineiros: descoberta das fontes e coleta pela API do SAPL.

Quais municípios: todos os de Minas Gerais com população acima de POP_MINIMA (IBGE), mais os que
já estavam no acervo. A lista, com a fonte encontrada para cada um, fica em
`scripts/municipios_mg.json` — versionada, para que se possa conferir e corrigir à mão (por
exemplo, indicar o endereço do SAPL de uma câmara que use domínio próprio).

Descoberta (refeita a cada DIAS_DESCOBERTA dias):
  1. população pelo SIDRA/IBGE (Censo 2022; na falta, a estimativa mais recente);
  2. para cada município, testa se a câmara tem SAPL em `sapl.<nome>.mg.leg.br` (padrão Interlegis).

Coleta, por câmara com SAPL:
  - carga inicial: percorre o índice completo e baixa tudo o que falta, das normas mais recentes
    para as mais antigas, dentro do tempo disponível; retoma no dia seguinte até completar;
  - depois de completa: só o ano corrente e o anterior.
  - PDFs escaneados passam por OCR (Tesseract, português) quando têm até OCR_MAX_PAGINAS páginas.

    python3 -I scripts/municipios_mg.py descobrir          # só refaz a lista e mostra a cobertura
    python3 -I scripts/municipios_mg.py coletar [--tempo-max MIN]
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
import comum  # noqa: E402

RAIZ = os.path.abspath(os.path.join(AQUI, ".."))
RAIZ_MUN = os.path.join(RAIZ, "municipais", "MG")
CONFIG = os.path.join(AQUI, "municipios_mg.json")
IBGE_MG = os.path.join(AQUI, "ibge_municipios_mg.json")
COBERTURA = os.path.join(RAIZ_MUN, "COBERTURA.md")
POP_MINIMA = 30000
DIAS_DESCOBERTA = 30
OCR_MAX_PAGINAS = 15
SIDRA = [  # Censo 2022 (tabela 4709) e, se falhar, estimativa mais recente (tabela 6579)
    "https://apisidra.ibge.gov.br/values/t/4709/n6/in%20n3%2031/v/93/p/2022",
    "https://apisidra.ibge.gov.br/values/t/6579/n6/in%20n3%2031/v/9324/p/last%201",
]
AVISO_OCR = "> Texto obtido por OCR a partir de PDF escaneado; pode conter erros de reconhecimento.\n\n"

HOJE = dt.date.today()
_redes, _redes_lock = {}, threading.Lock()


class TempoEsgotado(Exception):
    pass


def rede(host):
    """Um limitador por câmara: as câmaras são servidores diferentes e podem ser consultadas em paralelo."""
    with _redes_lock:
        if host not in _redes:
            _redes[host] = comum.Rede(rps=1.5)
        return _redes[host]


def pasta_municipio(nome):
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def host_padrao(nome):
    return f"sapl.{pasta_municipio(nome).replace('-', '')}.mg.leg.br"


# ---------------------------------------------------------------- descoberta

def populacoes():
    for url in SIDRA:
        try:
            st, dados, _ = comum.Rede(rps=1).get(url, "application/json", tentativas=3, timeout=180)
        except RuntimeError as e:
            print(f"[descoberta] SIDRA indisponível: {e}", flush=True)
            continue
        if st != 200:
            continue
        pops = {}
        for linha in json.loads(dados)[1:]:
            codigo = next((v for v in linha.values() if re.fullmatch(r"31\d{5}", str(v))), None)
            valor = str(linha.get("V") or "")
            if codigo and valor.isdigit():
                pops[codigo] = int(valor)
        if len(pops) > 800:
            print(f"[descoberta] população de {len(pops)} municípios ({url.split('/t/')[1].split('/')[0]})", flush=True)
            return pops
    return {}


def tem_sapl(host):
    try:
        st, dados, _ = rede(host).get(f"https://{host}/api/norma/normajuridica/?page_size=1",
                                      "application/json", tentativas=2, timeout=30)
    except RuntimeError:
        return None
    if st != 200:
        return None
    try:
        d = json.loads(dados)
    except json.JSONDecodeError:
        return None
    return (d.get("pagination") or {}).get("total_entries") if "results" in d else None


def carregar_config():
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    return {"atualizado_em": "", "pop_minima": POP_MINIMA, "municipios": {}}


def salvar_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def ja_no_acervo():
    """Municípios que já têm normas no acervo, com o host SAPL que aparece nos arquivos."""
    achados = {}
    for pasta in sorted(os.listdir(RAIZ_MUN)) if os.path.isdir(RAIZ_MUN) else []:
        base = os.path.join(RAIZ_MUN, pasta)
        if not os.path.isdir(base):
            continue
        for raiz, _, arquivos in os.walk(base):
            for a in arquivos:
                if not a.endswith(".md"):
                    continue
                with open(os.path.join(raiz, a), encoding="utf-8") as f:
                    cab = f.read(4000)
                ibge = re.search(r'^codigo_ibge: "?(\d{7})', cab, re.M)
                host = re.search(r'^fonte: "https?://(sapl\.[^/"]+)/', cab, re.M)
                if ibge:
                    atual = achados.setdefault(ibge[1], {"pasta": pasta, "host": None})
                    if host and not atual["host"]:
                        atual["host"] = host[1]
    return achados


def descobrir(forcar=False):
    cfg = carregar_config()
    if not forcar and cfg.get("atualizado_em"):
        idade = (HOJE - dt.date.fromisoformat(cfg["atualizado_em"])).days
        if idade < DIAS_DESCOBERTA:
            return cfg
    with open(IBGE_MG, encoding="utf-8") as f:
        nomes = {m["codigo_ibge"]: m["nome"] for m in json.load(f)}
    pops = populacoes()
    existentes = ja_no_acervo()
    alvo = {c for c, p in pops.items() if p > cfg.get("pop_minima", POP_MINIMA)} | set(existentes)
    if not pops and cfg["municipios"]:
        print("[descoberta] sem dados de população; mantendo a lista anterior", flush=True)
        return cfg
    municipios = cfg["municipios"]
    candidatos = []
    for codigo in sorted(alvo):
        nome = nomes.get(codigo, codigo)
        m = municipios.setdefault(codigo, {"nome": nome, "pasta": pasta_municipio(nome), "sapl": None,
                                           "sapl_manual": False, "carga_completa": False})
        m["nome"] = nome
        if codigo in pops:
            m["populacao"] = pops[codigo]
        if codigo in existentes:
            m["pasta"] = existentes[codigo]["pasta"]
            m["sapl"] = m.get("sapl") or existentes[codigo]["host"]
        if not m.get("sapl_manual"):
            candidatos.append(codigo)

    def testar(codigo):
        m = municipios[codigo]
        hosts = [h for h in dict.fromkeys([m.get("sapl"), host_padrao(m["nome"])]) if h]
        for h in hosts:
            total = tem_sapl(h)
            if total is not None:
                return codigo, h, total
        return codigo, None, None

    with ThreadPoolExecutor(8) as ex:
        for codigo, host, total in ex.map(testar, candidatos):
            m = municipios[codigo]
            if host and host != m.get("sapl"):
                m["carga_completa"] = False
            if host or not m.get("sapl"):
                m["sapl"] = host
            m["normas_na_fonte"] = total
    cfg["atualizado_em"] = HOJE.isoformat()
    salvar_config(cfg)
    escrever_cobertura(cfg)
    com = sum(1 for m in municipios.values() if m.get("sapl"))
    print(f"[descoberta] {len(municipios)} municípios; {com} com SAPL", flush=True)
    return cfg


def contar_normas(pasta):
    base = os.path.join(RAIZ_MUN, pasta)
    return sum(a.endswith(".md") for _, _, arqs in os.walk(base) for a in arqs) if os.path.isdir(base) else 0


def escrever_cobertura(cfg):
    linhas = ["# Cobertura municipal — Minas Gerais", "",
              f"Municípios com mais de {cfg.get('pop_minima', POP_MINIMA):,} habitantes (IBGE) e os que já "
              "estavam no acervo. Gerado por `scripts/municipios_mg.py`; a lista editável é "
              "`scripts/municipios_mg.json`.".replace(",", "."), "",
              "| Município | População | Fonte automática | Normas no acervo | Carga inicial |",
              "| --- | ---: | --- | ---: | :---: |"]
    ordem = sorted(cfg["municipios"].items(), key=lambda kv: -(kv[1].get("populacao") or 0))
    for codigo, m in ordem:
        pop = f"{m['populacao']:,}".replace(",", ".") if m.get("populacao") else "—"
        fonte = f"SAPL ([{m['sapl']}](https://{m['sapl']}))" if m.get("sapl") else "**sem fonte automática**"
        carga = "—" if not m.get("sapl") else ("completa" if m.get("carga_completa") else "em andamento")
        linhas.append(f"| {m['nome']} | {pop} | {fonte} | {contar_normas(m['pasta'])} | {carga} |")
    sem = [m["nome"] for m in cfg["municipios"].values() if not m.get("sapl")]
    linhas += ["", f"**{len(sem)} municípios sem fonte automática.** A câmara ou a prefeitura não usa o SAPL "
               "no endereço padrão; cada um precisa de um coletor próprio ou do endereço correto em "
               "`scripts/municipios_mg.json` (campo `sapl`, com `sapl_manual: true`).", ""]
    os.makedirs(RAIZ_MUN, exist_ok=True)
    with open(COBERTURA, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas))


# ---------------------------------------------------------------- coleta

def api(host, caminho):
    st, dados, _ = rede(host).get(f"https://{host}/api/{caminho}", "application/json", tentativas=3, timeout=90)
    if st != 200 or not dados:
        return None
    try:
        return json.loads(dados)
    except json.JSONDecodeError:
        return None


def listar(host, anos=None, prazo=None):
    tipos = {}
    pag = 1
    while pag <= 20:
        d = api(host, f"norma/tiponormajuridica/?page_size=100&page={pag}")
        if not d:
            break
        for t in d.get("results", []):
            tipos[t["id"]] = t.get("descricao") or t.get("__str__") or "Norma"
        if not (d.get("pagination") or {}).get("next_page"):
            break
        pag += 1
    normas = []
    for filtro in ([f"ano={a}&" for a in anos] if anos else [""]):
        pag = 1
        while pag <= 1000:
            if prazo and time.monotonic() > prazo:
                raise TempoEsgotado()
            d = api(host, f"norma/normajuridica/?{filtro}page_size=100&page={pag}")
            if not d:
                break
            normas += d.get("results", [])
            if not (d.get("pagination") or {}).get("next_page"):
                break
            pag += 1
    if anos:
        normas = [n for n in normas if str(n.get("ano")) in {str(a) for a in anos}]
    return tipos, normas


def ocr(bruto):
    """OCR de PDF escaneado com Tesseract (português). Vazio se a ferramenta não existir ou o PDF for grande."""
    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "a.pdf")
        with open(pdf, "wb") as f:
            f.write(bruto)
        info = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True, timeout=60).stdout
        paginas = int((re.search(r"Pages:\s+(\d+)", info) or [0, 0])[1])
        if not 0 < paginas <= OCR_MAX_PAGINAS:
            return ""
        subprocess.run(["pdftoppm", "-r", "300", "-gray", "-png", pdf, os.path.join(tmp, "p")],
                       capture_output=True, timeout=600)
        textos = []
        for img in sorted(x for x in os.listdir(tmp) if x.endswith(".png")):
            r = subprocess.run(["tesseract", os.path.join(tmp, img), "stdout", "-l", "por", "--psm", "3"],
                               capture_output=True, timeout=300, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
            textos.append(r.stdout.decode("utf-8", "replace"))
        return "\f".join(textos)


def converter(bruto, url, ctype):
    try:
        corpo, motivo = comum.documento_para_md(bruto, url, ctype)
    except Exception as e:  # noqa: BLE001 — ex.: .doc sem conversor no Linux
        return None, f"conversão falhou: {e}"[:200]
    if corpo or not bruto.startswith(b"%PDF"):
        return corpo, motivo
    texto = ocr(bruto)
    if len(re.sub(r"\W", "", texto)) >= 200:
        return AVISO_OCR + comum.texto_pdf_para_md(texto), None
    return None, motivo


def gravar(host, m, codigo, tipo_fonte, n, registrar):
    url = (n.get("texto_integral") or "").replace("http://", "https://")
    if not url:
        return "sem_texto"
    st, bruto, ctype = rede(host).get(url, tentativas=2, timeout=180)
    if st != 200 or not bruto:
        registrar("falhas", f"municipais/MG/{m['pasta']}", f"SAPL {host} norma {n['id']}: HTTP {st}")
        return "sem_texto"
    corpo, motivo = converter(bruto, url, ctype)
    if not corpo:
        registrar("falhas", f"municipais/MG/{m['pasta']}", f"SAPL {host} norma {n['id']}: {motivo}")
        return "sem_texto"
    with _redes_lock:  # canonico() avisa por print; serializa para não embaralhar o log
        pasta, tipo = comum.canonico(tipo_fonte)
    numero = str(n.get("numero") or "").strip()
    data = n.get("data") or ""
    try:
        data_br = dt.date.fromisoformat(data).strftime("%d/%m/%Y")
    except ValueError:
        data_br = ""
    titulo = f"{tipo} nº {numero}" + (f", de {data_br}" if data_br else f"/{n['ano']}")
    slug = f"{pasta}-{comum.slugify(numero.replace('.', '')) or 'sn'}-{n['ano']}-sapl-{n['id']}"
    md = os.path.join(RAIZ_MUN, m["pasta"], pasta, str(n["ano"]), slug + ".md")
    meta = [("titulo", titulo), ("tipo", tipo), ("tipo_na_fonte", tipo_fonte), ("numero", numero), ("data", data),
            ("ementa", " ".join((n.get("ementa") or "").split())), ("municipio", m["nome"]), ("uf", "MG"),
            ("codigo_ibge", codigo), ("versao_texto", "texto integral anexado no SAPL municipal"),
            ("fonte", f"https://{host}/norma/{n['id']}"), ("fonte_texto", url),
            ("sha256_fonte", hashlib.sha256(bruto).hexdigest())]
    linhas = ["---"] + [f"{k}: {comum.yaml_str(v)}" for k, v in meta]
    linhas += [f"baixado_em: {comum.yaml_str(HOJE.isoformat())}", "---", "", f"# {titulo}", "", ""]
    os.makedirs(os.path.dirname(md), exist_ok=True)
    with open(md + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + corpo.strip() + "\n")
    os.replace(md + ".tmp", md)
    registrar("novas", md, f"{m['nome']}: " + (n.get("ementa") or "")[:150])
    return "ok"


def ids_existentes():
    ids = set()
    for raiz, _, arquivos in os.walk(RAIZ_MUN):
        for a in arquivos:
            r = re.search(r"-sapl-(\d+)\.md$", a)
            if r:
                ids.add((os.path.relpath(raiz, RAIZ_MUN).split(os.sep)[0], r[1]))
    return ids


def coletar(registrar, tempo_max=0, workers=8):
    """Coleta todas as câmaras com SAPL. Retorna quando termina ou quando o tempo acaba."""
    prazo = time.monotonic() + tempo_max * 60 if tempo_max else None
    cfg = descobrir()
    existentes = ids_existentes()
    camaras = [(c, m) for c, m in cfg["municipios"].items() if m.get("sapl")]
    # carga inicial das maiores cidades primeiro
    camaras.sort(key=lambda cm: (cm[1].get("carga_completa", False), -(cm[1].get("populacao") or 0)))
    print(f"[municipios] {len(camaras)} câmaras com SAPL; "
          f"{sum(not m.get('carga_completa') for _, m in camaras)} em carga inicial", flush=True)

    def camara(item):
        codigo, m = item
        host = m["sapl"]
        completa = m.get("carga_completa", False)
        try:
            tipos, normas = listar(host, None if not completa else (HOJE.year - 1, HOJE.year), prazo)
        except TempoEsgotado:
            return m["nome"], 0, False
        except RuntimeError as e:
            registrar("falhas", f"municipais/MG/{m['pasta']}", f"SAPL {host}: {e}"[:200])
            return m["nome"], 0, completa
        faltam = [n for n in normas if (m["pasta"], str(n["id"])) not in existentes]
        faltam.sort(key=lambda n: (n.get("data") or str(n.get("ano") or ""), n["id"]), reverse=True)
        novas = 0
        for n in faltam:
            if prazo and time.monotonic() > prazo:
                return m["nome"], novas, False
            try:
                if gravar(host, m, codigo, tipos.get(n.get("tipo"), "Norma"), n, registrar) == "ok":
                    novas += 1
            except RuntimeError as e:  # rede caiu no meio
                registrar("falhas", f"municipais/MG/{m['pasta']}", f"SAPL {host} norma {n['id']}: {e}"[:200])
        return m["nome"], novas, True

    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(camara, cm): cm for cm in camaras}
        for fut in as_completed(futs):
            codigo, m = futs[fut]
            try:
                nome, novas, terminou = fut.result()
            except Exception as e:  # noqa: BLE001
                registrar("falhas", f"municipais/MG/{m['pasta']}", str(e)[:200])
                continue
            if terminou and not m.get("carga_completa"):
                m["carga_completa"] = True
                print(f"[municipios] {nome}: carga inicial completa", flush=True)
            if novas:
                print(f"[municipios] {nome}: {novas} normas novas", flush=True)
    salvar_config(cfg)
    escrever_cobertura(cfg)
    if prazo and time.monotonic() > prazo:
        print("[municipios] tempo da etapa municipal esgotado; a carga continua na próxima execução", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("acao", choices=["descobrir", "coletar"])
    ap.add_argument("--tempo-max", type=float, default=0)
    a = ap.parse_args()
    if a.acao == "descobrir":
        descobrir(forcar=True)
        return 0

    def registrar(chave, caminho, extra=""):
        print(f"[{chave}] {caminho} {extra}", flush=True)
    coletar(registrar, a.tempo_max)
    return 0


if __name__ == "__main__":
    sys.exit(main())
