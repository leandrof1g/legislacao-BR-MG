"""Baixa páginas de amostra (e um nível de links de legislação) para estudar o formato de cada sistema.

Uso no GitHub Actions (workflow "Amostras"): lê ferramentas/amostras.txt e grava em amostras/.
"""
import hashlib
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
PADRAO = re.compile(r"legisla|leis?_|/leis?\b|/lei/|norma|decreto|ato|jflegis|pesquisar|consulta|detalhe|visualizar|arquivo|\.pdf", re.I)
SAIDA = sys.argv[2] if len(sys.argv) > 2 else "amostras"


def baixar(url):
    dados = None
    if url.startswith("POST "):  # "POST <url> <corpo urlencoded>"
        _, url, corpo = url.split(" ", 2)
        dados = corpo.encode()
    req = urllib.request.Request(url, data=dados, headers={"User-Agent": UA, "Accept": "*/*", **(
        {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"} if dados else {})})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.geturl(), r.headers.get("Content-Type", ""), r.read(800_000)
    except urllib.error.HTTPError as e:
        return e.code, url, "", e.read(200_000) if e.fp else b""
    except Exception as e:  # noqa: BLE001
        return f"erro {type(e).__name__}: {e}"[:200], url, "", b""


def nome(url):
    p = urllib.parse.urlparse(url.split(" ")[1] if url.startswith("POST ") else url)
    base = re.sub(r"[^a-zA-Z0-9]+", "_", p.netloc + p.path)[:80]
    return f"{base}_{hashlib.sha1(url.encode()).hexdigest()[:8]}"


def gravar(url, profundidade, indice):
    st, final, ctype, corpo = baixar(url)
    ext = ".pdf" if corpo[:4] == b"%PDF" else (".json" if "json" in ctype else ".html")
    arq = nome(url) + ext
    if corpo and ext != ".pdf":
        with open(os.path.join(SAIDA, arq), "wb") as f:
            f.write(corpo)
    indice.append(f"{profundidade}\t{st}\t{ctype}\t{len(corpo)}\t{url}\t{final}\t{arq if corpo and ext != '.pdf' else '-'}")
    links = []
    if profundidade == 0 and ext == ".html":
        html = corpo.decode("utf-8", "replace")
        for href in re.findall(r'href=["\']([^"\'#]+)["\']', html, re.I):
            u = urllib.parse.urljoin(final, href.replace("&amp;", "&"))
            if u.startswith("http") and PADRAO.search(u) and u not in links:
                links.append(u)
    return links[:10]


def main():
    os.makedirs(SAIDA, exist_ok=True)
    urls = [l.strip() for l in open(sys.argv[1], encoding="utf-8") if l.strip() and not l.startswith("#")]
    indice = []
    with ThreadPoolExecutor(12) as ex:
        filhos = sum(ex.map(lambda u: gravar(u, 0, indice), urls), [])
        list(ex.map(lambda u: gravar(u, 1, indice), dict.fromkeys(filhos)))
    with open(os.path.join(SAIDA, "_indice.tsv"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(indice)) + "\n")


if __name__ == "__main__":
    main()
