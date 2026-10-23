# Legislação federal e de Minas Gerais em Markdown

Leis, decretos e demais normas **federais**, do **Estado de Minas Gerais** e de **municípios
mineiros**, em texto simples (Markdown), com cabeçalho estruturado e link para a fonte oficial
de cada norma — **atualizadas automaticamente todos os dias**.

> [!IMPORTANT]
> Os textos têm caráter informativo e **não substituem a publicação oficial** (DOU, Diário do
> Executivo de MG, diários municipais). São conversões automáticas. Antes de citar, confira a
> fonte indicada no campo `fonte` do cabeçalho.

## O que tem aqui

| Esfera | Pasta | Normas | Fonte oficial |
| --- | --- | ---: | --- |
| Constituição Federal | `constituicao.md` | 1 | Planalto |
| Federal | `federal/` | ~166 mil | Senado Federal / [normas.leg.br](https://normas.leg.br) |
| Minas Gerais | `estaduais/MG/` | ~85 mil | [ALMG — Dados Abertos](https://dadosabertos.almg.gov.br) |
| Municípios mineiros | `municipais/MG/<municipio>/` | em expansão | SAPL das câmaras municipais |

Federal: leis ordinárias e complementares, decretos-lei, decretos (numerados e não numerados),
medidas provisórias, emendas constitucionais, decretos legislativos e resoluções do Senado, da
Câmara e do Congresso.

Minas Gerais: Constituição Estadual, emendas, leis ordinárias, complementares e delegadas,
decretos, resoluções, deliberações e portarias publicadas na base da ALMG.

Municípios: todos os de Minas Gerais com **mais de 30 mil habitantes** (IBGE), mais os 95 que vieram
do acervo original. A lista e a situação de cada um — se tem fonte automática, quantas normas já
foram coletadas, se a carga inicial terminou — estão em
[`municipais/MG/COBERTURA.md`](municipais/MG/COBERTURA.md). A coleta automática usa o SAPL
(sistema do Interlegis usado por boa parte das câmaras); municípios cuja câmara não usa o SAPL
aparecem como “sem fonte automática” e precisam de coletor próprio.

**Não estão aqui:** normas técnicas e atos internos de órgãos (por exemplo, as Instruções Técnicas
do CBMMG).

## Como os arquivos são organizados

```
federal/<tipo>/<ano>/<tipo>-<numero>-<data>.md        ex.: federal/leis-ordinarias/2017/lei-13425-2017-03-30.md
estaduais/MG/<tipo>/<ano>/<tipo>-<numero>-<ano>.md    ex.: estaduais/MG/leis-ordinarias/2001/lei-14130-2001.md
municipais/MG/<municipio>/<tipo>/<ano>/...md
atualizacoes/<ano>/<data>.md                          o que mudou em cada atualização diária
```

Cada arquivo começa com um cabeçalho YAML:

```yaml
---
titulo: "Lei nº 14130, de 19/12/2001"
uf: "MG"
tipo: "Lei Ordinária"
numero: "14130"
data: 2001-12-19
ementa: "DISPÕE SOBRE A PREVENÇÃO CONTRA INCÊNDIO E PÂNICO NO ESTADO E DÁ OUTRAS PROVIDÊNCIAS."
situacao: "Vigente"
revogada: false
versao_texto: "atualizada"
fonte: "https://dadosabertos.almg.gov.br/api/v2/legislacao/mineira/LEI/14130/2001/documento?tipoDoc=572"
baixado_em: 2026-10-13
---
```

## Atualização automática

O workflow [`.github/workflows/atualizar.yml`](.github/workflows/atualizar.yml) roda todo dia às
03h17 (horário de Brasília) e executa [`scripts/atualizar.py`](scripts/atualizar.py), que:

1. **Baixa as normas novas** — consulta os índices oficiais do ano corrente e do anterior
   (Senado, ALMG e SAPL de cada câmara) e baixa o que ainda não está no acervo.
2. **Atualiza as normas alteradas** — quando a ementa de uma norma nova diz que ela altera outra
   (“Altera a Lei nº 14.130, de 19 de dezembro de 2001…”), a norma alterada é baixada de novo, já
   com o texto compilado.
3. **Revalida o acervo em rodízio** — a cada dia, 1/30 das normas com texto compilado (federais e
   mineiras) é conferido na fonte; em 30 dias o acervo inteiro é revisto.
4. **Municípios** — a cada 30 dias refaz a lista de municípios acima de 30 mil habitantes e
   procura o SAPL de cada câmara. Para câmara nova, faz a carga inicial (todo o histórico, das
   normas mais recentes para as mais antigas, até 2 h por dia, retomando no dia seguinte); depois,
   só as normas novas. PDFs escaneados passam por OCR.
5. **Atualiza a situação das normas mineiras** — o índice da ALMG informa a situação (vigente,
   revogada…) de todas as normas; mudanças entram no cabeçalho.

Um arquivo só é regravado se o texto mudou de fato. O resumo de cada dia fica em
[`atualizacoes/`](atualizacoes/) e no histórico de commits — `git log -p` sobre um arquivo mostra
a evolução do texto de uma norma.

Para rodar manualmente: aba **Actions → Atualizar legislação → Run workflow**, ou localmente:

```bash
python3 -I scripts/atualizar.py                         # tudo
python3 -I scripts/atualizar.py --esferas mg --sem-revalidacao
```

Requer Python 3.12+, `poppler-utils` (pdftotext) e, para OCR, `tesseract-ocr` com o idioma português.

## Como baixar só uma parte

O repositório é grande (~250 mil arquivos). Para baixar só o que interessa:

```bash
git clone --filter=blob:none --sparse https://github.com/geocbmmg/legislacao
cd legislacao
git sparse-checkout set estaduais/MG          # ou federal, municipais/MG ...
```

## Origem

O acervo inicial (outubro de 2026) e os coletores federal e mineiro vêm do projeto
[ctresb/leis](https://github.com/ctresb/leis), de João Davi, sob licença MIT. Daqui em diante, a
atualização é feita por este repositório diretamente nas fontes oficiais.

## Licença

Código sob licença MIT (ver [LICENSE](LICENSE)). Os textos normativos são atos oficiais e não são
protegidos por direito autoral (Lei nº 9.610/98, art. 8º, IV).
