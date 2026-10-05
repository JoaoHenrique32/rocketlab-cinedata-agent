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

## Passo 4 — Cliente LLM, cache, workflow e CLI (2026-10-02)

**Status:** ✅ Concluído e testado offline (LLM fake). ⏳ Avaliação com o LLM real depende da `OPENROUTER_API_KEY` no `.env`.

**O que foi feito**
| Arquivo | Conteúdo |
|---|---|
| `src/config.py`, `.env.example` | Settings de LLM (`openrouter_base_url`, `llm_models` em ordem de fallback, `llm_timeout_s`, `llm_temperature=0`), quota (`daily_request_limit=50`, `quota_warning_at=40`) e `cache_path` |
| `src/agent/cache.py` | `AgentCache`: tabela `responses` (chave sha256 de pergunta normalizada + `PROMPT_VERSION`; guarda SQL, premissas e resumo) e tabela `quota` (requisições por dia UTC) |
| `src/agent/llm.py` | `LLMClient.complete()` com fallback ordenado; `openrouter_completion_fn()` (SDK `openai`, `max_retries=0`); `parse_json_object()` tolerante; `strip_reasoning()`; exceções `LLMError` / `QuotaExceededError` / `LLMResponseError` |
| `src/agent/formatter.py` | `AgentResponse`; `to_markdown()` (pergunta → resumo → premissas → tabela → SQL auditado → origem e nº de chamadas); números em pt-BR; `local_summary()` |
| `src/agent/workflow.py` | `CineDataAgent.ask(question, summarize, use_cache)` e `from_settings()` |
| `src/cli.py` | `python -m src.cli "pergunta"`, modo interativo, `--no-summary`, `--no-cache`, `--quota`, `--clear-cache` |
| `tests/fakes.py` | `ScriptedLLM` (roteiro de respostas/exceções), `MemoryQuota`, construtores de erros do SDK |
| `tests/test_llm.py`, `test_cache.py`, `test_workflow.py`, `test_formatter.py` | 50 testes offline |
| `tests/test_queries.py` | Avaliação opt-in (`RUN_LLM_EVAL=1`): o agente responde as 14 perguntas e o resultado é comparado ao do gabarito |

**Justificativa**
- **Modelos padrão:** os modelos citados no planejamento (`llama-3.3-70b-instruct:free`, `qwen-2.5-72b-instruct:free`) **não existem mais** no catálogo `:free` (consulta a `/api/v1/models` em 2026-10-02). A cadeia padrão passou a ser `gemma-4-31b-it` → `nemotron-3-super-120b-a12b` → `qwen3.8-27b`, configurável via `LLM_MODELS` sem mexer em código.
- **Orçamento de chamadas por pergunta:**
  | Cenário | Req |
  |---|---|
  | Cache hit com resumo | 0 (SQL reexecutado localmente) |
  | Caminho feliz | 2 (SQL + resumo) |
  | `--no-summary` | 1 |
  | SQL inválido | +1 retry, com o erro do SQLite/guardrail devolvido ao modelo |
- **Fallback:** 429, timeout, conexão, 5xx, 404 (modelo removido), 400 e resposta vazia passam para o próximo modelo. 401/402/403 abortam, porque os outros modelos falhariam igual e só gastariam quota. 429 com "per-day" (limite diário do OpenRouter) também aborta, com `QuotaExceededError`.
- **Contagem conservadora de quota:** cada tentativa é registrada *antes* do envio, e o limite local é checado antes de cada tentativa, inclusive no meio do fallback.
- **Sem `response_format`:** só parte dos modelos `:free` o suporta. O parser aceita cercas de markdown, `<think>` e texto ao redor do JSON.
- **Cache só de sucesso:** SQL que falhou nunca é cacheado. No hit, o SQL é **reexecutado** (os dados estão sempre atualizados) e o resumo só é gerado se ainda não existir.
- **Degradação graciosa:** se o resumo falhar (quota, timeout), a resposta sai com o resumo local por template. Os dados já obtidos não se perdem.
- **Auditoria:** em caso de erro, o último SQL tentado também é exibido.
- **Avaliação opt-in:** compara a coluna-chave do gabarito como conjunto, em qualquer coluna da resposta, tolerando ordem de empates e posição de coluna. Roda sem resumo (~14 req); reexecuções saem do cache (0 req).

**Resultado:** 135 testes passando + 14 de avaliação LLM pulados por padrão; Black e Flake8 limpos. Smoke test do CLI: sem chave → mensagem de configuração e exit 1; `--quota` OK; render de ponta a ponta com LLM fake sobre o banco real OK.

---

## Passo 4.1 — Avaliação real com o OpenRouter (2026-10-02)

**Status:** ✅ Concluído. 14/14 perguntas corretas; 18 de 50 req consumidas no dia.

**Como foi executada**
- Smoke test com 1 pergunta antes de rodar as outras 13 (proteção de quota).
- Script instrumentado (fora do repositório) envolveu a função de completion para registrar cada tentativa: modelo, resultado, latência e se o JSON era válido.
- As 13 restantes rodaram com `LLM_MODELS` reordenado (nemotron primeiro) só naquela execução, depois que o gemma deu 429 no smoke test.
- Revalidação pela suíte oficial `RUN_LLM_EVAL=1 pytest -m llm`: **14 passed com 0 req** (tudo saiu do cache).

**Comportamento observado do fallback e do formato**
| Evento | Ocorrências | Tratamento |
|---|---|---|
| `gemma-4-31b-it:free` → HTTP 429 | 1/1 | Fallback automático para o nemotron, na mesma pergunta |
| `nemotron-3-super-120b-a12b:free` respondeu | 17/17 chamadas | Latência de 3,7s a 21,5s |
| JSON inválido na 1ª resposta | 3/17 (margem, ator 5 anos, diretores) | Retry com o erro devolvido ao modelo, 3/3 recuperados (+3 req) |

**Resultado comparativo (agente × gabarito)**
| # | Pergunta | 1º resultado do agente | Comparador v1 | Final | Observação |
|---|---|---|---|---|---|
| 1 | Top 10 receita R$ | Avatar: The Way Of Water | ✅ | ✅ | via fallback (gemma 429) |
| 2 | Lucro médio por gênero | Science Fiction (US$ 183 mi) | ❌ | ✅ | valores idênticos; o agente usou `LIMIT 10` e o gabarito traz os 19 |
| 3 | Maior margem | Secret Superstar (479×) | ✅ | ✅ | P1 aplicada; JSON recuperado no retry |
| 4 | 5 mais populares | Blue Beetle | ✅ | ✅ | |
| 5 | Divergência TMDB × IMDb | Me Against You... (6,43) | ✅ | ✅ | P2 aplicada |
| 6 | IMDb por ano | 2016: 6,34 | ❌ | ✅ | gabarito ajustado ao enunciado literal (ver abaixo) |
| 7 | Ator, últimos 5 anos | Eric Roberts (54) | ❌ | ✅ | `LIMIT 1` em pergunta no singular; D2 aplicada; JSON recuperado |
| 8 | Diretores, nota (≥ 5 filmes) | — | ✅ | ✅ | JSON recuperado no retry |
| 9 | Dupla ator–diretor | Joe Anoa'i × Kevin Dunn (37) | ❌ | ✅ | `LIMIT 1`; usou espontaneamente o padrão `MATERIALIZED` do few-shot |
| 10 | Filmes por gênero | Drama (28.086) | ✅ | ✅ | |
| 11 | Produtora com maior lucro | Marvel Studios (US$ 14,9 bi) | ❌ | ✅ | `LIMIT 1` |
| 12 | Gênero com maior margem média | Music (15,55×) | ❌ | ✅ | `LIMIT 1`; P1 aplicada |
| 13 | Mais avaliados | Die Hart 2: Die Harter (13) | ❌ | ✅ | empate de títulos duplicados; a chave passou a ser a contagem |
| 14 | Divergência usuários × IMDb | — | ✅ | ✅ | P2 aplicada |

**Transparência sobre a mudança de 6/14 para 14/14:** nenhum SQL do agente foi alterado. As mudanças foram no avaliador, e todas estão justificadas acima:
1. **Comparador v2** (`key_values_match`): compara os *k* primeiros valores da coluna-chave (k = menor nº de linhas) como multiconjunto. A v1 exigia o conjunto completo do gabarito e por isso reprovava `LIMIT 1` em perguntas no singular ("**Qual** ator…", "**Qual** produtora…"), que é a resposta correta. Agora "acertar o 1º lugar" passa, e "trazer o 1º lugar errado" continua reprovando (teste `test_key_values_match_accepts_top1_answer`).
2. **`rev_mais_avaliados`:** a coluna-chave passou de título para contagem. Os títulos duplicados na origem (P3) empatam em 10 avaliações, então qual título aparece no corte é arbitrário.
3. **`pop_imdb_por_ano`:** removido o filtro `status_filme = 'Lançado'` do gabarito. Ele era uma interpretação minha, não estava no enunciado ("Nota média IMDb por ano de lançamento"). O agente seguiu o texto literal, que é o critério oficial.

**Outros ajustes feitos com base na avaliação**
| Arquivo | Alteração | Motivo |
|---|---|---|
| `src/agent/llm.py` | Parser tolerante a quebras de linha cruas em strings (`strict=False`) e ao escape inválido `'` | Hipótese para as 3 falhas de JSON. O log da avaliação guardou só 300 caracteres por resposta, então a causa exata não foi confirmada. A resposta bruta agora vai para `logger.debug` |
| `src/agent/llm.py` | `logger.warning` a cada fallback; `LLMResponse.fallbacks` com a trilha de falhas | Observabilidade da instabilidade dos modelos |
| `src/config.py`, `.env.example` | Ordem padrão: nemotron → gemma → qwen | Nemotron 17/17 com boa aderência ao formato; gemma deu 429 no 1º uso. Economiza 1 req por pergunta enquanto o gemma estiver limitado |

**Melhoria possível (não implementada):** em perguntas no singular, o agente devolve só 1 linha. Para o público executivo, um top 3–5 daria mais contexto. Fica como ajuste de prompt opcional.

---

## Passo 4.2 — Top 5 em perguntas singulares e causa real do JSON malformado (2026-10-02)

**Status:** ✅ Concluído e testado (offline + 5 req de validação real)

**O que foi feito**
| Arquivo | Alteração |
|---|---|
| `src/agent/prompts.py` | Regra: perguntas no singular sobre liderança retornam o top 5 (`LIMIT 5`) em vez de `LIMIT 1`; o resumo destaca o líder e compara com o 2º. `PROMPT_VERSION` → `2026-10-02.3` |
| `src/agent/llm.py` | Reparo de "cauda com escape duplo" e recuperação do campo `sql` como último recurso, antes de gastar retry |
| `tests/golden_queries.py` | Colunas-chave robustas a empates/tradução: ator (contagem), dupla (contagem), gênero × margem (margem) |
| `src/agent/formatter.py` | Rodapé "SQL do cache" (antes dizia só "cache" mesmo com o resumo gerado na hora) |
| `requirements.txt` | `openai>=3.24,<4` (os dublês de teste usam `httpx2`, dependência do SDK 3.x); removidos `fastapi`/`uvicorn`, sem uso |

**Justificativa**
- **Top 5 (decisão do usuário):** dá contexto analítico ao executivo (ex.: distância entre o líder e o 2º colocado).
- **Causa real do JSON malformado:** desta vez a resposta bruta foi capturada inteira. O nemotron fecha a string `sql` corretamente e escreve o resto do objeto com escape duplo (`",
\"premissas\": [...]}"}`). A hipótese do Passo 4.1 (`'` / quebras cruas) **não** era a causa. O reparo desfaz `\"` e `
` e reprocessa. Se ainda falhar, extrai só o campo `sql`. Incidência observada: 4 de 21 chamadas (~20%), e cada uma custava 1 req de retry.
- **Colunas-chave:** com o top 5, o corte cai em empates (5+ atores com 37 filmes, 5+ duplas com 31), e o agente também pode traduzir nomes de gênero. A comparação passa a usar a métrica. O líder continua verificado porque sua contagem/margem é única.

**Validação real (quota 18 → 25)**
| Verificação | Resultado | Req |
|---|---|---|
| 4 perguntas singulares com a nova regra | 4/4 com top 5 (ex.: Eric Roberts 54, seguido de 4 atores com 37) | 5 (1 retry por JSON) |
| Reprocessamento offline da resposta bruta que falhou | Recuperada com as 3 premissas | 0 |
| Exemplo do README ("Qual produtora…", com resumo) | Resumo cita o líder e a diferença para o 2º | 1 |
| Pedido de escrita real ("Apague todos os filmes…") | Recusado pelo modelo, nenhum SQL executado | 1 |

**Pendência conhecida:** as outras 10 perguntas não foram reavaliadas sob o `PROMPT_VERSION` 3 (o cache delas foi invalidado). A regra nova só afeta perguntas no singular; rodar `RUN_LLM_EVAL=1 pytest -m llm` reavalia as 14 por cerca de 10 req.

---

## Passo 5 — README (2026-10-02)

**Status:** ✅ Concluído

**O que foi feito**
- `README.md`: exemplo real de saída, destaques, arquitetura (diagrama Mermaid + tabela de módulos), decisões de negócio e qualidade de dados, guardrails, estratégia de quota, instalação (venv Windows/Linux/macOS, banco, `.env` com tabela de variáveis), uso do CLI, testes offline, avaliação online opt-in, lint, estrutura e limitações.

**Justificativa**
- O requisito oficial exige versionamento no GitHub com um README passo a passo para executar a aplicação.
- Cada afirmação do README foi conferida antes do commit: comandos de teste/lint executados, recusa de escrita validada com o LLM real, exemplo de saída gerado pelo CLI real.

---

## Passo 5.1 — Reavaliação completa e ajuste das regras de nota (2026-10-05)

**Status:** ✅ Concluído e testado (14/14 com o LLM real)

**O que foi feito**
- Reavaliação das 14 perguntas sob o `PROMPT_VERSION` `2026-10-02.3`: **13/14**. Falhou `cast_diretores_maior_nota`: o modelo aplicou `qtd_imdb >= 50` (regra P2, que vale só para divergência entre fontes) e usou `LIMIT 5` numa pergunta no plural. Scott Wozniak (líder do gabarito, 9,34) e Trevor L. Allen ficaram de fora.
- Decisão do usuário (opção A): corrigir o prompt, não o gabarito.

| Arquivo | Mudança |
|---|---|
| `src/agent/prompts.py` | Regra P2 explicita que o mínimo de votos vale **só** para comparação entre fontes; médias/rankings simples não filtram `qtd_tmdb`/`qtd_imdb >= 50`, salvo se a pergunta pedir um mínimo. Notas: `qtd_tmdb > 0` / `nota_imdb IS NOT NULL` são os únicos filtros em médias simples. Perguntas no plural usam `LIMIT 10`, salvo outra quantidade pedida. `PROMPT_VERSION` → `2026-10-05.1` (invalida o cache) |
| `README.md` | Último resultado com data e versão do prompt |

**Justificativa**
- O filtro extra muda a resposta de negócio (exclui o 1º colocado) sem o usuário ter pedido. A P2 existe para divergências, onde 1 voto distorce a diferença entre fontes.
- A exceção "salvo se a pergunta pedir um mínimo de votos" mantém coerência com o few-shot de ação (`qtd_tmdb >= 100` pedido na pergunta).

**Validação**
| Verificação | Resultado | Req |
|---|---|---|
| Suíte offline + Black + Flake8 | 140 passed, 14 skipped; lint limpo | 0 |
| `RUN_LLM_EVAL=1 pytest -m llm` (v3) | 13/14 | 10 |
| `RUN_LLM_EVAL=1 pytest -m llm` (`2026-10-05.1`) | **14/14**; diretores: sem filtro de votos, `LIMIT 10`, Scott Wozniak em 1º; divergências mantêm `>= 50` | 14 |

Quota em 2026-10-05: 24/50. As 14 respostas estão em cache sob a versão nova.

---

## Próximo ⏳
- `git push` manual pelo desenvolvedor.
