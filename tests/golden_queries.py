"""Perguntas oficiais do case CineData Analytics com SQL gabarito.

Os gabaritos aplicam as regras de negócio do projeto (ver docs/dev_log.md):
lucro/margem exigem `receita > 0 AND orcamento > 0` (D1), margem exige ainda
`orcamento_usd >= 10000` (P1), divergências de nota exigem amostra mínima (P2) e
"últimos N anos" é relativo ao ano mais recente com filmes lançados (D2).

Estes casos NÃO são usados como few-shot em `src.agent.prompts`, para que a
suíte meça generalização e não memorização.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GoldenCase:
    id: str
    category: str
    question: str
    sql: str
    # Coluna do gabarito cujos valores devem aparecer em alguma coluna da resposta.
    key_column: int = 0
    # Distribuição completa ("por gênero", "por ano"): o agente deve trazer
    # todas as linhas do gabarito, não só um prefixo.
    full_result: bool = False


GOLDEN_CASES: tuple[GoldenCase, ...] = (
    # 1. Bilheteria e Finanças
    GoldenCase(
        id="fin_top10_receita_brl",
        category="Bilheteria e Finanças",
        question="Quais são os 10 filmes com maior receita em R$?",
        sql="""
SELECT m.titulo, m.ano_lancamento, f.receita_brl
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
WHERE f.receita_brl > 0
ORDER BY f.receita_brl DESC
LIMIT 10""",
    ),
    GoldenCase(
        id="fin_lucro_medio_genero",
        full_result=True,
        category="Bilheteria e Finanças",
        question=(
            "Qual o lucro médio por gênero, considerando apenas filmes com "
            "receita informada?"
        ),
        sql="""
SELECT g.nome_genero,
       COUNT(*) AS qtd_filmes,
       ROUND(AVG(f.receita_usd - f.orcamento_usd), 2) AS lucro_medio_usd
FROM fact_movies_performance f
JOIN bridge_movie_genre bg ON bg.sk_movie_id = f.sk_movie_id
JOIN dim_genres g ON g.sk_genre_id = bg.sk_genre_id
WHERE f.receita_usd > 0 AND f.orcamento_usd > 0
GROUP BY g.nome_genero
ORDER BY lucro_medio_usd DESC""",
    ),
    GoldenCase(
        id="fin_maior_margem",
        category="Bilheteria e Finanças",
        question=(
            "Quais filmes têm a maior margem de lucro, entre os que possuem "
            "receita e orçamento informados?"
        ),
        sql="""
SELECT m.titulo, f.orcamento_usd, f.receita_usd,
       ROUND((f.receita_usd - f.orcamento_usd) * 1.0 / f.orcamento_usd, 2)
           AS margem_lucro
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
WHERE f.receita_usd > 0 AND f.orcamento_usd >= 10000
ORDER BY margem_lucro DESC
LIMIT 10""",
    ),
    # 2. Popularidade e Engajamento
    GoldenCase(
        id="pop_top5_populares",
        category="Popularidade e Engajamento",
        question="Quais são os 5 filmes mais populares?",
        sql="""
SELECT m.titulo, m.ano_lancamento, f.popularidade
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
WHERE f.popularidade IS NOT NULL
ORDER BY f.popularidade DESC
LIMIT 5""",
    ),
    GoldenCase(
        id="pop_divergencia_tmdb_imdb",
        category="Popularidade e Engajamento",
        question=(
            "Quais filmes têm a maior divergência entre a nota TMDB e a nota IMDb?"
        ),
        sql="""
SELECT m.titulo, f.nota_tmdb, f.qtd_tmdb, f.nota_imdb, f.qtd_imdb,
       ROUND(ABS(f.nota_tmdb - f.nota_imdb), 2) AS divergencia
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
WHERE f.qtd_tmdb >= 50 AND f.qtd_imdb >= 50 AND f.nota_imdb IS NOT NULL
ORDER BY divergencia DESC
LIMIT 10""",
    ),
    GoldenCase(
        id="pop_imdb_por_ano",
        full_result=True,
        category="Popularidade e Engajamento",
        question="Qual a nota média IMDb por ano de lançamento?",
        sql="""
SELECT m.ano_lancamento,
       COUNT(*) AS qtd_filmes,
       ROUND(AVG(f.nota_imdb), 2) AS nota_media_imdb
FROM fact_movies_performance f
JOIN dim_movies m ON m.sk_movie_id = f.sk_movie_id
WHERE f.nota_imdb IS NOT NULL
GROUP BY m.ano_lancamento
ORDER BY m.ano_lancamento""",
    ),
    # 3. Elenco e Equipe
    GoldenCase(
        id="cast_ator_mais_filmes_5anos",
        category="Elenco e Equipe",
        question=(
            "Qual ator teve mais participações em filmes lançados nos "
            "últimos 5 anos?"
        ),
        sql="""
WITH ref AS (
    SELECT MAX(ano_lancamento) AS ano_max
    FROM dim_movies
    WHERE status_filme = 'Lançado'
)
SELECT p.nome_pessoa, COUNT(*) AS qtd_filmes
FROM dim_people p
JOIN bridge_movie_person bp ON bp.sk_person_id = p.sk_person_id
JOIN dim_movies m ON m.sk_movie_id = bp.sk_movie_id
CROSS JOIN ref
WHERE p.tipo_pessoa = 'Ator'
  AND m.status_filme = 'Lançado'
  AND m.ano_lancamento > ref.ano_max - 5
GROUP BY p.sk_person_id, p.nome_pessoa
ORDER BY qtd_filmes DESC
LIMIT 10""",
        # Vários atores empatam com 37 filmes logo após o líder: compara a contagem.
        key_column=1,
    ),
    GoldenCase(
        id="cast_diretores_maior_nota",
        category="Elenco e Equipe",
        question="Quais diretores têm a maior nota média (mínimo de 5 filmes)?",
        sql="""
SELECT p.nome_pessoa,
       COUNT(*) AS qtd_filmes,
       ROUND(AVG(f.nota_imdb), 2) AS nota_media_imdb
FROM dim_people p
JOIN bridge_movie_person bp ON bp.sk_person_id = p.sk_person_id
JOIN fact_movies_performance f ON f.sk_movie_id = bp.sk_movie_id
WHERE p.tipo_pessoa = 'Diretor' AND f.nota_imdb IS NOT NULL
GROUP BY p.sk_person_id, p.nome_pessoa
HAVING COUNT(*) >= 5
ORDER BY nota_media_imdb DESC
LIMIT 10""",
    ),
    GoldenCase(
        id="cast_dupla_ator_diretor",
        category="Elenco e Equipe",
        question="Qual dupla ator-diretor mais trabalhou junta?",
        # Padrão otimizado: filtrar pessoas por tipo ANTES do self-join na bridge
        # (7,8 mi de pares no join ingênuo; ~5s vs ~105s).
        sql="""
WITH diretores AS MATERIALIZED (
    SELECT bp.sk_movie_id, bp.sk_person_id
    FROM dim_people p
    JOIN bridge_movie_person bp ON bp.sk_person_id = p.sk_person_id
    WHERE p.tipo_pessoa = 'Diretor'
),
atores AS MATERIALIZED (
    SELECT bp.sk_movie_id, bp.sk_person_id
    FROM dim_people p
    JOIN bridge_movie_person bp ON bp.sk_person_id = p.sk_person_id
    WHERE p.tipo_pessoa = 'Ator'
),
duplas AS (
    SELECT a.sk_person_id AS ator_id, d.sk_person_id AS diretor_id,
           COUNT(*) AS qtd_filmes
    FROM diretores d
    JOIN atores a ON a.sk_movie_id = d.sk_movie_id
    GROUP BY a.sk_person_id, d.sk_person_id
    ORDER BY qtd_filmes DESC
    LIMIT 10
)
SELECT pa.nome_pessoa AS ator, pd.nome_pessoa AS diretor, du.qtd_filmes
FROM duplas du
JOIN dim_people pa ON pa.sk_person_id = du.ator_id
JOIN dim_people pd ON pd.sk_person_id = du.diretor_id
ORDER BY du.qtd_filmes DESC""",
        # Várias duplas empatam com 31 filmes: compara a contagem.
        key_column=2,
    ),
    # 4. Gêneros e Produtoras
    GoldenCase(
        id="gen_qtd_por_genero",
        full_result=True,
        category="Gêneros e Produtoras",
        question="Qual a quantidade de filmes por gênero?",
        sql="""
SELECT g.nome_genero, COUNT(*) AS qtd_filmes
FROM dim_genres g
JOIN bridge_movie_genre bg ON bg.sk_genre_id = g.sk_genre_id
GROUP BY g.nome_genero
ORDER BY qtd_filmes DESC""",
    ),
    GoldenCase(
        id="gen_produtora_maior_lucro",
        category="Gêneros e Produtoras",
        question="Qual produtora tem o maior lucro total?",
        sql="""
SELECT c.nome_produtora,
       COUNT(*) AS qtd_filmes,
       SUM(f.receita_usd - f.orcamento_usd) AS lucro_total_usd
FROM dim_companies c
JOIN bridge_movie_company bc ON bc.sk_company_id = c.sk_company_id
JOIN fact_movies_performance f ON f.sk_movie_id = bc.sk_movie_id
WHERE f.receita_usd > 0 AND f.orcamento_usd > 0
GROUP BY c.sk_company_id, c.nome_produtora
ORDER BY lucro_total_usd DESC
LIMIT 10""",
    ),
    GoldenCase(
        id="gen_maior_margem_media",
        category="Gêneros e Produtoras",
        question="Qual gênero tem a maior margem de lucro média?",
        sql="""
SELECT g.nome_genero,
       COUNT(*) AS qtd_filmes,
       ROUND(AVG((f.receita_usd - f.orcamento_usd) * 1.0 / f.orcamento_usd), 2)
           AS margem_media
FROM fact_movies_performance f
JOIN bridge_movie_genre bg ON bg.sk_movie_id = f.sk_movie_id
JOIN dim_genres g ON g.sk_genre_id = bg.sk_genre_id
WHERE f.receita_usd > 0 AND f.orcamento_usd >= 10000
GROUP BY g.nome_genero
ORDER BY margem_media DESC""",
        # O agente pode traduzir o nome do gênero (Music -> Música): compara a margem.
        key_column=2,
    ),
    # 5. Avaliações dos Usuários
    GoldenCase(
        id="rev_mais_avaliados",
        category="Avaliações dos Usuários",
        question="Quais filmes foram mais avaliados pelos usuários?",
        sql="""
SELECT m.titulo, r.qtd_avaliacoes_usuarios, r.nota_media_usuarios
FROM dim_reviews r
JOIN dim_movies m ON m.sk_movie_id = r.sk_movie_id
ORDER BY r.qtd_avaliacoes_usuarios DESC, r.nota_media_usuarios DESC
LIMIT 10""",
        # Títulos duplicados empatam na contagem: compara-se a contagem.
        key_column=1,
    ),
    GoldenCase(
        id="rev_divergencia_usuarios_imdb",
        category="Avaliações dos Usuários",
        question=(
            "Em quais filmes a nota média dos usuários mais diverge da nota IMDb?"
        ),
        sql="""
SELECT m.titulo, r.qtd_avaliacoes_usuarios, r.nota_media_usuarios, f.nota_imdb,
       ROUND(ABS(r.nota_media_usuarios - f.nota_imdb), 2) AS divergencia
FROM dim_reviews r
JOIN dim_movies m ON m.sk_movie_id = r.sk_movie_id
JOIN fact_movies_performance f ON f.sk_movie_id = r.sk_movie_id
WHERE r.qtd_avaliacoes_usuarios >= 3
  AND r.nota_media_usuarios IS NOT NULL
  AND f.qtd_imdb >= 50 AND f.nota_imdb IS NOT NULL
ORDER BY divergencia DESC
LIMIT 10""",
    ),
)
