# Checklist de Producao 24/7 - RIS

## Antes de publicar

- [ ] Repositorio privado no GitHub.
- [ ] `.env` fora do Git.
- [ ] `ENV=production`.
- [ ] `SEED_DEMO_DATA=false`.
- [ ] `SECRET_KEY` forte e exclusiva.
- [ ] `JWT_SECRET` forte e exclusiva.
- [ ] `MASTER_PASSWORD` trocada.
- [ ] Banco PostgreSQL criado.
- [ ] `DATABASE_URL` configurada.
- [ ] `ORS_API_KEY` configurada.
- [ ] Dominio escolhido.
- [ ] HTTPS ativo.

## Servidor

- [ ] Web service Python ativo.
- [ ] Start command usando Gunicorn.
- [ ] Health check apontando para `/healthz`.
- [ ] Logs ativados no provedor.
- [ ] Reinicio automatico em caso de falha.

## Banco de dados

- [ ] PostgreSQL gerenciado.
- [ ] Backup automatico diario.
- [ ] Retencao minima de 7 dias.
- [ ] Teste de restauracao feito.
- [ ] Acesso ao banco restrito.

## Monitoramento

- [ ] Monitor externo consultando `https://seu-dominio.com/healthz`.
- [ ] Alerta por email ou WhatsApp.
- [ ] Alerta de uso alto de CPU/RAM.
- [ ] Alerta de erro 5xx.
- [ ] Alerta de falha de backup.

## Alta disponibilidade

- [ ] Cloudflare configurado no DNS.
- [ ] Segunda instancia da aplicacao planejada.
- [ ] Plano do banco com replica ou standby avaliado.
- [ ] Procedimento de rollback documentado.
- [ ] Backup externo mensal.

## Operacao

- [ ] Criar empresa real.
- [ ] Criar usuarios por camada: operacional, gerencia, CEO/proprietario e master.
- [ ] Criar motoristas por rota.
- [ ] Validar que cada empresa ve somente seus dados.
- [ ] Validar smartphone do motorista em rede 4G/5G.
- [ ] Validar mapa e OpenRouteService em producao.
