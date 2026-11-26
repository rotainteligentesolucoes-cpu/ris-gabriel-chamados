# RIS SaaS

Plataforma privada da Rota Inteligente Solucoes para roteirizacao, controle operacional, operacao mobile, mercadorias, comprovantes, relatorios e gestao SaaS multiempresa.

## Aviso de propriedade

Este repositorio e privado. O codigo, fluxos, telas, regras de negocio e estrutura da plataforma nao possuem licenca publica de uso, copia, distribuicao, revenda ou sublicenciamento.

## Deploy Render

Configuracao sugerida:

- Root Directory: `backend`
- Build Command: `pip install -r requirements.txt`
- Start Command: `python migrate.py && AUTO_INITIALIZE_DATABASE=false BACKUP_SCHEDULER_MODE=gunicorn gunicorn wsgi:app --config gunicorn.conf.py`
- Health Check Path: `/readyz`

Variaveis obrigatorias em producao:

- `ENV=production`
- `DATABASE_URL=postgresql://...`
- `SECRET_KEY=...`
- `JWT_SECRET=...`
- `MASTER_EMAIL=...`
- `MASTER_PASSWORD=...`
- `CORS_ORIGINS=https://app.rotainteligentesolucoes.com.br`

O comando de start executa criacao, migracoes e seeds uma unica vez antes dos workers. Uma trava do PostgreSQL elege somente um worker para o agendador de backup.
