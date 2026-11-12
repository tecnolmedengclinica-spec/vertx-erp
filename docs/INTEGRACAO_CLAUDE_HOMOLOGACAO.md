# Integração VERTYX — revisão local de homologação

## Referências preservadas

- Branch de trabalho: `codex/integracao-claude-homologacao`.
- Base: `72bb0b746465653485a79c90b140157d9d5292a0` (`codex/homologacao-r2`).
- Entrega importada: `e96c51ed5049d3f03230aa0ae761c9c29ae3cd17`, branch
  `backup/saude-licitacoes-checkpoint` do repositório `Gui2761/vertx-erp`.
- `main` e a branch original de homologação foram preservadas.
- A consolidação é seletiva, sem incorporar como pai o histórico externo que
  contém scripts com credenciais fixas. Os commits de origem continuam preservados.

## Correções da integração

- Sincronizações fiscais NFC-e/NFS-e bloqueiam somente as linhas base. Foram
  removidos joins opcionais da consulta com `FOR UPDATE`, incompatíveis com PostgreSQL.
- As novas telas de Saúde exigem autenticação e a permissão municipal correspondente.
  Personalização exige administração com escopo de Prefeitura, validação de entrada,
  transação e auditoria. Não existe fallback para a primeira organização ativa.
- Identidade visual armazenada na sessão fica vinculada à organização selecionada.
- Homologação bloqueia SMTP, chamadas bancárias de produção e transportes fiscais
  ADN/NF-e/NFC-e explicitamente, incluindo chamadas que contornavam os bloqueios anteriores.
- Testes de navegação verificam links conforme as permissões. O simulador DOM dos
  testes JavaScript contempla o tema visual novo e preserva o teste de troca de tenant.
- Dependências de testes estão incluídas na instalação da CI.
- Espaços em branco da entrega importada foram normalizados.
- Scripts de população com credenciais fixas e relatórios sensíveis ficam fora da
  consolidação e do contexto Docker; os arquivos locais foram preservados.

## Banco e migrations

Somente o PostgreSQL 16.15 descartável, em `tmp/claude_pg_utf8`, porta `55438`,
é utilizado. Bancos separados recebem cópias do schema com dados iniciais de migrations.
Os testes transacionais recebem cópias novas para não remover os catálogos dos módulos seguintes.
Conexões externas dos processos de testes são bloqueadas; arquivos de teste são isolados.

O schema tem 142 migrations, 359 tabelas, nenhum índice inválido e nenhuma constraint
sem validação. Não existem operações pendentes no plano desse banco descartável.
Nenhuma migration histórica foi modificada. Foram incorporadas cinco migrations novas:

| Migration | Dependências principais |
| --- | --- |
| `platform_core.0025` | `platform_core.0024` |
| `platform_core.0026` | `platform_core.0025`, modelo de usuário |
| `operacao.0029` | `operacao.0028`, `platform_core.0024` |
| `operacao.0030` | `operacao.0029`, `platform_core.0026`, `gestao.0017`, modelo de usuário |
| `rh_municipal.0017` | `rh_municipal.0016`, `platform_core.0025` |

Consulta remota exclusivamente de leitura confirmou `vertyx_testes`, PostgreSQL 16.15,
fora de recuperação, com as 137 migrations anteriores: operacao.0028,
platform_core.0024 e rh_municipal.0016. Essas cinco migrations novas exigem autorização
separada, backup verificado e plano conferido no destino antes da publicação.
Não foi realizada escrita no banco remoto. Nenhuma das cinco migrations novas contém
RunPython ou RunSQL; elas criam estruturas e alteram campos, preservando as tabelas anteriores.

## Validação

Resultados por teste e módulo ficam em `tmp/integration_review/final/`, sem versionamento.
Tentativas anteriores de preparação foram preservadas separadamente e não equivalem
a uma execução final aprovada. A bateria extensa foi interrompida por solicitação
do proprietário, priorizando a preparação da publicação, sem retomada da suíte completa.

O último conjunto preservado tem 587 casos distintos concluídos: 569 aprovações,
17 falhas e um erro. Não é uma certificação de toda a plataforma. Doze falhas de
assertLogs ocorreram porque o executor auxiliar desabilitou logging globalmente;
um erro de subprocesso foi causado pelo módulo auxiliar fora do PYTHONPATH.
Os resultados brutos permanecem preservados, sem reclassificá-los como aprovações.

- `manage.py check`: aprovado.
- `makemigrations --check --dry-run`: sem alterações de modelos pendentes.
- `migrate --plan`, banco descartável: sem operações pendentes.
- `check --deploy`: sem erros; avisos W005/W021 de HSTS dependem da política do domínio.
- `pip check`: nenhuma dependência quebrada no ambiente local.
- Testes offline de preparação/inicialização: 25 aprovados, incluindo verificações
  de ferramentas administrativas locais que permanecem fora do commit.
- Testes JavaScript de PDV offline/tema: 12 aprovados.
- Verificação final curta: cinco testes de bloqueio SMTP/bancário/fiscal aprovados,
  sem banco; sete testes de proteção das telas municipais aprovados em PostgreSQL
  descartável, incluindo consultas com lock e isolamento por organização. A fixture
  desses sete testes foi tornada independente de catálogos deixados por testes anteriores.

## Pendências adiadas

| Pendência | Evidência e próximo passo |
| --- | --- |
| PDF e reabertura de OS | Dois testes retornam 409; diagnóstico local encontrou FileNotFoundError em caminho extenso do Windows. Confirmar PDF no Linux/R2 antes de liberar esse fluxo. |
| Contrato de resposta de rota bloqueada | Um teste esperava 403/404 e recebeu 405; não houve sucesso de acesso. Alinhar contrato e teste. |
| Diagnóstico de logs/R2 | Repetir os casos afetados pelo logging desabilitado em executor normal; uma asserção adicional de diagnóstico também permanece pendente. |
| Upgrade histórico em subprocesso | Corrigir configuração do executor auxiliar, sem alterar migrations históricas. |
| Consulta municipal com FOR UPDATE e DISTINCT | Implementado filtro por subconsulta e aprovado teste curto de lock/isolamento; o caso completo da Fase E não foi repetido. |
| Relatório executivo e licença | Um teste de entitlement esperava 403 e recebeu 200 para administrador da própria organização; revisar regra comercial antes de afirmar homologação desse recurso. |

## Limitações e condições antes da publicação

- Aprovação automatizada não certifica integrações fiscais, bancárias ou SUS oficiais.
  Os fluxos simulados não comprovam transmissão ou recebimento junto aos provedores.
- As telas novas de fila/TV/totem, ACS, triagem e PEC visual possuem componentes
  demonstrativos; não substituem os serviços clínicos existentes nem comprovam fluxos completos.
- O teste real do R2 e a inspeção de acesso das contas na aplicação publicada precisam
  ser repetidos após uma publicação autorizada. Testes locais usam arquivos e provedores sintéticos.
- As credenciais de teste expostas não são publicadas nesta consolidação e não foram
  redefinidas. Contas com essas credenciais não podem ser expostas publicamente;
  é necessário controle de acesso ao ambiente e rotação antes da produção.
- A senha SMTP ainda usa campo em texto: migrar para referência segura ou cifragem,
  definir recuperação/rotação, sanitizar falhas e validar TLS é pendência obrigatória
  de produção. O bloqueio de envio na homologação não resolve o armazenamento desse segredo.
- Inspeção somente de leitura confirmou no Render branch `codex/homologacao-r2`,
  auto-deploy desativado e
  Docker Command `/bin/sh /app/scripts/start-homologation.sh`. O CMD Docker padrão
  executa migrations; o entrypoint específico de homologação executa apenas Gunicorn.
- O serviço `tecnolmed-erp` continua em `main`, sem alterações. A nova branch de
  integração ainda precisa ser selecionada exclusivamente em `vertyx-erp` após autorização.
- Preservar `DJANGO_SETTINGS_MODULE=tecnolmed.homologation_settings` e o seletor R2
  existente. Não alterar nem divulgar as credenciais de armazenamento.
- Valores das variáveis no painel estão mascarados: presença dos nomes foi confirmada,
  mas o vínculo DATABASE_URL e os valores ativos das settings devem ser reconfirmados
  de forma sanitizada antes do deploy. Não revelar nem substituir segredos.
- Publicação proposta: conferir destino, backup e cinco migrations sob autorização
  separada; selecionar branch integrada e SHA aprovado, manter auto-deploy desativado,
  iniciar somente Gunicorn e validar health/login/SaaS/R2 com dados fictícios.
- Rollback: republicar o SHA anterior `72bb0b746465653485a79c90b140157d9d5292a0`,
  preservando schema e dados; não reverter migrations automaticamente. Restaurar backup
  somente se necessário e mediante autorização específica.
- Este relatório não autoriza deploy, migrations remotas ou alterações de contas.
