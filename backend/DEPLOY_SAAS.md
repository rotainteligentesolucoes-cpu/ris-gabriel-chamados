# RIS como SaaS

## Objetivo

Publicar o RIS em um servidor seu, para que clientes e motoristas acessem apenas por login e senha, sem receber o codigo-fonte.

Tambem deixar o sistema preparado para operacao 24/7, com banco PostgreSQL, backups, monitoramento e caminho de alta disponibilidade.

## Modelo correto

- Voce hospeda o sistema.
- Cliente acessa por navegador.
- Cliente nao recebe a pasta do projeto.
- Cada empresa fica isolada por `company_id`.
- Motorista acessa somente a propria rota.

## Opcoes de hospedagem

### Opcao recomendada para comecar: Render ou Railway

1. Criar conta no Render ou Railway.
2. Criar um banco PostgreSQL.
3. Subir o codigo em um repositorio privado no GitHub.
4. Criar um Web Service apontando para a pasta `backend`.
5. Configurar variaveis de ambiente.
6. Usar o comando:

```bash
gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
```

## Variaveis obrigatorias

Use o arquivo `.env.production.example` como modelo.

Obrigatorias:

```text
ENV=production
SECRET_KEY=...
JWT_SECRET=...
DATABASE_URL=postgresql://...
ORS_API_KEY=...
MASTER_EMAIL=rotainteligentesolucoes@gmail.com
MASTER_PASSWORD=...
SEED_DEMO_DATA=false
```

## Banco de dados

Para SaaS real, use PostgreSQL. SQLite serve apenas para teste local.

## Seguranca

- Nunca entregue o codigo-fonte ao cliente.
- Nunca publique com `debug=True`.
- Nunca use senha padrao em producao.
- Use HTTPS.
- Use repositorio privado.
- Mantenha `.env` fora do Git.
- Crie backups diarios do banco.

## Acesso fora da rede

Para motoristas acessarem fora do Wi-Fi, use uma destas opcoes:

1. Servidor em nuvem com dominio e HTTPS.
2. Cloudflare Tunnel apontando para o servidor local.
3. Ngrok apenas para testes, nao como solucao definitiva.

## Fluxo de acesso

Master:

```text
rotainteligentesolucoes@gmail.com
```

Motorista:

```text
/motorista
```

Empresa:

```text
/dashboard
```

## Proximo passo

Escolher onde hospedar:

- Render
- Railway
- VPS Hostinger/DigitalOcean
- AWS/GCP/Azure

Depois disso, configurar dominio e HTTPS.

## Arquitetura recomendada 24/7

### Fase 1 - Producao inicial

Use esta fase para colocar o sistema no ar de forma profissional:

- Web Service com Python + Gunicorn.
- Banco PostgreSQL gerenciado.
- HTTPS automatico.
- Dominio proprio, exemplo `app.rotainteligentesolucoes.com.br`.
- Backups automaticos diarios do banco.
- Monitoramento externo consultando `/healthz`.
- Repositorio GitHub privado.

### Fase 2 - Mais estabilidade

Quando houver mais clientes e motoristas simultaneos:

- Plano de servidor com mais CPU/RAM.
- 2 ou mais instancias da aplicacao.
- Banco PostgreSQL com plano maior e backup PITR, quando disponivel.
- Monitoramento com alerta por email/WhatsApp.
- Logs centralizados.
- Cloudflare na frente do dominio.

### Fase 3 - Alta disponibilidade

Para reduzir queda ao maximo:

- Load balancer.
- Aplicacao rodando em mais de uma instancia.
- Banco PostgreSQL com standby/replica/failover.
- Backups fora do provedor principal.
- Procedimento de restauracao testado.
- Deploy com rollback.

## Health check

O sistema possui rota de monitoramento:

```text
/healthz
```

Ela retorna `200` quando a aplicacao e o banco estao respondendo.

Configure o provedor ou uma ferramenta externa para testar:

```text
https://seu-dominio.com/healthz
```

Se retornar erro, o monitoramento deve avisar imediatamente.

## O que contratar/configurar

Minimo para iniciar:

- 1 Web Service Python.
- 1 banco PostgreSQL gerenciado.
- Dominio proprio.
- HTTPS.
- Backup diario.
- Monitoramento externo.

Recomendado para varios clientes:

- 2 Web Services ou plano com autoscale.
- PostgreSQL com backup automatico e retencao minima de 7 a 30 dias.
- Cloudflare DNS/WAF.
- Ambiente separado de teste/homologacao.
- Politica de troca de senha master.

## Variaveis de ambiente em producao

Obrigatorias:

```text
ENV=production
SEED_DEMO_DATA=false
SECRET_KEY=...
JWT_SECRET=...
DATABASE_URL=postgresql://...
ORS_API_KEY=...
MASTER_EMAIL=rotainteligentesolucoes@gmail.com
MASTER_PASSWORD=...
MASTER_NAME=Rota Inteligente Solucoes
```

## Comando de start

Use:

```bash
gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
```

Em servidores maiores, aumente workers conforme CPU/RAM.

## Cuidados importantes

- Nunca use SQLite em producao.
- Nunca use `debug=True` em producao.
- Nunca suba o arquivo `.env` para GitHub.
- Nunca entregue o codigo-fonte para clientes.
- Troque a senha master antes de publicar.
- Teste restauracao de backup antes de vender para muitos clientes.
