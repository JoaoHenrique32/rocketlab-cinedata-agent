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

## Passo 5.2 — Registro detalhado da avaliação das 14 perguntas (2026-10-05)

**Status:** ✅ Concluído (14/14 no teste automatizado; 2 perguntas com ressalva de completude documentada)

**O que foi feito**
- Cada pergunta oficial foi respondida pelo agente com resumo executivo gerado pelo LLM (`summarize=True`). O SQL saiu do cache da avaliação 14/14 (`PROMPT_VERSION` `2026-10-05.1`) e foi reexecutado no banco. Foi gasta 1 req por resumo (14 no total; quota do dia 26 → 40/50).
- Para cada pergunta ficam registrados: o tipo/padrão do SQL, o resumo executivo integral, as premissas, a tabela completa retornada, o SQL integral executado e o status da validação.
- Os textos dos resumos estão **literais**, como o agente respondeu, incluindo imperfeições de formatação numérica do LLM (ver "Ressalvas").

**Justificativa**
- Dá ao avaliador evidência auditável da resposta de ponta a ponta (pergunta → SQL → dados → resumo), e não só do resultado do teste automatizado.
- O tipo de SQL mostra a variedade técnica coberta: joins via bridge, CTEs, CTE materializada, agregações com `HAVING`, métricas derivadas e referência temporal dinâmica.

**Ambiente:** modelo `nvidia/nemotron-3-super-120b-a12b:free` (1º da cadeia; nenhum fallback acionado). Os números das tabelas usam o formatador do agente (padrão pt-BR).

### 1. Quais são os 10 filmes com maior receita em R$?
- **Categoria:** Bilheteria e Finanças · **ID:** `fin_top10_receita_brl`
- **Tipo/Padrão de SQL:** Join fato × dimensão + ranking Top-N (`ORDER BY` / `LIMIT 10`)
- **Linhas retornadas:** 10 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> O filme com maior receita em R$ é **Avatar: The Way Of Water**, que arrecadou **R$ 12.390.136.500,54**. Em segundo lugar vem **Avengers: Endgame**, com **R$ 11.094.720.000,00**, ficando aproximadamente **R$ 1.295.416.500,54** atrás do líder. A lista completa inclui ainda Spider‑Man: No Way Home (R$ 10.977.782.882,74), Avengers: Infinity War (R$ 7.190.430.847,63), Top Gun: Maverick (R$ 7.160.804.869,01), Barbie (R$ 6.856.159.007,38), The Super Mario Bros. Movie (R$ 6.838.413.799,10), The Lion King (R$ 6.227.552.146,58), Frozen II (R$ 6.094.028.191,32) e Jurassic World: Fallen Kingdom (R$ 4.934.822.930,85), todas com receitas superiores a R$ 4,9 bilhões. Não foram identificadas anomalias de qualidade nos dados apresentados.

**Premissas**
- Receita em R$ considerada a coluna receita_brl.
- Apenas filmes com receita informada (receita_brl > 0) são incluídos.

**Dados retornados**

| titulo | ano_lancamento | receita_brl |
| --- | --- | --- |
| Avatar: The Way Of Water | 2022 | 12.390.136.500,54 |
| Avengers: Endgame | 2019 | 11.094.720.000 |
| Spider-man: No Way Home | 2021 | 10.977.782.882,74 |
| Avengers: Infinity War | 2018 | 7.190.430.847,63 |
| Top Gun: Maverick | 2022 | 7.160.804.869,01 |
| Barbie | 2023 | 6.856.159.007,38 |
| The Super Mario Bros. Movie | 2023 | 6.838.413.799,10 |
| The Lion King | 2019 | 6.227.552.146,58 |
| Frozen Ii | 2019 | 6.094.028.191,32 |
| Jurassic World: Fallen Kingdom | 2018 | 4.934.822.930,85 |

**SQL executado**
```sql
SELECT m.titulo, m.ano_lancamento, f.receita_brl AS receita_brl
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
WHERE f.receita_brl > 0
ORDER BY f.receita_brl DESC
LIMIT 10
```

### 2. Qual o lucro médio por gênero, considerando apenas filmes com receita informada?
- **Categoria:** Bilheteria e Finanças · **ID:** `fin_lucro_medio_genero`
- **Tipo/Padrão de SQL:** CTE de filtragem + multi-join via bridge (`bridge_movie_genre`) + agregação `AVG` / `GROUP BY`
- **Linhas retornadas:** 10 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ⚠️ Aprovada no teste automatizado, **com ressalva**: o agente aplicou `LIMIT 10` e devolveu 10 dos 19 gêneros. O gabarito lista todos. O teste compara só as primeiras linhas, então o corte não foi detectado. Ver "Ressalvas" abaixo.

**Resumo executivo (integral)**

> O gênero com maior lucro médio foi **Science Fiction**, com **US$ 183,055,334,67** por filme, seguido de perto por **Adventure**, cuja média foi **US$ 181,399,653,22** – uma diferença de aproximadamente **US$ 1,655,681,45**. Os demais gêneros apresentaram lucros médios que variam entre **US$ 126,985,391,65** (Fantasy) e **US$ 41,516,368,31** (Horror), com amostras que vão de 45 a 379 filmes. A análise considerou apenas filmes com receita e orçamento positivos, e os valores foram arredondados a duas casas decimais.

**Premissas**
- Lucro calculado como receita_usd - orcamento_usd (USD).
- Considerados apenas filmes com receita > 0 e orçamento > 0, conforme regra de negócio para análise de lucro.
- Média arredondada com 2 casas decimais.
- Resultado limitado aos 10 gêneros com maior lucro médio (padrão para perguntas no plural).

**Dados retornados**

| nome_genero | lucro_medio_usd | qtd_filmes |
| --- | --- | --- |
| Science Fiction | 183.055.334,67 | 134 |
| Adventure | 181.399.653,22 | 246 |
| Fantasy | 126.985.391,65 | 134 |
| Animation | 126.541.446,29 | 97 |
| Family | 116.663.901,69 | 142 |
| Action | 114.110.307,89 | 379 |
| War | 63.179.459,23 | 57 |
| Music | 52.139.511,67 | 45 |
| Comedy | 50.932.226,53 | 403 |
| Horror | 41.516.368,31 | 168 |

**SQL executado**
```sql
WITH filtered AS (
    SELECT f.sk_movie_id,
           (f.receita_usd - f.orcamento_usd) AS lucro_usd
    FROM fact_movies_performance f
    WHERE f.receita_usd > 0
      AND f.orcamento_usd > 0
)
SELECT g.nome_genero,
       ROUND(AVG(fil.lucro_usd), 2) AS lucro_medio_usd,
       COUNT(*) AS qtd_filmes
FROM filtered fil
JOIN dim_movies m ON m.sk_movie_id = fil.sk_movie_id
JOIN bridge_movie_genre bg ON bg.sk_movie_id = m.sk_movie_id
JOIN dim_genres g ON g.sk_genre_id = bg.sk_genre_id
GROUP BY g.sk_genre_id, g.nome_genero
ORDER BY lucro_medio_usd DESC
LIMIT 10
```

### 3. Quais filmes têm a maior margem de lucro, entre os que possuem receita e orçamento informados?
- **Categoria:** Bilheteria e Finanças · **ID:** `fin_maior_margem`
- **Tipo/Padrão de SQL:** Join fato × dimensão + métrica derivada (margem calculada na consulta) + ranking Top-N
- **Linhas retornadas:** 10 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> O filme com a maior margem de lucro é **Secret Superstar** (2017), apresentando margem de **479,0** (receita de US$ 137,416,709 e orçamento de US$ 286,284). Em segundo lugar vem **Demond The Movie** (2021), com margem de **299,0** (receita US$ 3,000,000 e orçamento US$ 10,000), seguido por **Unbound** (2018), cuja margem é **261,5** (receita US$ 21,000,000 e orçamento US$ 80,000). Observa‑se que alguns registros apresentam possíveis problemas de qualidade, como o título “Ariana Grande - Positions Album (official Live Performances) | Vevo”, que não parece ser um filme, e o nome “Demond The Movie”, que pode conter erro de digitação.

**Premissas**
- Margem de lucro calculada como (receita - orçamento) / orçamento usando valores em USD.
- Apenas filmes com receita > 0, orçamento > 0 e orçamento >= 10.000 USD são considerados, conforme regras de negócio para análise de margem.

**Dados retornados**

| titulo | ano_lancamento | receita_usd | orcamento_usd | lucro_usd | margem_lucro |
| --- | --- | --- | --- | --- | --- |
| Secret Superstar | 2017 | 137.416.709 | 286.284 | 137.130.425 | 479,00 |
| Demond The Movie | 2021 | 3.000.000 | 10.000 | 2.990.000 | 299,00 |
| Unbound | 2018 | 21.000.000 | 80.000 | 20.920.000 | 261,50 |
| Me Against You: Mr. S's Vendetta | 2020 | 9.500.000 | 75.000 | 9.425.000 | 125,67 |
| Dragon Ball Super: Broly | 2018 | 125.002.821 | 1.000.000 | 124.002.821 | 124,00 |
| Ariana Grande - Positions Album (official Live Performances) \| Vevo | 2021 | 3.000.000 | 25.000 | 2.975.000 | 119,00 |
| The Farewell | 2019 | 23.076.657 | 250.300 | 22.826.357 | 91,20 |
| The Villainess | 2017 | 8.737.458 | 125.000 | 8.612.458 | 68,90 |
| Terrifier 2 | 2022 | 15.065.239 | 250.000 | 14.815.239 | 59,26 |
| Get Out | 2017 | 255.407.969 | 4.500.000 | 250.907.969 | 55,76 |

**SQL executado**
```sql
SELECT m.titulo,
       m.ano_lancamento,
       f.receita_usd,
       f.orcamento_usd,
       (f.receita_usd - f.orcamento_usd) AS lucro_usd,
       ROUND(((f.receita_usd - f.orcamento_usd) * 1.0 / f.orcamento_usd), 2) AS margem_lucro
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
WHERE f.receita_usd > 0
  AND f.orcamento_usd > 0
  AND f.orcamento_usd >= 10000
ORDER BY margem_lucro DESC
LIMIT 10
```

### 4. Quais são os 5 filmes mais populares?
- **Categoria:** Popularidade e Engajamento · **ID:** `pop_top5_populares`
- **Tipo/Padrão de SQL:** Join fato × dimensão + ranking Top-N (`LIMIT 5`)
- **Linhas retornadas:** 5 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> Os cinco filmes mais populares são liderados por **Blue Beetle** (popularidade 2 994,357), seguido de **Gran Turismo** (2 680,593) e **La Fellinette** (2 020,0). A diferença entre o primeiro e o segundo lugar é de aproximadamente 313,764 pontos de popularidade. Observa‑se que alguns títulos apresentam valores de popularidade muito próximos ou iguais ao ano de lançamento (ex.: La Fellinette 2020.0, The Fear Footage 2: Curse Of The Tape 2019.0 e Wwe Survivor Series 2018 2018.0), o que pode indicar problemas de qualidade nos dados.

**Premissas**
- _(o agente não registrou premissas)_

**Dados retornados**

| titulo | ano_lancamento | popularidade |
| --- | --- | --- |
| Blue Beetle | 2023 | 2.994,36 |
| Gran Turismo | 2023 | 2.680,59 |
| La Fellinette | 2020 | 2.020,00 |
| The Fear Footage 2: Curse Of The Tape | 2020 | 2.019,00 |
| Wwe Survivor Series 2018 | 2018 | 2.018,00 |

**SQL executado**
```sql
SELECT m.titulo, m.ano_lancamento, f.popularidade
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
ORDER BY f.popularidade DESC
LIMIT 5
```

### 5. Quais filmes têm a maior divergência entre a nota TMDB e a nota IMDb?
- **Categoria:** Popularidade e Engajamento · **ID:** `pop_divergencia_tmdb_imdb`
- **Tipo/Padrão de SQL:** CTE + métrica derivada entre fontes (`ABS(nota_tmdb - nota_imdb)`) + filtros de amostra mínima (P2) + ranking
- **Linhas retornadas:** 10 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> O filme com a maior divergência entre as notas TMDB e IMDb é **“Me Against You: Mr. S's Vendetta”** (2020), cuja diferença absoluta é de **6,43 pontos** (nota TMDB 8,13 vs. nota IMDb 1,7, com 460 e 511 avaliações, respectivamente). Em segundo lugar vem **“It's Me Against You - The Mystery Of The Spellbound School”** (2021), com divergência de **6,30 pontos** (nota TMDB 8,5 vs. nota IMDb 2,2, 72 e 177 avaliações), ou seja, apenas **0,13 ponto** a menos que o líder. Observa‑se que alguns títulos são muito semelhantes (ex.: as duas primeiras linhas), o que pode indicar cadastros duplicados na fonte, e que a amostra TMDB de “Viking: Bloodlust” está próxima do limite mínimo exigido (52 avaliações). Todos os filmes listados atendem aos critérios de qtd_tmdb ≥ 50 e qtd_imdb ≥ 50.

**Premissas**
- Divergência calculada como a diferença absoluta entre nota TMDB e nota IMDb.
- Para comparação entre fontes, exigimos amostra mínima: qtd_tmdb >= 50 e qtd_imdb >= 50.
- Somente filmes com nota IMDb não nula são considerados.
- Resultados ordenados pela maior divergência, limitados aos top 10.

**Dados retornados**

| titulo | ano_lancamento | nota_tmdb | qtd_tmdb | nota_imdb | qtd_imdb | divergencia |
| --- | --- | --- | --- | --- | --- | --- |
| Me Against You: Mr. S's Vendetta | 2020 | 8,13 | 460 | 1,70 | 511 | 6,43 |
| It's Me Against You - The Mystery Of The Spellbound School | 2021 | 8,50 | 72 | 2,20 | 177 | 6,30 |
| 5gang: A Different Kind Of Christmas | 2019 | 8,20 | 104 | 2,00 | 3.451 | 6,20 |
| Viking: Bloodlust | 2023 | 6,93 | 52 | 2,20 | 363 | 4,73 |
| Baaghi 3 | 2020 | 6,80 | 99 | 2,30 | 18.231 | 4,50 |
| Beneath The Surface | 2022 | 6,63 | 60 | 2,40 | 242 | 4,22 |
| Harry And Meghan: Escaping The Palace | 2021 | 6,76 | 153 | 2,60 | 1.485 | 4,16 |
| Megalodon Rising | 2021 | 6,11 | 125 | 2,10 | 1.090 | 4,01 |
| Arctic Apocalypse | 2019 | 6,20 | 165 | 2,20 | 746 | 4,00 |
| The Immortal Wars: Resurgence | 2019 | 5,97 | 70 | 2,00 | 261 | 3,97 |

**SQL executado**
```sql
WITH filtered AS (
    SELECT m.titulo,
           m.ano_lancamento,
           f.nota_tmdb,
           f.qtd_tmdb,
           f.nota_imdb,
           f.qtd_imdb,
           ABS(f.nota_tmdb - f.nota_imdb) AS divergencia
    FROM fact_movies_performance f
    JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
    WHERE f.qtd_tmdb >= 50
      AND f.qtd_imdb >= 50
      AND f.nota_imdb IS NOT NULL
)
SELECT titulo,
       ano_lancamento,
       ROUND(nota_tmdb, 2) AS nota_tmdb,
       qtd_tmdb,
       ROUND(nota_imdb, 2) AS nota_imdb,
       qtd_imdb,
       ROUND(divergencia, 2) AS divergencia
FROM filtered
ORDER BY divergencia DESC
LIMIT 10
```

### 6. Qual a nota média IMDb por ano de lançamento?
- **Categoria:** Popularidade e Engajamento · **ID:** `pop_imdb_por_ano`
- **Tipo/Padrão de SQL:** Agregação temporal (`AVG` / `GROUP BY ano_lancamento`) com série ordenada por ano
- **Linhas retornadas:** 13 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> Entre 2016 e 2024 a nota média IMDb variou entre 6,15 e 6,34, com as maiores médias em 2016 e 2017 (6,34) e a menor em 2024 (6,15), calculadas a partir de mais de 9 mil filmes por ano. A partir de 2025 as médias apresentam oscilações bruscas (6,58 em 2025, 7,5 em 2026, 6,4 em 2027 e 3,8 em 2029), mas cada um desses anos possui amostra extremamente reduzida (4, 1, 2 e 1 filme respectivamente), o que indica possível problema de qualidade ou dados de placeholder. Além disso, 2024 mostra uma queda abrupta no volume (1 621 filmes) em relação aos anos anteriores, sugerindo que o conjunto de dados pode estar incompleto para os lançamentos mais recentes.

**Premissas**
- Nota IMDb considerada apenas quando nota_imdb IS NOT NULL (sem requisito mínimo de votos para médias simples).
- Agregação por ano de lançamento inclui todos os filmes com nota IMDb disponível, independentemente do status de lançamento.

**Dados retornados**

| ano_lancamento | nota_media_imdb | qtd_filmes |
| --- | --- | --- |
| 2016 | 6,34 | 10.381 |
| 2017 | 6,34 | 11.189 |
| 2018 | 6,27 | 11.327 |
| 2019 | 6,26 | 11.637 |
| 2020 | 6,24 | 9.534 |
| 2021 | 6,23 | 9.578 |
| 2022 | 6,23 | 9.887 |
| 2023 | 6,23 | 7.809 |
| 2024 | 6,15 | 1.621 |
| 2025 | 6,58 | 4 |
| 2026 | 7,50 | 1 |
| 2027 | 6,40 | 2 |
| 2029 | 3,80 | 1 |

**SQL executado**
```sql
SELECT m.ano_lancamento,
       ROUND(AVG(f.nota_imdb), 2) AS nota_media_imdb,
       COUNT(*) AS qtd_filmes
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
WHERE f.nota_imdb IS NOT NULL
GROUP BY m.ano_lancamento
ORDER BY m.ano_lancamento
LIMIT 200
```

### 7. Qual ator teve mais participações em filmes lançados nos últimos 5 anos?
- **Categoria:** Elenco e Equipe · **ID:** `cast_ator_mais_filmes_5anos`
- **Tipo/Padrão de SQL:** Múltiplas CTEs (ano de referência dinâmico via `CROSS JOIN`) + multi-join via bridge + agregação por pessoa + Top-N
- **Linhas retornadas:** 5 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> Eric Roberts foi o ator com maior número de participações em filmes lançados nos últimos 5 anos, contabilizando 54 obras. Ele ficou à frente de quatro atores que empataram em segundo lugar, cada um com 37 filmes, ou seja, uma diferença de 17 participações em relação ao próximo colocado. Não foram identificadas anomalias de qualidade nos dados apresentados e o resultado traz o top 5 solicitado, portanto não há truncamento relevante.

**Premissas**
- Últimos 5 anos relativo ao ano mais recente com filmes lançados no banco.
- Considera apenas filmes com status 'Lançado'.
- Contagem de participações por ator (sk_person_id).
- Retorna o top 5 para dar contexto, sendo o 1º colocado a resposta.

**Dados retornados**

| nome_pessoa | qtd_filmes |
| --- | --- |
| Eric Roberts | 54 |
| David Love | 37 |
| Anton Pelizzari | 37 |
| Cameron Nichols | 37 |
| Jazzyjoeyjr | 37 |

**SQL executado**
```sql
WITH ref AS (
    SELECT MAX(ano_lancamento) AS ano_max
    FROM dim_movies
    WHERE status_filme = 'Lançado'
),
actor_movies AS (
    SELECT p.sk_person_id, p.nome_pessoa
    FROM dim_people p
    JOIN bridge_movie_person bp ON bp.sk_person_id = p.sk_person_id
    JOIN dim_movies m ON m.sk_movie_id = bp.sk_movie_id
    CROSS JOIN ref
    WHERE p.tipo_pessoa = 'Ator'
      AND m.status_filme = 'Lançado'
      AND m.ano_lancamento > ref.ano_max - 5
)
SELECT am.nome_pessoa, COUNT(*) AS qtd_filmes
FROM actor_movies am
GROUP BY am.sk_person_id, am.nome_pessoa
ORDER BY qtd_filmes DESC
LIMIT 5
```

### 8. Quais diretores têm a maior nota média (mínimo de 5 filmes)?
- **Categoria:** Elenco e Equipe · **ID:** `cast_diretores_maior_nota`
- **Tipo/Padrão de SQL:** CTE + multi-join via bridge (`bridge_movie_person`) + agregação `AVG` com `HAVING COUNT(*) >= 5` + ranking
- **Linhas retornadas:** 10 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> O diretor com a maior nota média IMDb, considerando apenas aqueles com pelo menos cinco filmes, é **Scott Wozniak**, cuja média é **9,34** em cinco títulos. Ele fica à frente de **Yūichirō Hayashi** e **Jun Shishido**, que empatam em segundo lugar com média de **9,19** cada um (diferença de **0,15** ponto para o líder). Não há indícios óbvios de problemas de qualidade nos dados apresentados (sem títulos duplicados, valores extremos ou popularidades equivocadas). O resultado mostra os dez primeiros colocados; pode haver outros diretores além dessa lista que também atendem ao critério de mínimo de cinco filmes.

**Premissas**
- Nota considerada é IMDb (nota_imdb).
- Apenas diretores (tipo_pessoa = 'Diretor').
- Filmes com nota IMDb nula são excluídos.
- Mínimo de 5 filmes por diretor para entrar no ranking.
- Média arredondada com 2 casas decimais.

**Dados retornados**

| nome_pessoa | media_nota | qtd_filmes |
| --- | --- | --- |
| Scott Wozniak | 9,34 | 5 |
| Yūichirō Hayashi | 9,19 | 8 |
| Jun Shishido | 9,19 | 8 |
| Trevor L. Allen | 9,15 | 6 |
| Alonso O. Lara | 9,09 | 14 |
| Tokio Igarashi | 9,00 | 5 |
| Erlik | 8,95 | 6 |
| Stuart Webster | 8,88 | 5 |
| Mark Fischbach | 8,83 | 6 |
| John D. Boswell | 8,70 | 8 |

**SQL executado**
```sql
WITH dir_movies AS (
    SELECT p.sk_person_id, p.nome_pessoa, f.nota_imdb
    FROM dim_people p
    JOIN bridge_movie_person bp ON bp.sk_person_id = p.sk_person_id
    JOIN fact_movies_performance f ON f.sk_movie_id = bp.sk_movie_id
    WHERE p.tipo_pessoa = 'Diretor'
      AND f.nota_imdb IS NOT NULL
)
SELECT dm.nome_pessoa,
       ROUND(AVG(dm.nota_imdb), 2) AS media_nota,
       COUNT(*) AS qtd_filmes
FROM dir_movies dm
GROUP BY dm.sk_person_id, dm.nome_pessoa
HAVING COUNT(*) >= 5
ORDER BY media_nota DESC, qtd_filmes DESC
LIMIT 10
```

### 9. Qual dupla ator-diretor mais trabalhou junta?
- **Categoria:** Elenco e Equipe · **ID:** `cast_dupla_ator_diretor`
- **Tipo/Padrão de SQL:** CTEs `MATERIALIZED` por papel + join de co-ocorrência (pares no mesmo filme) + Top-N dentro de CTE + re-join com `dim_people`
- **Linhas retornadas:** 5 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> A dupla que mais colaborou foi Joe Anoa'i (ator) e Kevin Dunn (diretor), com 37 filmes juntos. Em segundo lugar ficou Colby Lopez também com Kevin Dunn, com 32 filmes, enquanto o primeiro tem 37. Além disso, o diretor Kevin Dunn aparece em duas das cinco primeiras posições e há um empate em 31 filmes entre três pares, sugerindo possível concentração de trabalho ou duplicidade de cadastros, e como só o top 5 foi exibido, outros pares com valores semelhantes podem não estar visíveis.

**Premissas**
- Consideramos todos os filmes presentes no banco (não filtramos por status de lançamento).
- A mesma pessoa pode aparecer tanto como ator quanto como diretor (cadastros distintos por papel).
- Retornamos o top 5 de pares ator‑diretor para fornecer contexto; o primeiro colocado é a resposta à pergunta.

**Dados retornados**

| ator | diretor | qtd_filmes |
| --- | --- | --- |
| Joe Anoa'i | Kevin Dunn | 37 |
| Colby Lopez | Kevin Dunn | 32 |
| David Love | Chad Payne | 31 |
| Anton Pelizzari | Chad Payne | 31 |
| Cameron Nichols | Chad Payne | 31 |

**SQL executado**
```sql
WITH
  atores AS MATERIALIZED (
    SELECT bp.sk_movie_id, bp.sk_person_id
    FROM dim_people p
    JOIN bridge_movie_person bp ON bp.sk_person_id = p.sk_person_id
    WHERE p.tipo_pessoa = 'Ator'
  ),
  diretores AS MATERIALIZED (
    SELECT bp.sk_movie_id, bp.sk_person_id
    FROM dim_people p
    JOIN bridge_movie_person bp ON bp.sk_person_id = p.sk_person_id
    WHERE p.tipo_pessoa = 'Diretor'
  ),
  pares AS (
    SELECT a.sk_person_id AS ator_id, d.sk_person_id AS diretor_id, COUNT(*) AS qtd_filmes
    FROM atores a
    JOIN diretores d ON d.sk_movie_id = a.sk_movie_id
    GROUP BY a.sk_person_id, d.sk_person_id
    ORDER BY qtd_filmes DESC
    LIMIT 5
  )
SELECT at.nome_pessoa AS ator, dir.nome_pessoa AS diretor, p.qtd_filmes
FROM pares p
JOIN dim_people at ON at.sk_person_id = p.ator_id
JOIN dim_people dir ON dir.sk_person_id = p.diretor_id
ORDER BY p.qtd_filmes DESC
LIMIT 200
```

### 10. Qual a quantidade de filmes por gênero?
- **Categoria:** Gêneros e Produtoras · **ID:** `gen_qtd_por_genero`
- **Tipo/Padrão de SQL:** Join bridge × dimensão + agregação `COUNT` / `GROUP BY`
- **Linhas retornadas:** 10 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ⚠️ Aprovada no teste automatizado, **com ressalva**: o agente aplicou `LIMIT 10` e devolveu 10 dos 19 gêneros. O gabarito lista todos. O teste compara só as primeiras linhas, então o corte não foi detectado. Ver "Ressalvas" abaixo.

**Resumo executivo (integral)**

> O gênero mais frequente é **Drama**, com **28.086** filmes, seguido de **Documentary** (18.082) e **Comedy** (16.048). Os dez gêneros listados representam a maior parte da distribuição, mas o resultado está truncado e pode haver outros gêneros não exibidos. Não foram observadas anomalias evidentes de qualidade nos dados apresentados.

**Premissas**
- Cada filme pode estar associado a mais de um gênero; nesse caso, ele é contado em cada gênero ao qual pertence. Não foram aplicados filtros de status, ano de lançamento ou receita/orçamento, pois a pergunta pede a quantidade bruta de filmes por gênero.

**Dados retornados**

| nome_genero | qtd_filmes |
| --- | --- |
| Drama | 28.086 |
| Documentary | 18.082 |
| Comedy | 16.048 |
| Horror | 8.674 |
| Thriller | 8.540 |
| Romance | 6.209 |
| Action | 5.028 |
| Animation | 3.911 |
| Crime | 3.902 |
| Tv Movie | 3.336 |

**SQL executado**
```sql
SELECT g.nome_genero,
       COUNT(*) AS qtd_filmes
FROM bridge_movie_genre bg
JOIN dim_genres g ON g.sk_genre_id = bg.sk_genre_id
GROUP BY g.sk_genre_id, g.nome_genero
ORDER BY qtd_filmes DESC
LIMIT 10
```

### 11. Qual produtora tem o maior lucro total?
- **Categoria:** Gêneros e Produtoras · **ID:** `gen_produtora_maior_lucro`
- **Tipo/Padrão de SQL:** Multi-join via bridge (`bridge_movie_company`) + agregação `SUM` / `GROUP BY` + ranking Top-N
- **Linhas retornadas:** 5 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> A produtora com maior lucro total é a **Marvel Studios**, que obteve **US$ 14.897.936.776** de lucro considerando apenas filmes com receita e orçamento informados (receita_usd > 0 e orcamento_usd > 0). Ela fica à frente da **Universal Pictures**, segunda colocada, com **US$ 13.691.646.318**, uma diferença de aproximadamente **US$ 1.206 bilhão**, apesar de ter produzido muito menos filmes (17 contra 91). As demais produtoras no ranking são Columbia Pictures, Warner Bros. Pictures e Walt Disney Pictures, com lucros entre **US$ 9,4** e **US$ 9,9 bilhões**. (Resultado limitado às 5 primeiras linhas; nenhuma anomalia de qualidade foi evidente nos dados apresentados.)

**Premissas**
- Lucro total em USD considerando apenas filmes com receita e orçamento informados (receita_usd > 0 e orcamento_usd > 0).
- Resultado limitado às 5 produtoras com maior lucro total para fornecer contexto; a primeira linha é a produtora com maior lucro.

**Dados retornados**

| nome_produtora | lucro_total_usd | qtd_filmes |
| --- | --- | --- |
| Marvel Studios | 14.897.936.776 | 17 |
| Universal Pictures | 13.691.646.318 | 91 |
| Columbia Pictures | 9.853.057.126 | 52 |
| Warner Bros. Pictures | 9.490.342.370 | 61 |
| Walt Disney Pictures | 9.431.052.715 | 34 |

**SQL executado**
```sql
SELECT c.nome_produtora,
       SUM(f.lucro_usd) AS lucro_total_usd,
       COUNT(*) AS qtd_filmes
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
JOIN bridge_movie_company bc ON bc.sk_movie_id = m.sk_movie_id
JOIN dim_companies c ON c.sk_company_id = bc.sk_company_id
WHERE f.receita_usd > 0
  AND f.orcamento_usd > 0
GROUP BY c.sk_company_id, c.nome_produtora
ORDER BY lucro_total_usd DESC
LIMIT 5
```

### 12. Qual gênero tem a maior margem de lucro média?
- **Categoria:** Gêneros e Produtoras · **ID:** `gen_maior_margem_media`
- **Tipo/Padrão de SQL:** CTE de métrica por filme + multi-join via bridge + agregação `AVG` por gênero + ranking Top-N
- **Linhas retornadas:** 5 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> O gênero com a maior margem de lucro média é **Music**, apresentando uma margem média de **15,55** com base em **44** filmes lançados. Em seguida vem **Science Fiction**, cuja margem média é de **4,03** – cerca de 74 % inferior à de Music. Os demais gêneros (Horror, Thriller e Adventure) ficam entre **3,7** e **3,99**, com amostras consideravelmente maiores (de 128 a 308 filmes cada), o que pode tornar a estimativa de Music menos estável devido ao seu número relativamente baixo de observações.

**Premissas**
- Consideramos apenas filmes lançados (status_filme = 'Lançado').
- Margem de lucro calculada em USD: (receita_usd - orcamento_usd) / orcamento_usd.
- Filmes com receita_usd > 0 e orcamento_usd > 0 (conforme regra de lucro/margem).
- Orçamento mínimo de 10.000 USD para evitar distorções por valores irrisórios.
- A média é calculada por gênero e os gêneros são ordenados pela margem média decrescente.
- Retornamos o top 5 gêneros para fornecer contexto; o primeiro é o gênero com maior margem média.

**Dados retornados**

| nome_genero | margem_lucro_media | qtd_filmes |
| --- | --- | --- |
| Music | 15,55 | 44 |
| Science Fiction | 4,03 | 128 |
| Horror | 3,99 | 150 |
| Thriller | 3,85 | 308 |
| Adventure | 3,70 | 241 |

**SQL executado**
```sql
WITH movie_margins AS (
    SELECT
        g.sk_genre_id,
        g.nome_genero,
        (f.receita_usd - f.orcamento_usd) * 1.0 / f.orcamento_usd AS margem
    FROM fact_movies_performance f
    JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
    JOIN bridge_movie_genre bg ON bg.sk_movie_id = m.sk_movie_id
    JOIN dim_genres g ON g.sk_genre_id = bg.sk_genre_id
    WHERE m.status_filme = 'Lançado'
      AND f.receita_usd > 0
      AND f.orcamento_usd > 0
      AND f.orcamento_usd >= 10000
)
SELECT
    nome_genero,
    ROUND(AVG(margem), 2) AS margem_lucro_media,
    COUNT(*) AS qtd_filmes
FROM movie_margins
GROUP BY sk_genre_id, nome_genero
ORDER BY margem_lucro_media DESC
LIMIT 5
```

### 13. Quais filmes foram mais avaliados pelos usuários?
- **Categoria:** Avaliações dos Usuários · **ID:** `rev_mais_avaliados`
- **Tipo/Padrão de SQL:** Join dimensão × dimensão (`dim_movies` × `dim_reviews`) + ranking Top-N
- **Linhas retornadas:** 10 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> O filme mais avaliado pelos usuários foi **“Die Hart 2: Die Harter”**, com **13** avaliações registradas. Em seguida, outras entradas do mesmo título aparecem com **12** avaliações, enquanto o filme **“Die Hart: Die Harter”** apresenta **11** avaliações, seguindo uma contagem decrescente até **9** nas linhas exibidas. Nota‑se que o mesmo título ocorre várias vezes com contagens diferentes, sugerindo possível duplicidade de cadastros na origem, e que o resultado está limitado às 10 primeiras linhas, podendo haver outros filmes não listados. Esses números são relativamente baixos (máximo de 13), o que pode indicar uma amostra pequena ou dados ainda em fase de coleta, conforme a premissa de considerar apenas filmes com qtd_avaliacoes_usuarios não nulo.

**Premissas**
- Consideramos apenas filmes que possuem registro de avaliações de usuários (qtd_avaliacoes_usuarios não nulo).

**Dados retornados**

| titulo | ano_lancamento | qtd_avaliacoes_usuarios |
| --- | --- | --- |
| Die Hart 2: Die Harter | 2024 | 13 |
| Die Hart 2: Die Harter | 2024 | 12 |
| Die Hart: Die Harter | 2024 | 11 |
| Die Hart: Die Harter | 2024 | 10 |
| Die Hart: Die Harter | 2024 | 10 |
| Die Hart: Die Harter | 2024 | 10 |
| Die Hart 2: Die Harter | 2024 | 10 |
| Die Hart 2: Die Harter | 2024 | 9 |
| Die Hart 2: Die Harter | 2024 | 9 |
| Die Hart 2: Die Harter | 2024 | 9 |

**SQL executado**
```sql
SELECT m.titulo, m.ano_lancamento, r.qtd_avaliacoes_usuarios
FROM dim_movies m
JOIN dim_reviews r ON r.sk_movie_id = m.sk_movie_id
WHERE r.qtd_avaliacoes_usuarios IS NOT NULL
ORDER BY r.qtd_avaliacoes_usuarios DESC
LIMIT 10
```

### 14. Em quais filmes a nota média dos usuários mais diverge da nota IMDb?
- **Categoria:** Avaliações dos Usuários · **ID:** `rev_divergencia_usuarios_imdb`
- **Tipo/Padrão de SQL:** Join de 3 tabelas (duas fontes de nota) + métrica derivada `ABS` + filtros de amostra mínima (P2) + ranking
- **Linhas retornadas:** 10 · **Chamadas ao LLM nesta execução:** 1 (resumo)
- **Status da validação:** ✅ Aprovada (compatível com o gabarito oficial)

**Resumo executivo (integral)**

> O filme com maior divergência entre a nota média dos usuários e a nota IMDb é "Bittersweet Memories: 14 Isolated Days To Make An Album", com diferença absoluta de 7,07 pontos (usuários 2,43 vs IMDb 9,5). Em segundo lugar aparece "Save Ralph", com diferença de 7,03 pontos (usuários 1,37 vs IMDb 8,4), seguido por "One Piece Fan Letter" (duas entradas) com diferenças de aproximadamente 6,369999999999999 e 6,229999999999999 pontos. Observa‑se que o resultado contém possíveis duplicidades (ex.: "One Piece Fan Letter" aparece duas vezes para o mesmo ano) e notas de usuários extremamente baixas (como 0,8 para "Velvet Buzzsaw"), o que pode indicar problemas de qualidade nos dados; além disso, a lista está limitada aos 10 maiores desvios conforme as premissas de pelo menos 3 avaliações de usuários e 50 votos IMDb.

**Premissas**
- Divergência calculada como diferença absoluta entre a nota média dos usuários (dim_reviews.nota_media_usuarios) e a nota IMDb (fact_movies_performance.nota_imdb).
- Para comparação entre fontes, exigimos amostra mínima: pelo menos 3 avaliações de usuários (qtd_avaliacoes_usuarios >= 3) e pelo menos 50 votos IMDb (qtd_imdb >= 50).
- Consideramos apenas filmes que possuem ambas as notas disponíveis (não nulas).
- Os resultados são ordenados pela maior divergência e limitados a 10 filmes (padrão para perguntas no plural).

**Dados retornados**

| titulo | ano_lancamento | nota_media_usuarios | nota_imdb | diferenca |
| --- | --- | --- | --- | --- |
| Bittersweet Memories: 14 Isolated Days To Make An Album | 2023 | 2,43 | 9,50 | 7,07 |
| Save Ralph | 2021 | 1,37 | 8,40 | 7,03 |
| One Piece Fan Letter | 2024 | 2,83 | 9,20 | 6,37 |
| One Piece Fan Letter | 2024 | 2,97 | 9,20 | 6,23 |
| The Internet And You | 2016 | 3,70 | 9,10 | 5,40 |
| The Rose Family | 2020 | 2,43 | 7,70 | 5,27 |
| Ena: Temptation Stairway | 2021 | 3,63 | 8,80 | 5,17 |
| Bunch Of Kunst - A Film About Sleaford Mods | 2017 | 2,23 | 7,40 | 5,17 |
| More Than He Knows | 2019 | 2,47 | 7,50 | 5,03 |
| Velvet Buzzsaw | 2019 | 0,80 | 5,70 | 4,90 |

**SQL executado**
```sql
SELECT m.titulo,
       m.ano_lancamento,
       dr.nota_media_usuarios,
       f.nota_imdb,
       ABS(dr.nota_media_usuarios - f.nota_imdb) AS diferenca
FROM dim_movies m
JOIN fact_movies_performance f ON f.sk_movie_id = m.sk_movie_id
JOIN dim_reviews dr ON dr.sk_movie_id = m.sk_movie_id
WHERE dr.qtd_avaliacoes_usuarios >= 3
  AND f.qtd_imdb >= 50
  AND dr.nota_media_usuarios IS NOT NULL
  AND f.nota_imdb IS NOT NULL
ORDER BY diferenca DESC
LIMIT 10
```

### Ressalvas encontradas neste registro
| # | Pergunta | Observação | Impacto | Encaminhamento |
|---|---|---|---|---|
| 1 | Lucro médio por gênero; quantidade de filmes por gênero | `LIMIT 10` cortou as duas listagens em 10 dos 19 gêneros. Provável efeito colateral da regra "plural → LIMIT 10" do Passo 5.1, aplicada a uma distribuição completa e não a um ranking. No caso da quantidade por gênero, o próprio resumo do agente avisa que o resultado está truncado | Resposta incompleta. O teste por prefixo não detecta | Ajustar a regra: distribuições "por categoria/ano" sem pedido de ranking retornam todas as linhas. **Corrigido no Passo 5.3** (19/19 nas duas perguntas) |
| 2 | Lucro médio por gênero; maior margem | Resumo do LLM mistura separadores (`US$ 183,055,334,67`) | Só cosmético: tabela e SQL estão corretos | Registrado como está |
| 3 | Divergência usuários × IMDb | Resumo cita `6,369999999999999` (float sem arredondar) | Só cosmético | Registrado como está |
| 4 | 5 filmes mais populares | Resumo escreve `2 994,357` (separador de milhar com espaço) | Só cosmético | Registrado como está |

---

## Passo 5.3 — Distribuições completas sem LIMIT e revalidação focada (2026-10-05)

**Status:** ✅ Concluído; revalidação parcial: 4/5 casos passaram e 1 desvio foi documentado

**O que foi feito**
| Arquivo | Mudança |
|---|---|
| `src/agent/prompts.py` | Regra de LIMIT dividida. **Distribuição/agrupamento completo** ("por gênero", "por ano"), sem pedido de ranking, **não usa LIMIT** e traz todas as categorias. **Rankings explícitos no plural** continuam com `LIMIT 10`. Perguntas de liderança no singular continuam com top 5. `PROMPT_VERSION` → `2026-10-05.2` (invalida o cache) |
| `tests/golden_queries.py` | Novo campo `full_result` em `GoldenCase`, marcado nas 3 distribuições (lucro médio por gênero, nota IMDb por ano, quantidade por gênero) |
| `tests/test_queries.py` | Com `full_result`, a avaliação exige o **mesmo número de linhas** do gabarito, além do prefixo. Fecha a brecha que deixou o corte em 10/19 passar no Passo 5.1 |

**Justificativa**
- A regra "plural → LIMIT 10" do Passo 5.1 confundia distribuição com ranking. "Lucro médio por gênero" pede o quadro completo, enquanto "Quais diretores têm a maior nota" pede um top. A distinção agora está explícita no prompt.
- A comparação só por prefixo foi desenhada para tolerar tops de tamanhos diferentes. Em distribuições ela escondia respostas incompletas, por isso o check de contagem fica restrito a esses casos.
- **Revalidação focada (decisão do usuário):** a quota do dia (40/50) não comportava a suíte completa (~14 req). Foram rodados os 2 casos afetados e 3 de controle, cobrindo os três tipos de LIMIT.

**Validação**
| Verificação | Resultado | Req |
|---|---|---|
| Suíte offline + Black + Flake8 | 140 passed, 14 skipped; lint limpo | 0 |
| `fin_lucro_medio_genero` (distribuição) | ✅ **19/19** gêneros, sem LIMIT do modelo (só o `LIMIT 200` de segurança do guardrail); líder Science Fiction 183.055.334,67 | 1 |
| `gen_qtd_por_genero` (distribuição) | ✅ **19/19** gêneros; líder Drama 28.086 | 1 |
| `cast_diretores_maior_nota` (controle: ranking no plural) | ✅ `LIMIT 10`, sem filtro de votos; Scott Wozniak 9,34 em 1º | 1 |
| `gen_produtora_maior_lucro` (controle: liderança no singular) | ✅ `LIMIT 5`; Marvel Studios 14.897.936.776 | 1 |
| `pop_imdb_por_ano` (controle: distribuição) | ❌ **11/13** anos. **Não é LIMIT**: o modelo acrescentou `status_filme = 'Lançado'` sem a pergunta pedir. Saem 2027 e 2029 (3 filmes ao todo) e mudam as contagens de 2020, 2023, 2024 e 2025. Os anos 2016–2022 coincidem com o gabarito. Antes passava só porque a comparação era por prefixo; o check novo detectou | 1 |

Quota em 2026-10-05: 45/50.

**Ressalva em aberto: `pop_imdb_por_ano`**
- A leitura do modelo (média só de filmes lançados) é defensável, mas diverge da regra de negócio, que só filtra status quando a pergunta diz "lançados". A premissa aparece na resposta ("Consideramos apenas filmes com status 'Lançado'…"), então o usuário vê o critério aplicado.
- Corrigir exigiria reforçar a regra no prompt, mudar de novo a versão (o que apaga o cache dos 5 casos) e revalidar com as 5 req restantes, sem margem para retry. Ficou fora por decisão de risco no dia da entrega.
- O SQL com o filtro está no cache da versão `2026-10-05.2`, então uma demonstração dessa pergunta mostra a versão com 11 anos.

**Cobertura da versão `2026-10-05.2`:** 5 de 14 perguntas revalidadas. As outras 9 passaram (14/14) sob `2026-10-05.1`, e a única mudança desde então é a regra de LIMIT. Elas **não** foram reexecutadas nesta versão e não têm cache: na primeira consulta, cada uma gera SQL novo (1 req).
