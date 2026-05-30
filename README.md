# RIS SaaS

Plataforma privada da Rota Inteligente Solucoes para roteirizacao, controle operacional, operacao mobile, mercadorias, comprovantes, relatorios e gestao SaaS multiempresa.

## Aviso de propriedade

Este repositorio e privado. O codigo, fluxos, telas, regras de negocio e estrutura da plataforma nao possuem licenca publica de uso, copia, distribuicao, revenda ou sublicenciamento.

## Deploy Render

Configuracao sugerida:

- Root Directory: `backend`
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`
- Health Check Path: `/readyz`

Variaveis obrigatorias em producao:

- `ENV=production`
- `DATABASE_URL=postgresql://...`
- `SECRET_KEY=...`
- `JWT_SECRET=...`
- `MASTER_EMAIL=...`
- `MASTER_PASSWORD=...`
- `CORS_ORIGINS=https://app.rotainteligentesolucoes.com.br`
