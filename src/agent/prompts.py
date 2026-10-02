"""Prompts do agente: schema compacto, regras de negócio e few-shots.

Decisões de quota: o schema vai embutido no system prompt (zero roundtrips de
descoberta) e cada etapa usa uma única chamada ao LLM. `PROMPT_VERSION` entra na
chave do cache: qualquer mudança aqui invalida respostas antigas.

Os few-shots ensinam *padrões* (tradução de gênero, filtros de lucro, ano de
referência, self-join otimizado) e propositalmente NÃO repetem as perguntas da
suíte oficial (`tests/golden_queries.py`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

PROMPT_VERSION = "2026-10-02.1"
SUMMARY_MAX_ROWS = 50


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    note: str = ""


# Espelho do schema real (validado por tests/test_prompts.py contra o banco).
SCHEMA: dict[str, tuple[Column, ...]] = {
    "dim_movies": (
        Column("sk_movie_id", "TEXT", "PK"),
        Column("id_filme", "TEXT", "id TMDB"),
        Column("titulo", "TEXT", "títulos podem se repetir entre filmes distintos"),
        Column("data_lancamento", "DATE"),
        Column("ano_lancamento", "INT", "2016-2029"),
        Column("duracao_minutos", "INT"),
        Column("idioma_original", "TEXT", "sempre NULL, não usar"),
        Column(
            "status_filme",
            "TEXT",
            "'Lançado' | 'Pós-Produção' | 'Em Produção' | 'Planejado'",
        ),
        Column("sinopse", "TEXT"),
        Column("url_poster", "TEXT"),
        Column("url_backdrop", "TEXT"),
    ),
    "fact_movies_performance": (
        Column("sk_movie_id", "TEXT", "PK, 1:1 com dim_movies"),
        Column("orcamento_usd", "NUMERIC", "NULL se desconhecido"),
        Column("receita_usd", "NUMERIC", "NULL se desconhecida"),
        Column("lucro_usd", "NUMERIC", "trata NULL como 0; preferir receita-orcamento"),
        Column("orcamento_brl", "NUMERIC"),
        Column("receita_brl", "NUMERIC"),
        Column("lucro_brl", "NUMERIC", "mesma ressalva de lucro_usd"),
        Column("popularidade", "REAL", "índice TMDB"),
        Column("nota_tmdb", "REAL", "0-10; 0 quando qtd_tmdb = 0"),
        Column("qtd_tmdb", "INT", "nº de votos TMDB"),
        Column("nota_imdb", "REAL", "0-10; NULL se ausente"),
        Column("qtd_imdb", "INT", "nº de votos IMDb"),
    ),
    "dim_genres": (
        Column("sk_genre_id", "TEXT", "PK"),
        Column(
            "nome_genero",
            "TEXT",
            "em inglês: Action, Adventure, Animation, Comedy, Crime, Documentary, "
            "Drama, Family, Fantasy, History, Horror, Music, Mystery, Romance, "
            "Science Fiction, Thriller, Tv Movie, War, Western",
        ),
    ),
    "dim_people": (
        Column("sk_person_id", "TEXT", "PK"),
        Column("nome_pessoa", "TEXT"),
        Column("tipo_pessoa", "TEXT", "'Ator' | 'Diretor' | 'Roteirista'"),
    ),
    "dim_companies": (
        Column("sk_company_id", "TEXT", "PK"),
        Column("nome_produtora", "TEXT"),
    ),
    "dim_reviews": (
        Column("sk_review_id", "TEXT", "PK"),
        Column("sk_movie_id", "TEXT", "UNIQUE, 1 linha por filme avaliado"),
        Column("qtd_avaliacoes_usuarios", "INT"),
        Column("nota_media_usuarios", "REAL", "0-10"),
    ),
    "movie_reviews": (
        Column("id", "INT", "PK"),
        Column("sk_movie_review_id", "TEXT"),
        Column("sk_movie_id", "TEXT"),
        Column("name", "TEXT", "autor da review"),
        Column("rating", "REAL", "0-10"),
        Column("text", "TEXT"),
        Column("created_at", "DATETIME"),
    ),
    "bridge_movie_genre": (
        Column("sk_movie_id", "TEXT"),
        Column("sk_genre_id", "TEXT"),
    ),
    "bridge_movie_person": (
        Column("sk_movie_id", "TEXT"),
        Column("sk_person_id", "TEXT", "papel vem de dim_people.tipo_pessoa"),
    ),
    "bridge_movie_company": (
        Column("sk_movie_id", "TEXT"),
        Column("sk_company_id", "TEXT"),
    ),
}


def render_schema() -> str:
    """Renderiza o schema em formato compacto (economia de tokens)."""
    lines: list[str] = []
    for table, columns in SCHEMA.items():
        cols = ", ".join(
            f"{c.name} {c.type}" + (f" -- {c.note}" if c.note else "") for c in columns
        )
        lines.append(f"{table}({cols})")
    return "\n".join(lines)


BUSINESS_RULES = """\
- Joins sempre pelas chaves sk_* (hash TEXT). Pessoas, gêneros e produtoras se
  ligam a filmes pelas tabelas bridge_*.
- Faturamento = Receita = Bilheteria -> receita_usd (receita_brl se pedirem R$ ou
  reais). Padrão: USD. Orçamento -> orcamento_usd/orcamento_brl.
- Lucro = receita - orcamento. Margem de lucro = (receita - orcamento) * 1.0 /
  orcamento. Em QUALQUER análise de lucro ou margem filtre
  receita_usd > 0 AND orcamento_usd > 0 (ou o par _brl), mesmo que a pergunta
  peça só "receita informada"; registre isso nas premissas.
- Rankings de receita/orçamento: filtre a coluna > 0 (valores ausentes são NULL).
- Notas TMDB: filtre qtd_tmdb > 0. Notas IMDb: filtre nota_imdb IS NOT NULL.
  "Nota" sem especificar = nota_imdb.
- Gêneros estão em inglês: traduza (Ficção Científica -> 'Science Fiction',
  Terror -> 'Horror', Comédia -> 'Comedy', Animação -> 'Animation' etc.).
- "Filmes lançados" -> status_filme = 'Lançado'. "Últimos N anos" é relativo ao
  ano mais recente com filmes lançados no banco, NÃO à data atual:
  WITH ref AS (SELECT MAX(ano_lancamento) AS ano_max FROM dim_movies
  WHERE status_filme = 'Lançado') ... ano_lancamento > ref.ano_max - N.
- Atores/diretores/roteiristas: filtre dim_people.tipo_pessoa. Agrupe por
  sk_person_id (não só pelo nome). Mínimo de filmes -> HAVING COUNT(*) >= N.
- Pares de pessoas no mesmo filme (ex.: ator x diretor): primeiro materialize cada
  papel em uma CTE `AS MATERIALIZED` partindo de dim_people filtrado por tipo,
  depois faça o join pelas CTEs; o self-join direto na bridge é lento demais.
- Retorne colunas legíveis (titulo, nome_pessoa, nome_genero...), nunca só sk_*.
  Inclua as métricas usadas no ranking e uma contagem (qtd_filmes) em agregações.
- Use ROUND(..., 2) em médias e razões. Use ORDER BY explícito e LIMIT (padrão 10)
  em rankings."""


@dataclass(frozen=True)
class FewShot:
    question: str
    sql: str
    premissas: tuple[str, ...] = ()


FEW_SHOTS: tuple[FewShot, ...] = (
    FewShot(
        question="Quais produtoras mais lançaram filmes de terror?",
        sql="""SELECT c.nome_produtora, COUNT(*) AS qtd_filmes
FROM dim_companies c
JOIN bridge_movie_company bc ON bc.sk_company_id = c.sk_company_id
JOIN bridge_movie_genre bg ON bg.sk_movie_id = bc.sk_movie_id
JOIN dim_genres g ON g.sk_genre_id = bg.sk_genre_id
WHERE g.nome_genero = 'Horror'
GROUP BY c.sk_company_id, c.nome_produtora
ORDER BY qtd_filmes DESC
LIMIT 10""",
        premissas=("Terror mapeado para o gênero 'Horror'.",),
    ),
    FewShot(
        question="Qual foi o lucro total por ano de lançamento?",
        sql="""SELECT m.ano_lancamento, COUNT(*) AS qtd_filmes,
       SUM(f.receita_usd - f.orcamento_usd) AS lucro_total_usd
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
WHERE f.receita_usd > 0 AND f.orcamento_usd > 0
GROUP BY m.ano_lancamento
ORDER BY m.ano_lancamento""",
        premissas=(
            "Lucro em USD considerando apenas filmes com receita e orçamento "
            "informados.",
        ),
    ),
    FewShot(
        question=(
            "Quantos filmes de ficção científica foram lançados nos últimos 3 anos?"
        ),
        sql="""WITH ref AS (
    SELECT MAX(ano_lancamento) AS ano_max FROM dim_movies
    WHERE status_filme = 'Lançado'
)
SELECT m.ano_lancamento, COUNT(*) AS qtd_filmes
FROM dim_movies m
JOIN bridge_movie_genre bg ON bg.sk_movie_id = m.sk_movie_id
JOIN dim_genres g ON g.sk_genre_id = bg.sk_genre_id
CROSS JOIN ref
WHERE g.nome_genero = 'Science Fiction'
  AND m.status_filme = 'Lançado'
  AND m.ano_lancamento > ref.ano_max - 3
GROUP BY m.ano_lancamento
ORDER BY m.ano_lancamento""",
        premissas=(
            "Período relativo ao ano mais recente com filmes lançados no banco.",
        ),
    ),
    FewShot(
        question=(
            "Quais filmes de ação têm a melhor nota no TMDB, "
            "com pelo menos 100 votos?"
        ),
        sql="""SELECT m.titulo, m.ano_lancamento, f.nota_tmdb, f.qtd_tmdb
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
JOIN bridge_movie_genre bg ON bg.sk_movie_id = f.sk_movie_id
JOIN dim_genres g ON g.sk_genre_id = bg.sk_genre_id
WHERE g.nome_genero = 'Action' AND f.qtd_tmdb >= 100
ORDER BY f.nota_tmdb DESC
LIMIT 10""",
    ),
    FewShot(
        question="Quais duplas de roteirista e diretor mais trabalharam juntas?",
        sql="""WITH diretores AS MATERIALIZED (
    SELECT bp.sk_movie_id, bp.sk_person_id
    FROM dim_people p
    JOIN bridge_movie_person bp ON bp.sk_person_id = p.sk_person_id
    WHERE p.tipo_pessoa = 'Diretor'
),
roteiristas AS MATERIALIZED (
    SELECT bp.sk_movie_id, bp.sk_person_id
    FROM dim_people p
    JOIN bridge_movie_person bp ON bp.sk_person_id = p.sk_person_id
    WHERE p.tipo_pessoa = 'Roteirista'
),
duplas AS (
    SELECT r.sk_person_id AS roteirista_id, d.sk_person_id AS diretor_id,
           COUNT(*) AS qtd_filmes
    FROM diretores d
    JOIN roteiristas r ON r.sk_movie_id = d.sk_movie_id
    GROUP BY r.sk_person_id, d.sk_person_id
    ORDER BY qtd_filmes DESC
    LIMIT 10
)
SELECT pr.nome_pessoa AS roteirista, pd.nome_pessoa AS diretor, du.qtd_filmes
FROM duplas du
JOIN dim_people pr ON pr.sk_person_id = du.roteirista_id
JOIN dim_people pd ON pd.sk_person_id = du.diretor_id
ORDER BY du.qtd_filmes DESC""",
        premissas=(
            "A mesma pessoa pode aparecer como roteirista e diretora (cadastros "
            "distintos por papel).",
        ),
    ),
)

_OUTPUT_CONTRACT = """\
Responda SOMENTE com um objeto JSON, sem markdown, no formato:
{"sql": "<uma única consulta SELECT para SQLite>", "premissas": ["..."]}
- "premissas": filtros e interpretações que o usuário de negócio precisa saber.
- Se a pergunta pedir alteração de dados (inserir, apagar, atualizar...) ou não
  puder ser respondida com este banco, responda
  {"sql": null, "premissas": [], "erro": "<explicação curta em português>"}."""


def _few_shot_block() -> str:
    blocks = []
    for shot in FEW_SHOTS:
        answer = json.dumps(
            {"sql": shot.sql, "premissas": list(shot.premissas)}, ensure_ascii=False
        )
        blocks.append(f"Pergunta: {shot.question}\nResposta: {answer}")
    return "\n\n".join(blocks)


SQL_SYSTEM_PROMPT = f"""\
Você é um analista de dados sênior da CineData Analytics. Converta perguntas de \
negócio em português em UMA consulta SQLite somente-leitura sobre a camada Gold.

## Schema
{render_schema()}

## Regras de negócio
{BUSINESS_RULES}

## Formato de saída
{_OUTPUT_CONTRACT}

## Exemplos
{_few_shot_block()}"""


def build_sql_messages(
    question: str,
    previous_sql: str | None = None,
    error: str | None = None,
) -> list[dict[str, str]]:
    """Mensagens para a geração de SQL; inclui o erro anterior em caso de retry."""
    messages = [
        {"role": "system", "content": SQL_SYSTEM_PROMPT},
        {"role": "user", "content": f"Pergunta: {question}"},
    ]
    if previous_sql is not None and error is not None:
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps({"sql": previous_sql}, ensure_ascii=False),
            }
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    f"A consulta falhou com o erro: {error}\n"
                    "Corrija e responda novamente no mesmo formato JSON."
                ),
            }
        )
    return messages


SUMMARY_SYSTEM_PROMPT = """\
Você é um consultor de negócios da CineData Analytics. Escreva um resumo \
executivo em português para líderes não técnicos, a partir do resultado de uma \
consulta.
- 2 a 4 frases diretas, destacando o principal achado e números-chave.
- Use SOMENTE os números presentes no resultado; nunca invente ou extrapole.
- Valores monetários: US$ ou R$ conforme a coluna, com separador de milhar.
- Cite as premissas relevantes e, se o resultado estiver truncado, avise.
- Se algum valor parecer anômalo (ex.: orçamento de poucos dólares, nota com 1
  voto), sinalize como possível problema de qualidade do dado.
- Não mostre SQL nem nomes técnicos de colunas."""


def _rows_as_text(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], max_rows: int
) -> str:
    header = " | ".join(columns)
    body = "\n".join(" | ".join(str(v) for v in row) for row in rows[:max_rows])
    return f"{header}\n{body}" if body else f"{header}\n(sem linhas)"


def build_summary_messages(
    question: str,
    premissas: Sequence[str],
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    truncated: bool,
) -> list[dict[str, str]]:
    """Mensagens para o resumo executivo (até `SUMMARY_MAX_ROWS` linhas)."""
    shown = min(len(rows), SUMMARY_MAX_ROWS)
    note = (
        f"Resultado truncado: exibindo {shown} linhas."
        if truncated or len(rows) > SUMMARY_MAX_ROWS
        else f"{shown} linhas."
    )
    premissas_txt = "\n".join(f"- {p}" for p in premissas) or "- (nenhuma)"
    content = (
        f"Pergunta: {question}\n\nPremissas:\n{premissas_txt}\n\n"
        f"Resultado ({note}):\n{_rows_as_text(columns, rows, SUMMARY_MAX_ROWS)}"
    )
    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]
