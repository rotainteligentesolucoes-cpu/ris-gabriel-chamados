# Base SaaS Profissional - RIS

Este documento organiza o que precisa estar ativo antes de vender a plataforma em escala.

## Itens tecnicos obrigatorios

1. Banco PostgreSQL em cloud
   - Definir provedor: Render, Railway, DigitalOcean, AWS RDS, Azure Database ou outro.
   - Configurar `DATABASE_URL=postgresql://...`.
   - Migrar dados do SQLite local para PostgreSQL.

2. Backup automatico diario
   - Ativar backup nativo do provedor do PostgreSQL.
   - Manter retencao minima de 7 a 30 dias.
   - Configurar exportacao periodica fora do servidor principal.

3. Ambiente separado
   - Desenvolvimento: maquina local.
   - Producao: servidor cloud.
   - Variavel obrigatoria em producao: `ENV=production`.

4. Segredos de producao
   - Trocar `SECRET_KEY`.
   - Trocar `JWT_SECRET`.
   - Trocar `MASTER_PASSWORD`.
   - Nunca salvar senhas reais em arquivo publico.

5. Logs e auditoria
   - Tabela `audit_logs` criada.
   - Registrar login, aceite de termos, mudancas de assinatura e futuras acoes sensiveis.
   - Proximo passo: registrar alteracoes de clientes, rotas, mercadorias, usuarios e exportacoes.

6. Monitoramento 24/7
   - Health check publico: `/healthz`.
   - Readiness oficial com banco: `https://app.rotainteligentesolucoes.com.br/readyz`.
   - Configurar monitor externo apontando para `https://app.rotainteligentesolucoes.com.br/readyz`.
   - Validar resposta: `database=ok`, `environment=production`, `status=ready`.

7. Controle comercial
   - Tabelas criadas:
     - `subscription_plans`
     - `company_subscriptions`
   - Tela master:
     - `/web/saas-producao`

8. Contrato e termos
   - Termos tecnicos ativos em `/termos-de-uso`.
   - Aceite registrado no usuario.
   - Revisao juridica recomendada antes da venda em escala.

9. Instancia cloud sem hibernacao
   - HTTPS e dominio customizado nao impedem hibernacao do plano Free.
   - Para operacao 24/7 com cliente pagante, usar Instance Type pago no web service `ris-saas`.
   - Na Render, conferir se o servico esta em Starter ou superior antes do piloto comercial.

## Ordem recomendada para ir a mercado

1. Finalizar ambiente de producao em cloud.
2. Ativar PostgreSQL gerenciado.
3. Configurar backup automatico.
4. Configurar monitoramento externo 24/7.
5. Trocar o web service da Render para instancia sem hibernacao.
6. Criar planos comerciais reais.
7. Fechar contrato/termos com advogado.
8. Rodar piloto com 1 cliente real.
9. Ajustar precificacao, suporte e onboarding.

## Piloto com 1 cliente real

Objetivo: provar a operacao completa da RIS com dados reais antes da venda em escala.

- Cadastrar a empresa cliente real.
- Criar base/unidade e usuarios de operador, gestor e motorista.
- Cadastrar pontos reais com coordenadas, janela de atendimento e prioridade.
- Gerar uma rota oficial com data definida.
- Testar a execucao no celular do motorista em rede 4G/5G.
- Salvar relatorio final e registrar feedback do cliente.
- Manter UptimeRobot/Better Stack/Grafana validando `/readyz` durante o piloto.
