# Dev Log — CineData Analytics Agent

Registro cronológico de cada passo implementado: **o que** foi feito, **por que** foi feito assim e o **status**.
Legenda de status: ✅ Concluído e testado · 🟡 Concluído, sem teste automatizado · ⏳ Pendente

---

## Passo 0 — Diagnóstico do banco e da arquitetura (2026-10-02)

**Status:** ✅ Concluído

**O que foi feito**
- Inspeção do `data/cinerocket.db` (schema real, volumes, nulidade e distribuição das colunas-chave).
- Proposta de arquitetura: orquestração nativa (SDK `openai` → OpenRouter), até 2 req/pergunta, cache por hash, guardrails em 4 camadas.

**Justificativa**
- O schema real diverge do modelo descrito originalmente: colunas em português, chaves surrogate `sk_*`, `bridge_movie_person` sem `job`/`character`, `idioma_original` 100% NULL. Um DDL de prompt baseado no documento original geraria SQL inválido e queimaria a quota de 50 req/dia em retries.
- LangChain/LangGraph descartados: loops de tools consomem de 3 a 5 chamadas por pergunta de forma pouco controlável. Com 50 req/dia, o custo por pergunta precisa ser explícito.

---

## Passo 1 — Higiene do repositório (2026-10-02)

**Status:** ✅ Concluído (verificado com `git check-ignore`)

**O que foi feito**
| Arquivo | Alteração |
|---|---|
| `.gitignore` | Ignora `data/*.db` (+ `-journal`, `-wal`, `-shm`), `.cache/`, `CLAUDE.md`/`Claude.md` |
| `Claude.md` (local, não versionado) | Modelo dimensional e regras de negócio reescritos com o schema real; stack, guardrails e estrutura atualizados |

**Justificativa**
- O banco tem 581 MB, acima do limite de 100 MB por arquivo do GitHub: o push seria rejeitado.
- `Claude.md` é instrução local e, por política do projeto, não vai para o repositório público.

---

## Passo 2 — Conexão read-only e guardrails SQL (2026-10-02)

**Status:** ✅ Concluído e testado

**O que foi feito**
| Arquivo | Conteúdo |
|---|---|
| `src/config.py` | `Settings` (pydantic-settings, lê `.env`): `openrouter_api_key`, `db_path`, `query_timeout_s`, `max_rows`; `get_settings()` com cache |
| `src/database/connection.py` | `ReadOnlyDatabase.execute()` → `QueryResult(columns, rows, truncated, elapsed_ms)`; exceções `DatabaseError` / `QueryTimeoutError` |
| `src/agent/guardrails.py` | `validate_sql(sql, max_rows)` → SQL validado (+ `LIMIT` quando ausente); exceção `GuardrailViolation` |
| `tests/test_guardrails.py`, `tests/test_connection.py` | Testes offline (banco temporário), mais 1 teste opcional com o banco real |
| `requirements*.txt`, `pyproject.toml`, `.flake8`, `.env.example` | Dependências, Black (88 col.), pytest (`pythonpath`, marker `real_db`), Flake8 |

**Justificativa**
- **Defesa em camadas:** o regex exigido pelo case é frágil sozinho, por isso:
  1. O regex roda sobre o SQL **sem strings e comentários**, para não bloquear `'DELETE'` dentro de um texto e não deixar passar escrita escondida em comentário.
  2. `REPLACE(...)` (função de string) é permitido; `REPLACE INTO` é bloqueado.
  3. `;` fora de strings e comentários de bloco não fechados são rejeitados, impedindo encadeamento de statements.
  4. O `sqlglot` garante exatamente 1 statement do tipo `Query` e nenhum nó de escrita na árvore.
  5. A **garantia real** fica no engine: `mode=ro` + `PRAGMA query_only` + `set_authorizer` que só permite `SELECT`/`READ`/`FUNCTION`/`RECURSIVE` (e nega `load_extension`). Os testes de conexão executam escrita **sem** os guardrails textuais e confirmam que o engine recusa e que os dados não mudam.
- **SQL auditado = SQL executado:** o texto não é re-renderizado pelo sqlglot. Só se acrescenta `LIMIT` (em nova linha, para não ser anulado por um comentário `--` final).
- **Timeout** via `set_progress_handler` e **teto de linhas** via `fetchmany(max_rows + 1)`, que detecta truncamento sem materializar tudo.

**Resultado:** 53 testes passando; Black e Flake8 limpos.

---

## Decisões de negócio acordadas (2026-10-02)

| # | Decisão | Origem |
|---|---|---|
| D1 | Lucro e margem exigem `receita > 0 AND orcamento > 0`, mesmo quando a pergunta diz só "receita informada". A premissa é explicitada no resumo executivo. | Usuário confirmou |
| D2 | "Últimos N anos" é relativo ao `MAX(ano_lancamento)` dos filmes com `status_filme = 'Lançado'` (hoje 2026 → últimos 5 anos = 2022–2026), não à data atual. | Usuário escolheu |
| D3 | "Nota" sem qualificação = `nota_imdb`. Moeda padrão = USD, BRL quando a pergunta citar R$/reais. | Proposta técnica |

---

## Passo 3 — Prompts, few-shots e SQL gabarito (2026-10-02)

**Status:** ✅ Concluído e testado

**O que foi feito**
| Arquivo | Conteúdo |
|---|---|
| `src/agent/prompts.py` | `SCHEMA` estruturado (dataclass `Column`) + `render_schema()`; `BUSINESS_RULES`; 5 `FEW_SHOTS`; `SQL_SYSTEM_PROMPT` (~7,6k chars ≈ 2k tokens); `build_sql_messages()` (com feedback de erro para retry); `build_summary_messages()` (até 50 linhas); `PROMPT_VERSION` |
| `tests/golden_queries.py` | 14 perguntas oficiais com SQL gabarito (`GoldenCase`) |
| `tests/test_prompts.py` | Schema do prompt == schema real (tabela a tabela, coluna a coluna); few-shots passam nos guardrails e não vazam perguntas oficiais; contrato das mensagens |
| `tests/test_queries.py` | Cada gabarito e cada few-shot passa pelos guardrails e roda no banco real, retornando dados, dentro do timeout |
| `src/database/connection.py` | PRAGMAs de performance (`mmap_size` 1 GB, `cache_size` 256 MB, `temp_store=MEMORY`), executados antes do authorizer |
| `src/config.py`, `.env.example` | Timeout padrão de 15s → 30s |

**Justificativa**
- **Schema como dado, não como texto livre:** um teste compara `SCHEMA` com `PRAGMA table_info`. Se o banco mudar, o teste quebra antes de o LLM gerar SQL inválido.
- **Saída JSON `{sql, premissas}`:** premissas (filtros aplicados, tradução de gênero) chegam ao resumo executivo de forma estruturada, como pede a D1. Pedidos de escrita ou fora do escopo retornam `{"sql": null, "erro": ...}` sem gastar a execução.
- **Few-shots ≠ perguntas oficiais:** eles ensinam padrões (tradução de gênero, filtro de lucro, ano de referência D2, mínimo de votos, self-join otimizado) para que a suíte meça generalização, não memorização. Um teste garante que não há vazamento.
- **Performance — dupla ator–diretor:** o self-join ingênuo em `bridge_movie_person` gera 7,8 milhões de pares e levou **105s**. Benchmarks (processo novo a cada medição):
  | Variante | Tempo |
  |---|---|
  | Join ingênuo, sem PRAGMAs | ~105s |
  | Join ingênuo + PRAGMAs de cache/mmap | ~16s |
  | CTEs `MATERIALIZED` partindo de `dim_people` filtrado por tipo | ~4–5s |

  Como o padrão otimizado é o que mais pesa, ele virou regra de negócio no prompt e few-shot (dupla roteirista–diretor). Os PRAGMAs ficam como rede de segurança, e o timeout foi para 30s para que um SQL subótimo não queime um retry de LLM.

**Resultado:** 84 testes passando (~11s, incluindo o banco real); Black e Flake8 limpos.

**Achados de qualidade de dados (afetam a leitura dos resultados)**
| Achado | Impacto |
|---|---|
| Orçamentos irrisórios: 63 filmes com receita e orçamento > 0 têm `orcamento_usd < 1.000` (ex.: US$ 4, US$ 128) | Distorcem "maior margem" (top 1 = 133.830×) e "gênero com maior margem média" (Family = 946× puxado por outliers) |
| Notas com 1 voto: divergências TMDB×IMDb e usuários×IMDb dominadas por filmes com `qtd = 1` | Rankings de divergência sem significância |
| Títulos duplicados: 1.518 pares (título, ano) repetidos com `id_filme` distintos (ex.: ~40 cadastros de "Die Hart 2: Die Harter") | "Filmes mais avaliados" mostra o mesmo título várias vezes |
| `popularidade` igual ao ano de lançamento em 4 filmes (ex.: "La Fellinette" = 2020.0) | 2 deles aparecem no top 5 de popularidade |
| Cobertura temporal: 2024 tem 1.050 lançados; 2025–2026 têm só 3 | "Últimos 5 anos" (D2) cobre efetivamente 2022–2024 |

Os gabaritos seguem **literalmente** o enunciado e as decisões D1–D3. Os achados acima geram as decisões pendentes P1–P3.

### Decisões de qualidade de dados
P1–P3 foram aprovadas pelo usuário e aplicadas em seguida (ver "Passo 3.1").

---

## Revisão contra os requisitos oficiais (`docs/requisitos.md`) — 2026-10-02

**Status:** ✅ Concluído. Nenhuma divergência exigiu mudança de código.

**Matriz de rastreabilidade**
| Requisito oficial | Onde é atendido | Status |
|---|---|---|
| Text-to-SQL somente-leitura sobre a camada Gold | `guardrails.py` + `connection.py` | ✅ |
| Framework de agentes à escolha | Orquestração nativa (ver Passo 0) | ⏳ Passo 4 |
| Modelo `:free` via OpenRouter (sugestão: com tool calling) | `llm.py` | ⏳ Passo 4 |
| Linguagem Python; entregável Projeto Python ou FastAPI | `src/` (CLI + FastAPI opcional) | 🟡 Parcial |
| Planejar a quota de 50 req/dia | Schema no prompt, até 2 req/pergunta, cache | 🟡 Prompt pronto; cache no Passo 4 |
| "Receita ≈ Faturamento ≈ Bilheteria" | `BUSINESS_RULES` em `prompts.py` | ✅ |
| 14 perguntas de exemplo (5 categorias) | `tests/golden_queries.py`: textos idênticos ao enunciado | ✅ |
| Versionamento no GitHub + README passo a passo | `README.md` | ⏳ Pendente |
| Extras: guardrails, fallback de modelos, cache, avaliação com respostas esperadas | Guardrails ✅; gabaritos ✅; fallback/cache ⏳ Passo 4 | 🟡 |
| Extras opcionais: UI, gráficos, memória de conversa, busca semântica em sinopses | Fora do escopo até o MVP estar entregue | — |
| Prazo: 05/10/2026 às 18:00 | — | ⏳ |

**Pontos de atenção identificados**
- **"Lucro médio por gênero, considerando apenas filmes com receita informada":** a D1 é mais restritiva que o texto literal. Ela foi mantida porque, sem orçamento, o "lucro" seria a própria receita. A premissa aparece no resumo executivo, então o avaliador vê a interpretação.
- **"Nota média IMDb por ano":** o gabarito considera só `status_filme = 'Lançado'`, porque filmes não lançados com nota são ruído. Essa interpretação também é exibida como premissa.
- **Tool calling (sugerido, não obrigatório):** o desenho atual usa saída JSON `{sql, premissas}`, que funciona em qualquer modelo `:free`, inclusive nos sem suporte a tools, e custa 1 chamada. Se a avaliação valorizar tool calling explicitamente, o Passo 4 pode expor `execute_sql` como tool sem mudar prompts nem guardrails.
- **Local do banco:** o enunciado sugere o `.db` "na mesma pasta do código". O projeto usa `data/cinerocket.db`, configurável via `DB_PATH`, e isso precisa ser documentado no README.
- **`docs/requisitos.md`** traz "Copyright © 2026 Visagio. Todos os direitos reservados". A recomendação é não versioná-lo em repositório público (decisão do desenvolvedor).

---

## Passo 3.1 — Regras de qualidade de dados P1–P3 e compliance (2026-10-02)

**Status:** ✅ Concluído e testado

**Decisões aprovadas pelo usuário**
| # | Decisão |
|---|---|
| P1 | Margem de lucro exige também `orcamento_usd >= 10000` |
| P2 | Divergência de notas exige `qtd_tmdb >= 50`, `qtd_imdb >= 50`, `qtd_avaliacoes_usuarios >= 3` (conforme as fontes envolvidas) |
| P3 | Dado bruto não é filtrado; títulos duplicados e popularidade anômala são sinalizados no resumo executivo |
| D4 | Saída JSON `{sql, premissas}` mantida em vez de tool calling: 1 chamada previsível por pergunta |
| D5 | `docs/requisitos.md` (material com copyright da Visagio) fica fora do versionamento |
| D6 | Commits locais executados pelo assistente (Conventional Commits, sem co-autoria); `git push` só manual |

**O que foi feito**
| Arquivo | Alteração |
|---|---|
| `src/agent/prompts.py` | `BUSINESS_RULES` com P1/P2/P3; prompt de resumo instrui a sinalizar duplicatas, popularidade = ano, extremos e amostras pequenas; `PROMPT_VERSION` → `2026-10-02.2` (invalida o cache) |
| `tests/golden_queries.py` | Gabaritos `fin_maior_margem`, `gen_maior_margem_media`, `pop_divergencia_tmdb_imdb`, `rev_divergencia_usuarios_imdb` com os novos filtros |
| `.gitignore` | Inclui `docs/requisitos.md` |
| `Claude.md` (local) | Regras P1–P3 e nova política de Git |

**Efeito nos resultados**
| Pergunta | Antes | Depois |
|---|---|---|
| Maior margem | "Dad, I'm Sorry", 133.830× (orçamento de US$ 128) | "Secret Superstar", 479× |
| Gênero com maior margem média | Family, 946× (puxado por outliers) | Music, 15,6× |
| Divergência TMDB × IMDb | Filmes com 1 voto | Filmes com ≥ 50 votos em cada fonte |
| Divergência usuários × IMDb | Filmes com 1 avaliação | ≥ 3 avaliações e ≥ 50 votos IMDb |

**Commits:** os passos 1–3 foram commitados em 7 commits atômicos (`chore` → `feat(database)` → `feat(agent)` → `test` → `feat(agent)` → `test` → `docs`).

---

## Próximo: Passo 4 — Cliente LLM, cache e workflow ⏳
`llm.py` (fallback entre modelos `:free` + contador de quota), `cache.py` (hash da pergunta + `PROMPT_VERSION`), `workflow.py` (orçamento de até 2 req/pergunta + 1 retry), `formatter.py`.
