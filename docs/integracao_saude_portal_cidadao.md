# Integração local de Saúde e Portal do Cidadão

Origem: `Gui2761/vertx-erp`, branch `backup/saude-licitacoes-checkpoint`,
commit `7572c6f7cc4f9d0d78071f58b6e4d655f9ef72d3`.
Base preservada: `35798655260eac12dd6ae62284ed5883ccfef313`.

## Escopo

Fila persistente, totem autenticado, painel de chamadas sem identificação de
pacientes, triagem, visitas territoriais, indicadores operacionais, leitos e
telemetria, portal do cidadão e prévias tributárias municipais.
Não constitui homologação oficial de protocolos clínicos, indicadores SUS,
equipamentos, gov.br, arrecadação bancária ou transmissão fiscal.

## Proteções da integração

- Preservados autenticação, formulário validado e auditoria da personalização,
  contexto por Prefeitura e bloqueios externos da homologação.
- Sem seleção automática da primeira Prefeitura ou de cidadão/profissional
  arbitrário na triagem. Profissional ativo vinculado ao usuário na triagem,
  telemetria autenticada e reconhecimento de alertas.
- Permissões de leitura não autorizam operações de escrita. Escopos dos novos
  registros reutilizam as regras municipais existentes.
- Painéis e APIs de saúde usam `no-store`; JSON inicial usa `json_script`.
- Queixa livre da nova triagem usa Fernet e a chave clínica existente; não há
  fallback em texto aberto quando a chave está ausente ou inválida.
- Gateway externo de monitores desabilitado por padrão. A futura habilitação
  exige provisionamento explícito de tokens (somente SHA-256 armazenado),
  validação de transporte/CSRF, rotação e ensaio com equipamento real.
- Simulação gov.br desabilitada por padrão, habilitável apenas em testes com
  `MUNICIPAL_GOVBR_DEMO_ENABLED=True`, `DEBUG=True` e host local/testserver.
  O callback oficial retorna 503 até existir troca de código e validação JWT.
  Identidade declarada em query string nunca autentica um cidadão.
- DAM do cidadão valida a titularidade do lançamento. Consulta pública de
  certidão exige código de autenticidade; não permite enumeração só por número.

## Migrations

- `0018_contribuintemunicipal_cnae_principal_and_more`: entrega original, sem
  alterações; depende de `rh_municipal.0017`, `platform_core.0026` e AUTH_USER.
- `0019_classificacaomanchester_queixa_principal_cifrada`: acrescenta o campo
  binário para a queixa. Não transforma nem elimina registros anteriores.
  Eventuais queixas legadas em texto precisam de inventário e tratamento
  posterior autorizado; não foram migradas automaticamente.
- `0020_atendimentosaudemunicipal_queixa_portal_cifrada`: conserva a queixa do
  agendamento em campo cifrado. A fila recebe somente a descrição operacional,
  sem texto clínico no detalhe de prioridade.

Nenhuma migration histórica é reescrita. Aplicação em bancos existentes exige
backup verificável, revisão do plano real e autorização separada. O startup de
homologação permanece sem execução automática de migrations.

## Validação e publicação

Executar os módulos `test_track_a_b_realtime`,
`test_realtime_integration_security`, `test_health_preview_security`,
`test_phase_e`, `test_phase_f`, `test_phase_g`, `test_primary_care` e
`test_clinical_record` exclusivamente com settings de testes e PostgreSQL
descartável. Executar também `check`, `makemigrations --check --dry-run`
e `git diff --check`.

Esta integração não modifica Render, credenciais, bancos existentes ou R2.
Commit, push, migrations remotas e deploy permanecem etapas separadas.

## Resultado da execução local

- PostgreSQL 16.15 novo, porta 55439, banco `test_saude_portal_integration`.
- 145 migrations aplicadas no banco descartável, zero pendentes e zero índices
  inválidos; verificadas as duas colunas de texto clínico cifrado.
- Regressão dos oito módulos: 121 testes aprovados, zero falhas e zero skips.
- Última rodada dos testes novos e afetados: 26 aprovados, zero falhas e zero
  skips. Há sobreposição de 22 testes entre as rodadas: 125 testes distintos.
- `manage.py check`, `makemigrations --check --dry-run` e `git diff --check`
  aprovados. Migrations históricas e settings existentes preservados.
- Cluster descartável encerrado normalmente após os testes; arquivos mantidos.
- Resultados e manifesto local em `tmp/saude_portal_integration/`, ignorado pelo
  Git. Nenhuma operação em Render, Cloudflare ou bancos existentes.

A validação acima é automatizada e direcionada. Revisão visual no ambiente
publicado, autenticação oficial gov.br, equipamentos físicos e validação das
regras oficiais de Saúde/arrecadação continuam pendentes.
