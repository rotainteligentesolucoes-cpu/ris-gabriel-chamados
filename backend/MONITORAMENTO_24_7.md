# Monitoramento 24/7 - RIS

## Endpoint oficial

Use este endpoint para monitoramento externo de producao:

```text
https://app.rotainteligentesolucoes.com.br/readyz
```

Resposta saudavel esperada:

```json
{"database":"ok","environment":"production","status":"ready"}
```

## Configuracao recomendada

Ferramentas indicadas:

- UptimeRobot
- Better Stack
- Grafana Cloud Synthetic Monitoring

Configuracao:

- Tipo: HTTP(s) ou API monitor.
- Metodo: GET.
- URL: `https://app.rotainteligentesolucoes.com.br/readyz`.
- Intervalo: 5 minutos.
- Timeout: 30 segundos.
- Alerta: email principal da operacao e, se disponivel, WhatsApp/SMS.

Validacoes:

- HTTP status code igual a `200`.
- Corpo contem `"database":"ok"`.
- Corpo contem `"environment":"production"`.
- Corpo contem `"status":"ready"`.

## Render

No painel da Render, abra o servico da RIS e acesse:

```text
Environment > Environment Variables
```

Adicione:

```text
UPTIME_MONITOR_URL=https://app.rotainteligentesolucoes.com.br/readyz
```

Depois salve usando uma opcao que aplique a variavel em producao:

- Save and deploy, ou
- Save, rebuild, and deploy.

Use `/readyz` tambem como Health Check Path do servico.

## UptimeRobot rapido

Link pre-preenchido para criar o monitor por e-mail:

```text
https://uptimerobot.com/quick-start?url=https%3A%2F%2Fapp.rotainteligentesolucoes.com.br%2Freadyz&email=rotainteligentesolucoes%40gmail.com
```

Ao abrir o link, o UptimeRobot prepara a solicitacao e envia um e-mail para confirmacao. O monitor so fica ativo depois de clicar no e-mail e confirmar.

## Observacao sobre Codex

O monitor interno do Codex foi pausado porque o ambiente local apresentou falhas TLS/Schannel ao consultar HTTPS externo e gerou alertas inconclusivos. A RIS foi confirmada saudavel pelo navegador em producao.

Para SLA real, use monitor externo independente do computador local.

## Rotas de saude

- `/healthz`: confirma que a aplicacao responde.
- `/readyz`: confirma aplicacao e banco. Esta e a rota correta para Render e monitoramento 24/7.
