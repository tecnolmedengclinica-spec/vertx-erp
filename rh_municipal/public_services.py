"""Authorization and contract services for the Public Management ERP core."""
from datetime import timedelta
import secrets

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from platform_core.access import has_erp_access, membership_for
from platform_core.context import current_organization
from platform_core.modules import product_module_enabled
from platform_core.models import MembershipERP, OrganizationMembership, PlatformAudit

from .models import (
    AcessoMunicipal, ConviteMunicipal, PermissaoMunicipal, Secretaria, PRODUCT_CODE,
    PUBLIC_PERMISSION_CODES,
)

PUBLIC_PERMISSION_LABELS = {
    'dashboard.view': 'Visualizar painel municipal',
    'executive.dashboard.view': 'Consultar painel executivo municipal',
    'structure.view': 'Consultar estrutura',
    'structure.manage': 'Gerenciar estrutura',
    'access.view': 'Consultar acessos municipais',
    'access.manage': 'Gerenciar acessos municipais',
    'invite.create': 'Gerar convites',
    'invite.revoke': 'Revogar convites',
    'servers.view': 'Consultar profissionais/servidores',
    'servers.manage': 'Gerenciar profissionais/servidores',
    'rh.access': 'Acessar RH Municipal',
    'profile.manage': 'Configurar perfil da Prefeitura',
    'planning.view': 'Consultar Planejamento Público',
    'planning.manage': 'Gerenciar Planejamento Público',
    'budget.view': 'Consultar Orçamento Público',
    'budget.manage': 'Gerenciar Orçamento Público',
    'accounting.view': 'Consultar fundação Contábil',
    'accounting.manage': 'Gerenciar fundação Contábil',
    'treasury.view': 'Consultar fundação de Tesouraria',
    'treasury.manage': 'Gerenciar fundação de Tesouraria',
    'compras.view': 'Consultar Compras Públicas',
    'compras.manage': 'Gerenciar solicitações de compra',
    'compras.approve': 'Aprovar ou rejeitar solicitações',
    'cotacao.view': 'Consultar pesquisas de preço',
    'cotacao.manage': 'Gerenciar pesquisas de preço',
    'licitacao.view': 'Consultar processos e licitações',
    'licitacao.manage': 'Gerenciar processos e editais',
    'licitacao.judge': 'Registrar julgamento e classificação',
    'licitacao.homologate': 'Registrar adjudicação e homologação',
    'contract.view': 'Consultar contratos e atas',
    'contract.manage': 'Gerenciar contratos e atas',
    'contract.approve': 'Aprovar contratos e aditivos',
    'contract.execute': 'Registrar execução e fiscalização',
    'contract.documents': 'Gerenciar documentos contratuais',
    'price_registry.view': 'Consultar Registro de Preços',
    'price_registry.manage': 'Gerenciar atas e utilizações',
    'rh.view': 'Consultar RH Municipal',
    'rh.manage': 'Gerenciar cadastros funcionais',
    'rh.documents': 'Consultar documentos funcionais',
    'rh.approve': 'Aprovar solicitações de RH',
    'ponto.view': 'Consultar registros de ponto',
    'ponto.register': 'Registrar o próprio ponto',
    'ponto.adjust': 'Solicitar ou registrar ajuste autorizado',
    'ponto.approve': 'Aprovar justificativas e horas extras',
    'ponto.close': 'Aprovar e fechar período de ponto',
    'ponto.reopen': 'Reabrir período de ponto',
    'ponto.report': 'Consultar espelhos e relatórios de ponto',
    'folha.view': 'Consultar fundação da folha',
    'folha.manage': 'Gerenciar fundação da folha',
    'servidor.portal': 'Acessar o próprio portal do servidor',
    'patrimonio.view': 'Consultar patrimônio municipal',
    'patrimonio.manage': 'Cadastrar e atualizar patrimônio',
    'patrimonio.transfer': 'Movimentar patrimônio',
    'patrimonio.inventory': 'Conferir inventário patrimonial',
    'patrimonio.writeoff': 'Registrar baixa patrimonial',
    'patrimonio.report': 'Consultar relatórios patrimoniais',
    'almoxarifado.view': 'Consultar almoxarifado',
    'almoxarifado.manage': 'Gerenciar depósitos e itens',
    'almoxarifado.request': 'Solicitar materiais',
    'almoxarifado.approve': 'Aprovar requisições de material',
    'almoxarifado.issue': 'Separar e entregar materiais',
    'almoxarifado.transfer': 'Transferir estoque entre depósitos',
    'almoxarifado.inventory': 'Conferir inventário de estoque',
    'frotas.view': 'Consultar frotas',
    'frotas.manage': 'Gerenciar veículos e condutores',
    'frotas.dispatch': 'Registrar viagens e retornos',
    'frotas.fuel': 'Registrar abastecimentos',
    'frotas.maintenance': 'Gerenciar manutenções',
    'frotas.documents': 'Gerenciar documentos da frota',
    'frotas.report': 'Consultar relatórios de frota',
    'protocol.view': 'Consultar protocolos municipais',
    'protocol.manage': 'Registrar protocolos',
    'protocol.assign': 'Distribuir protocolos',
    'process.view': 'Consultar processos digitais',
    'process.manage': 'Autuar e atualizar processos',
    'process.dispatch': 'Tramitar processos',
    'process.analyze': 'Registrar despachos e pareceres',
    'process.close': 'Concluir e arquivar processos',
    'process.signature': 'Gerenciar solicitações de assinatura',
    'process.documents': 'Consultar documentos de processo',
    'process.restricted': 'Consultar processos restritos',
    'process.sensitive': 'Consultar processos sensíveis',
    'transparency.view': 'Consultar publicações de transparência',
    'transparency.manage': 'Preparar dados públicos para revisão',
    'transparency.publish': 'Revisar, publicar ou retirar dados públicos',
    'ouvidoria.view': 'Consultar manifestações da Ouvidoria',
    'ouvidoria.manage': 'Classificar e encaminhar manifestações',
    'ouvidoria.respond': 'Responder e encerrar manifestações',
    'esic.view': 'Consultar pedidos E-SIC',
    'esic.manage': 'Classificar e encaminhar pedidos E-SIC',
    'esic.respond': 'Responder pedidos E-SIC',
    'esic.appeal': 'Decidir recursos E-SIC',
    'contribuinte.view': 'Consultar contribuintes municipais',
    'contribuinte.manage': 'Gerenciar contribuintes municipais',
    'imovel.view': 'Consultar cadastro imobiliário territorial',
    'imovel.manage': 'Gerenciar cadastro imobiliário territorial',
    'tributo.view': 'Consultar lançamentos e regras tributárias',
    'tributo.manage': 'Gerenciar regras e lançamentos parametrizados',
    'arrecadacao.view': 'Consultar arrecadação municipal',
    'arrecadacao.manage': 'Registrar recebimentos manuais auditados',
    'divida.view': 'Consultar inscrições em dívida ativa',
    'divida.manage': 'Gerenciar inscrições em dívida ativa',
    'nfse.view': 'Consultar rascunhos NFS-e municipais',
    'nfse.manage': 'Preparar rascunhos NFS-e municipais',
    'saude.dashboard.view': 'Consultar indicadores agregados de saúde',
    'saude.primary.view': 'Consultar Atenção Básica e cobertura das UBS',
    'saude.primary.manage': 'Cadastrar famílias e vínculos territoriais nas UBS',
    'saude.units.view': 'Consultar unidades de saúde',
    'saude.units.manage': 'Gerenciar configuração de unidades de saúde',
    'saude.teams.view': 'Consultar equipes de saúde',
    'saude.teams.manage': 'Gerenciar equipes de saúde',
    'saude.professionals.view': 'Consultar profissionais de saúde',
    'saude.professionals.manage': 'Gerenciar vínculos de profissionais',
    'saude.patients.view': 'Consultar cadastros de cidadãos/pacientes',
    'saude.patients.manage': 'Gerenciar cadastros de cidadãos/pacientes',
    'saude.records.view': 'Consultar atendimentos',
    'saude.records.manage': 'Gerenciar atendimentos',
    'saude.records.sensitive': 'Acessar conteúdo clínico sensível com auditoria',
    'saude.records.create': 'Criar atendimentos e registros clínicos',
    'saude.records.edit': 'Retificar registros e concluir atendimentos',
    'saude.appointments.view': 'Consultar agendas de saúde',
    'saude.appointments.manage': 'Gerenciar agendas e atendimentos',
    'saude.field.view': 'Consultar visitas de campo',
    'saude.field.manage': 'Registrar visitas ACS/ACE',
    'saude.pharmacy.view': 'Consultar farmácia e lotes',
    'saude.pharmacy.manage': 'Gerenciar catálogo e lotes da farmácia',
    'saude.dispense': 'Registrar dispensação de medicamentos',
    'saude.sanitary.view': 'Consultar vigilância sanitária',
    'saude.sanitary.manage': 'Gerenciar inspeções sanitárias',
    'saude.epidemiology.view': 'Consultar notificações epidemiológicas restritas',
    'saude.epidemiology.manage': 'Gerenciar notificações epidemiológicas restritas',
    'saude.regulation.view': 'Consultar regulação e fila',
    'saude.regulation.manage': 'Gerenciar solicitações de regulação',
    'saude.regulation.prioritize': 'Alterar prioridade de regulação com auditoria',
    'saude.tfd.view': 'Consultar solicitações de TFD',
    'saude.tfd.manage': 'Gerenciar solicitações de TFD',
    'saude.transport.view': 'Consultar transporte sanitário',
    'saude.transport.manage': 'Gerenciar transporte sanitário',
    'saude.production.view': 'Consultar produção SUS e rascunhos AIH/APAC',
    'saude.production.manage': 'Registrar produção e rascunhos não transmitidos',
    'educacao.dashboard.view': 'Consultar indicadores agregados da Educação',
    'educacao.schools.view': 'Consultar escolas e unidades educacionais',
    'educacao.schools.manage': 'Gerenciar configuração escolar',
    'educacao.students.view': 'Consultar matrículas e alunos',
    'educacao.students.manage': 'Gerenciar cadastros educacionais',
    'educacao.enrollment.manage': 'Realizar matrículas e transferências',
    'educacao.teachers.view': 'Consultar lotações docentes',
    'educacao.teachers.manage': 'Gerenciar lotações docentes',
    'educacao.attendance.view': 'Consultar frequência escolar',
    'educacao.attendance.manage': 'Registrar frequência escolar',
    'educacao.grades.view': 'Consultar avaliações e notas',
    'educacao.grades.manage': 'Gerenciar avaliações e notas',
    'educacao.calendar.view': 'Consultar calendário escolar',
    'educacao.calendar.manage': 'Gerenciar calendário escolar',
    'educacao.meals.view': 'Consultar alimentação escolar',
    'educacao.meals.manage': 'Gerenciar cardápios escolares',
    'educacao.transport.view': 'Consultar rotas de transporte escolar',
    'educacao.transport.manage': 'Gerenciar rotas de transporte escolar',
    'governance.dashboard.view': 'Consultar painel de Controle Interno',
    'governance.risks.view': 'Consultar riscos institucionais',
    'governance.risks.manage': 'Gerenciar riscos institucionais',
    'governance.controls.view': 'Consultar controles internos',
    'governance.controls.manage': 'Gerenciar controles internos',
    'governance.findings.view': 'Consultar achados de controle',
    'governance.findings.manage': 'Registrar achados e recomendações',
    'governance.findings.close': 'Concluir achados de controle',
    'governance.actions.view': 'Consultar planos de ação',
    'governance.actions.manage': 'Gerenciar planos de ação',
    'governance.actions.approve': 'Validar conclusão de planos de ação',
    'governance.evidence.view': 'Consultar evidências privadas de controle',
    'governance.evidence.manage': 'Anexar evidências privadas de controle',
    'integration.view': 'Consultar integrações governamentais preparadas',
    'integration.manage': 'Configurar integrações governamentais',
}

PUBLIC_PERMISSION_MODULES = {
    code: 'planejamento_publico' for code in ('planning.view', 'planning.manage')
} | {
    code: 'orcamento_publico' for code in ('budget.view', 'budget.manage')
} | {
    code: 'contabilidade_pub' for code in ('accounting.view', 'accounting.manage')
} | {
    code: 'tesouraria_publica' for code in ('treasury.view', 'treasury.manage')
} | {
    code: 'rh_municipal' for code in ('rh.access',)
} | {
    code: 'compras_publicas' for code in ('compras.view', 'compras.manage', 'compras.approve', 'cotacao.view', 'cotacao.manage')
} | {
    code: 'licitacoes_publicas' for code in ('licitacao.view', 'licitacao.manage', 'licitacao.judge', 'licitacao.homologate')
} | {
    code: 'registro_precos' for code in ('price_registry.view', 'price_registry.manage')
} | {
    code: 'contratos_publicos' for code in ('contract.view', 'contract.manage', 'contract.approve', 'contract.execute', 'contract.documents')
} | {
    code: 'rh_municipal' for code in ('rh.view', 'rh.manage', 'rh.documents', 'rh.approve', 'folha.view', 'folha.manage')
} | {
    code: 'ponto_digital' for code in ('ponto.view', 'ponto.register', 'ponto.adjust', 'ponto.approve', 'ponto.close', 'ponto.reopen', 'ponto.report', 'servidor.portal')
} | {
    code: 'patrimonio' for code in ('patrimonio.view', 'patrimonio.manage', 'patrimonio.transfer', 'patrimonio.inventory', 'patrimonio.writeoff', 'patrimonio.report')
} | {
    code: 'almoxarifado' for code in ('almoxarifado.view', 'almoxarifado.manage', 'almoxarifado.request', 'almoxarifado.approve', 'almoxarifado.issue', 'almoxarifado.transfer', 'almoxarifado.inventory')
} | {
    code: 'frotas' for code in ('frotas.view', 'frotas.manage', 'frotas.dispatch', 'frotas.fuel', 'frotas.maintenance', 'frotas.documents', 'frotas.report')
} | {
    code: 'processo_digital' for code in (
        'protocol.view', 'protocol.manage', 'protocol.assign', 'process.view',
        'process.manage', 'process.dispatch', 'process.analyze', 'process.close',
        'process.signature', 'process.documents', 'process.restricted', 'process.sensitive',
    )
} | {
    code: 'transparencia' for code in ('transparency.view', 'transparency.manage', 'transparency.publish')
} | {
    code: 'ouvidoria' for code in ('ouvidoria.view', 'ouvidoria.manage', 'ouvidoria.respond')
} | {
    code: 'esic' for code in ('esic.view', 'esic.manage', 'esic.respond', 'esic.appeal')
} | {
    code: 'tributos_municipais' for code in (
        'contribuinte.view', 'contribuinte.manage', 'tributo.view', 'tributo.manage',
        'arrecadacao.view', 'arrecadacao.manage', 'divida.view', 'divida.manage',
    )
} | {
    code: 'gestao_territorial' for code in ('imovel.view', 'imovel.manage')
} | {
    code: 'nfse_municipal' for code in ('nfse.view', 'nfse.manage')
} | {
    code: 'saude_sus' for code in PUBLIC_PERMISSION_CODES if code.startswith('saude.')
} | {
    code: 'educacao_municipal' for code in PUBLIC_PERMISSION_CODES if code.startswith('educacao.')
} | {
    code: 'controle_interno' for code in PUBLIC_PERMISSION_CODES if code.startswith('governance.')
}


def accesses_for(user):
    membership = membership_for(user)
    if not membership:
        return AcessoMunicipal.objects.none()
    return AcessoMunicipal.objects.filter(membership=membership, ativo=True).select_related(
        'secretaria', 'departamento', 'setor', 'unidade', 'funcao', 'membership__organization'
    ).prefetch_related('permissoes')


def _object_scope_values(record):
    """Return municipality hierarchy ids for a municipal record."""
    if record is None:
        return {}
    model = record._meta.model_name
    if model == 'secretaria':
        return {'secretaria': record.pk}
    if model == 'departamentomunicipal':
        return {'secretaria': record.secretaria_id, 'departamento': record.pk}
    if model == 'setormunicipal':
        return {'secretaria': record.departamento.secretaria_id, 'departamento': record.departamento_id, 'setor': record.pk}
    if model == 'unidademunicipal':
        return {'secretaria': record.setor.departamento.secretaria_id, 'departamento': record.setor.departamento_id, 'setor': record.setor_id, 'unidade': record.pk}
    if model == 'unidadesaudemunicipal':
        return _object_scope_values(record.unidade)
    if model in {'senhafilaatendimento', 'servicofilasaude', 'localatendimentosaude', 'classificacaomanchester', 'alaenfermariasaude', 'leitosaudemunicipal', 'equipamentomonitorsaude'}:
        return _object_scope_values(record.unidade_saude)
    if model in {'telemetriamonitorleito', 'eventoalertamonitorsaude'}:
        return _object_scope_values(record.leito)
    if model in {
        'equipesaudemunicipal', 'profissionalsaudemunicipal', 'agendasaudemunicipal',
        'atendimentosaudemunicipal', 'lotemedicamentomunicipal', 'dispensacaomedicamentomunicipal',
        'registroproducaosus',
    }:
        return _object_scope_values(record.unidade_saude)
    if model == 'evolucaoclinicamunicipal':
        return _object_scope_values(record.atendimento)
    if model == 'cidadaosaudemunicipal':
        return _object_scope_values(record.unidade) if record.unidade_id else {}
    if model == 'familiaterritorialsaude':
        return _object_scope_values(record.unidade_saude)
    if model == 'vinculofamiliasaude':
        return _object_scope_values(record.familia)
    if model == 'prescricaosaudemunicipal':
        return _object_scope_values(record.atendimento)
    if model == 'itemprescricaosaude':
        return _object_scope_values(record.prescricao)
    if model == 'itemdispensacaosaude':
        return _object_scope_values(record.dispensacao)
    if model == 'visitaterritorialsaude':
        if record.agente_id:
            return _object_scope_values(record.agente)
        if record.familia_id:
            return _object_scope_values(record.familia)
        if record.cidadao_id:
            return _object_scope_values(record.cidadao)
        return _object_scope_values(record.imovel) if record.imovel_id else {}
    if model in {'inspecaosanitariamunicipal'}:
        return _object_scope_values(record.estabelecimento)
    if model in {'solicitacaoregulacaosaude'}:
        return _object_scope_values(record.unidade_origem)
    if model == 'vagaregulacaosaude':
        return _object_scope_values(record.unidade_saude)
    if model == 'agendamentoregulacaosaude':
        return _object_scope_values(record.vaga)
    if model == 'tfdmunicipal':
        return _object_scope_values(record.cidadao)
    if model == 'solicitacaocompra':
        return {'secretaria': record.secretaria_id, 'departamento': record.departamento_id,
                'setor': record.setor_id, 'unidade': record.unidade_id}
    if model in {'protocolomunicipal', 'processodigitalmunicipal', 'manifestacaoouvidoriamunicipal', 'pedidoesicmunicipal'}:
        values = {key: getattr(record, f'{key}_id', None) for key in ('secretaria', 'departamento', 'setor', 'unidade')}
        return {key: value for key, value in values.items() if value}
    if model in {'parteprocessomunicipal', 'tramiteprocessomunicipal', 'despachoprocessomunicipal',
                 'parecerprocessomunicipal', 'tarefaprocessomunicipal', 'documentoprocessomunicipal',
                 'versaodocumentoprocessomunicipal', 'solicitacaoassinaturamunicipal',
                 'signatariomunicipal', 'eventoassinaturamunicipal'}:
        target = getattr(record, 'processo', None)
        if target is None and getattr(record, 'documento_id', None):
            target = record.documento.processo
        if target is None and getattr(record, 'solicitacao_id', None):
            target = record.solicitacao.processo
        if target is None and getattr(record, 'manifestacao_id', None):
            target = record.manifestacao
        if target is None and getattr(record, 'pedido_id', None):
            target = record.pedido
        if target is not None:
            return _object_scope_values(target)
        return {}
    if model in {'respostaesicmunicipal', 'recursoesicmunicipal', 'eventoesicmunicipal'}:
        target = getattr(record, 'pedido', None)
        if target is None and getattr(record, 'recurso_id', None):
            target = record.recurso.pedido
        return _object_scope_values(target) if target else {}
    if model == 'respostaouvidoriamunicipal':
        return _object_scope_values(record.manifestacao)
    if model == 'itemsolicitacaocompra':
        return _object_scope_values(record.solicitacao)
    if model == 'processocontratacaomunicipal':
        values = {'secretaria': record.secretaria_id, 'unidade': record.unidade_id}
        if record.unidade_id:
            values.update(_object_scope_values(record.unidade))
        return values
    if model in {'itemcontratacaomunicipal', 'lotecontratacaomunicipal'}:
        return _object_scope_values(record.processo)
    if model == 'pesquisaprecomunicipal':
        return _object_scope_values(record.processo if record.processo_id else record.solicitacao)
    if model == 'cotacaofornecedormunicipal':
        return _object_scope_values(record.pesquisa)
    if model == 'itempesquisaprecomunicipal':
        return _object_scope_values(record.cotacao)
    if model == 'licitacaomunicipal':
        return _object_scope_values(record.processo)
    if model == 'fornecedorprocessomunicipal':
        return _object_scope_values(record.processo)
    if model == 'propostamunicipal':
        return _object_scope_values(record.fornecedor_processo)
    if model == 'itempropostamunicipal':
        return _object_scope_values(record.item_processo)
    if model == 'ataregistroprecosmunicipal':
        return _object_scope_values(record.processo)
    if model == 'itemataregistroprecosmunicipal':
        return _object_scope_values(record.ata)
    if model == 'utilizacaoataprecosmunicipal':
        return _object_scope_values(record.item_ata)
    if model == 'contratopublicomunicipal':
        values = _object_scope_values(record.processo)
        values.update({'secretaria': record.secretaria_id, 'unidade': record.unidade_id})
        if record.unidade_id:
            values.update(_object_scope_values(record.unidade))
        return values
    if model == 'itemcontratopublicomunicipal':
        return _object_scope_values(record.contrato)
    if model in {'aditivocontratopublico', 'execucaocontratopublico'}:
        return _object_scope_values(record.contrato)
    if model == 'documentocontratacaomunicipal':
        target = record.contrato if record.contrato_id else record.processo if record.processo_id else record.solicitacao
        return _object_scope_values(target)
    if model in {'bempatrimonial', 'veiculomunicipal'}:
        values = {}
        for key in ('secretaria', 'departamento', 'setor', 'unidade'):
            value = getattr(record, f'{key}_id', None)
            if value:
                values[key] = value
        if record.responsavel_id:
            values.update(_object_scope_values(record.responsavel))
        return values
    if model == 'movimentacaopatrimonial':
        return _object_scope_values(record.bem)
    if model == 'inventariopatrimonial':
        return _object_scope_values(record.unidade) if record.unidade_id else {}
    if model == 'iteminventariopatrimonial':
        return _object_scope_values(record.bem) if record.bem_id else _object_scope_values(record.inventario)
    if model in {'eventopatrimoniomunicipal', 'depreciacaopatrimonial'}:
        return _object_scope_values(record.bem)
    if model == 'depositoalmoxarifadomunicipal':
        return _object_scope_values(record.unidade) if record.unidade_id else {}
    if model in {'saldoalmoxarifadomunicipal', 'lotealmoxarifadomunicipal', 'movimentoalmoxarifadomunicipal'}:
        return _object_scope_values(record.deposito)
    if model == 'solicitacaomaterialmunicipal':
        return _object_scope_values(record.unidade)
    if model == 'itemsolicitacaomaterialmunicipal':
        return _object_scope_values(record.solicitacao)
    if model == 'recebimentoalmoxarifadomunicipal':
        return _object_scope_values(record.deposito)
    if model == 'inventarioalmoxarifadomunicipal':
        return _object_scope_values(record.deposito)
    if model == 'iteminventarioalmoxarifadomunicipal':
        return _object_scope_values(record.inventario)
    if model == 'requisicaomaterialauditada':
        return _object_scope_values(record.solicitacao)
    if model == 'eventoalmoxarifadomunicipal':
        return {}
    if model in {'condutormunicipal'}:
        return _object_scope_values(record.servidor)
    if model in {'viagemmunicipal', 'abastecimentomunicipal', 'manutencaoveiculomunicipal', 'documentofrotamunicipal', 'eventofrotamunicipal'}:
        return _object_scope_values(record.veiculo)
    if model in {
        'pontoservidor', 'solicitacaohoraextramunicipal', 'justificativapontomunicipal',
        'movimentacaobancohorasmunicipal', 'eventopontodigital', 'historicolotacaoservidor',
        'dependenteservidormunicipal', 'beneficioservidormunicipal',
        'remuneracaobaseservidormunicipal', 'lancamentofolhamunicipal',
    }:
        server = getattr(record, 'servidor', None)
        return _object_scope_values(server)
    if model == 'decisaopontomunicipal':
        target = record.justificativa if record.justificativa_id else record.solicitacao_hora_extra
        return _object_scope_values(target)
    if model == 'snapshotfechamentopontomunicipal':
        return _object_scope_values(record.periodo)
    if model == 'atribuicaojornadamunicipal':
        target = record.servidor if record.servidor_id else record.grupo
        return _object_scope_values(target)
    values = {}
    for key in ('secretaria', 'departamento', 'setor', 'unidade'):
        value = getattr(record, f'{key}_id', None)
        if value:
            values[key] = value
    if 'secretaria' not in values and getattr(record, 'departamento_id', None):
        values['secretaria'] = record.departamento.secretaria_id
    if 'departamento' not in values and getattr(record, 'setor_id', None):
        values['departamento'] = record.setor.departamento_id
    if 'secretaria' not in values and getattr(record, 'setor_id', None):
        values['secretaria'] = record.setor.departamento.secretaria_id
    if 'setor' not in values and getattr(record, 'unidade_id', None):
        values['setor'] = record.unidade.setor_id
    if 'departamento' not in values and getattr(record, 'unidade_id', None):
        values['departamento'] = record.unidade.setor.departamento_id
    if 'secretaria' not in values and getattr(record, 'unidade_id', None):
        values['secretaria'] = record.unidade.setor.departamento.secretaria_id
    return values


def scope_allows(access, record=None):
    if record is None:
        return True
    record_org = getattr(record, 'organization_id', None)
    if record_org is None:
        try:
            record_org = record.membership.organization_id
        except AttributeError:
            return False
    if record_org != access.membership.organization_id:
        return False
    if access.escopo == AcessoMunicipal.Escopo.PREFEITURA:
        return True
    key = {
        AcessoMunicipal.Escopo.SECRETARIA: 'secretaria',
        AcessoMunicipal.Escopo.DEPARTAMENTO: 'departamento',
        AcessoMunicipal.Escopo.SETOR: 'setor',
        AcessoMunicipal.Escopo.UNIDADE: 'unidade',
    }.get(access.escopo)
    return bool(key and _object_scope_values(record).get(key) == getattr(access, f'{key}_id'))


def has_municipal_permission(user, capability, *, module='gestao_publica', record=None):
    organization = current_organization()
    membership = membership_for(user, organization)
    required_module = PUBLIC_PERMISSION_MODULES.get(capability, module)
    if capability in PUBLIC_PERMISSION_MODULES and module not in {'gestao_publica', required_module}:
        return False
    if (capability not in PUBLIC_PERMISSION_CODES or not membership
            or not has_erp_access(user, organization, code=PRODUCT_CODE)
            or not product_module_enabled(user, PRODUCT_CODE, required_module, organization)):
        return False
    for access in accesses_for(user):
        if not scope_allows(access, record):
            continue
        if capability == 'profile.manage' and access.papel != AcessoMunicipal.Papel.ADMIN_PREFEITURA:
            continue
        if access.papel == AcessoMunicipal.Papel.ADMIN_PREFEITURA:
            return True
        implied = {
            'structure.view': {'structure.view', 'structure.manage'},
            'access.view': {'access.view', 'access.manage'},
            'servers.view': {'servers.view', 'servers.manage'},
        }.get(capability, {capability})
        if access.permissoes.filter(capacidade__in=implied, permitido=True).exists():
            return True
    return False


def require_municipal_permission(user, capability, *, module='gestao_publica', record=None):
    if not has_municipal_permission(user, capability, module=module, record=record):
        raise PermissionDenied('Acesso municipal não autorizado para esta organização, módulo, permissão ou escopo.')
    return membership_for(user)


SCOPE_PATHS = {
    'secretaria': {'secretaria': 'pk', 'departamento': 'departamentos__pk', 'setor': 'departamentos__setores__pk', 'unidade': 'departamentos__setores__unidades__pk'},
    'departamentomunicipal': {'secretaria': 'secretaria_id', 'departamento': 'pk', 'setor': 'setores__pk', 'unidade': 'setores__unidades__pk'},
    'setormunicipal': {'secretaria': 'departamento__secretaria_id', 'departamento': 'departamento_id', 'setor': 'pk', 'unidade': 'unidades__pk'},
    'unidademunicipal': {'secretaria': 'setor__departamento__secretaria_id', 'departamento': 'setor__departamento_id', 'setor': 'setor_id', 'unidade': 'pk'},
    'servidor': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'pontoservidor': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'solicitacaohoraextramunicipal': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'justificativapontomunicipal': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'movimentacaobancohorasmunicipal': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'eventopontodigital': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'historicolotacaoservidor': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'dependentesservidormunicipal': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'beneficioservidormunicipal': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'remuneracaobaseservidormunicipal': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'lancamentofolhamunicipal': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'decisaopontomunicipal': {'secretaria': 'justificativa__servidor__secretaria_id', 'departamento': 'justificativa__servidor__departamento_id', 'setor': 'justificativa__servidor__setor_id', 'unidade': 'justificativa__servidor__unidade_id'},
    'atribuicaojornadamunicipal': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'grupojornadamunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'acessomunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'convitemunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'orgaoorcamentariomunicipal': {'secretaria': 'secretaria_id'},
    'unidadeorcamentariamunicipal': {
        'secretaria': 'secretaria_id',
        'departamento': 'unidade_municipal__setor__departamento_id',
        'setor': 'unidade_municipal__setor_id',
        'unidade': 'unidade_municipal_id',
    },
    'programaplanejamentomunicipal': {'secretaria': 'secretaria_id'},
    'indicadorplanejamentomunicipal': {'secretaria': 'programa__secretaria_id'},
    'acaoplanejamentomunicipal': {'secretaria': 'programa__secretaria_id'},
    'dotacaoorcamentariamunicipal': {
        'secretaria': 'unidade_orcamentaria__secretaria_id',
        'departamento': 'unidade_orcamentaria__unidade_municipal__setor__departamento_id',
        'setor': 'unidade_orcamentaria__unidade_municipal__setor_id',
        'unidade': 'unidade_orcamentaria__unidade_municipal_id',
    },
    'creditoadicionalmunicipal': {
        'secretaria': 'dotacao_destino__unidade_orcamentaria__secretaria_id',
        'departamento': 'dotacao_destino__unidade_orcamentaria__unidade_municipal__setor__departamento_id',
        'setor': 'dotacao_destino__unidade_orcamentaria__unidade_municipal__setor_id',
        'unidade': 'dotacao_destino__unidade_orcamentaria__unidade_municipal_id',
    },
    'contabancariamunicipal': {
        'secretaria': 'unidade_orcamentaria__secretaria_id',
        'departamento': 'unidade_orcamentaria__unidade_municipal__setor__departamento_id',
        'setor': 'unidade_orcamentaria__unidade_municipal__setor_id',
        'unidade': 'unidade_orcamentaria__unidade_municipal_id',
    },
    'movimentacaotesourariamunicipal': {
        'secretaria': 'conta_origem__unidade_orcamentaria__secretaria_id',
        'departamento': 'conta_origem__unidade_orcamentaria__unidade_municipal__setor__departamento_id',
        'setor': 'conta_origem__unidade_orcamentaria__unidade_municipal__setor_id',
        'unidade': 'conta_origem__unidade_orcamentaria__unidade_municipal_id',
    },
    'conciliacaobancariamunicipal': {
        'secretaria': 'conta__unidade_orcamentaria__secretaria_id',
        'departamento': 'conta__unidade_orcamentaria__unidade_municipal__setor__departamento_id',
        'setor': 'conta__unidade_orcamentaria__unidade_municipal__setor_id',
        'unidade': 'conta__unidade_orcamentaria__unidade_municipal_id',
    },
    'solicitacaocompra': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'itemsolicitacaocompra': {'secretaria': 'solicitacao__secretaria_id', 'departamento': 'solicitacao__departamento_id', 'setor': 'solicitacao__setor_id', 'unidade': 'solicitacao__unidade_id'},
    'processocontratacaomunicipal': {'secretaria': 'secretaria_id', 'departamento': 'unidade__setor__departamento_id', 'setor': 'unidade__setor_id', 'unidade': 'unidade_id'},
    'lotecontratacaomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__unidade__setor__departamento_id', 'setor': 'processo__unidade__setor_id', 'unidade': 'processo__unidade_id'},
    'itemcontratacaomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__unidade__setor__departamento_id', 'setor': 'processo__unidade__setor_id', 'unidade': 'processo__unidade_id'},
    'pesquisaprecomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__unidade__setor__departamento_id', 'setor': 'processo__unidade__setor_id', 'unidade': 'processo__unidade_id', 'solicitacao': 'solicitacao__secretaria_id'},
    'cotacaofornecedormunicipal': {'secretaria': 'pesquisa__processo__secretaria_id', 'departamento': 'pesquisa__processo__unidade__setor__departamento_id', 'setor': 'pesquisa__processo__unidade__setor_id', 'unidade': 'pesquisa__processo__unidade_id'},
    'itempesquisaprecomunicipal': {'secretaria': 'cotacao__pesquisa__processo__secretaria_id', 'departamento': 'cotacao__pesquisa__processo__unidade__setor__departamento_id', 'setor': 'cotacao__pesquisa__processo__unidade__setor_id', 'unidade': 'cotacao__pesquisa__processo__unidade_id'},
    'licitacaomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__unidade__setor__departamento_id', 'setor': 'processo__unidade__setor_id', 'unidade': 'processo__unidade_id'},
    'fornecedorprocessomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__unidade__setor__departamento_id', 'setor': 'processo__unidade__setor_id', 'unidade': 'processo__unidade_id'},
    'propostamunicipal': {'secretaria': 'fornecedor_processo__processo__secretaria_id', 'departamento': 'fornecedor_processo__processo__unidade__setor__departamento_id', 'setor': 'fornecedor_processo__processo__unidade__setor_id', 'unidade': 'fornecedor_processo__processo__unidade_id'},
    'itempropostamunicipal': {'secretaria': 'item_processo__processo__secretaria_id', 'departamento': 'item_processo__processo__unidade__setor__departamento_id', 'setor': 'item_processo__processo__unidade__setor_id', 'unidade': 'item_processo__processo__unidade_id'},
    'ataregistroprecosmunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__unidade__setor__departamento_id', 'setor': 'processo__unidade__setor_id', 'unidade': 'processo__unidade_id'},
    'itemataregistroprecosmunicipal': {'secretaria': 'ata__processo__secretaria_id', 'departamento': 'ata__processo__unidade__setor__departamento_id', 'setor': 'ata__processo__unidade__setor_id', 'unidade': 'ata__processo__unidade_id'},
    'utilizacaoataprecosmunicipal': {'secretaria': 'item_ata__ata__processo__secretaria_id', 'departamento': 'item_ata__ata__processo__unidade__setor__departamento_id', 'setor': 'item_ata__ata__processo__unidade__setor_id', 'unidade': 'unidade_id'},
    'contratopublicomunicipal': {'secretaria': 'secretaria_id', 'departamento': 'unidade__setor__departamento_id', 'setor': 'unidade__setor_id', 'unidade': 'unidade_id'},
    'itemcontratopublicomunicipal': {'secretaria': 'contrato__secretaria_id', 'departamento': 'contrato__unidade__setor__departamento_id', 'setor': 'contrato__unidade__setor_id', 'unidade': 'contrato__unidade_id'},
    'aditivocontratopublico': {'secretaria': 'contrato__secretaria_id', 'departamento': 'contrato__unidade__setor__departamento_id', 'setor': 'contrato__unidade__setor_id', 'unidade': 'contrato__unidade_id'},
    'execucaocontratopublico': {'secretaria': 'contrato__secretaria_id', 'departamento': 'contrato__unidade__setor__departamento_id', 'setor': 'contrato__unidade__setor_id', 'unidade': 'contrato__unidade_id'},
    'documentocontratacaomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__unidade__setor__departamento_id', 'setor': 'processo__unidade__setor_id', 'unidade': 'processo__unidade_id'},
    'bempatrimonial': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'movimentacaopatrimonial': {'secretaria': 'bem__secretaria_id', 'departamento': 'bem__departamento_id', 'setor': 'bem__setor_id', 'unidade': 'bem__unidade_id'},
    'inventariopatrimonial': {'unidade': 'unidade_id'},
    'iteminventariopatrimonial': {'secretaria': 'bem__secretaria_id', 'departamento': 'bem__departamento_id', 'setor': 'bem__setor_id', 'unidade': 'bem__unidade_id'},
    'eventopatrimoniomunicipal': {'secretaria': 'bem__secretaria_id', 'departamento': 'bem__departamento_id', 'setor': 'bem__setor_id', 'unidade': 'bem__unidade_id'},
    'depreciacaopatrimonial': {'secretaria': 'bem__secretaria_id', 'departamento': 'bem__departamento_id', 'setor': 'bem__setor_id', 'unidade': 'bem__unidade_id'},
    'depositoalmoxarifadomunicipal': {'unidade': 'unidade_id'},
    'saldoalmoxarifadomunicipal': {'secretaria': 'deposito__unidade__setor__departamento__secretaria_id', 'departamento': 'deposito__unidade__setor__departamento_id', 'setor': 'deposito__unidade__setor_id', 'unidade': 'deposito__unidade_id'},
    'lotealmoxarifadomunicipal': {'secretaria': 'deposito__unidade__setor__departamento__secretaria_id', 'departamento': 'deposito__unidade__setor__departamento_id', 'setor': 'deposito__unidade__setor_id', 'unidade': 'deposito__unidade_id'},
    'solicitacaomaterialmunicipal': {'secretaria': 'unidade__setor__departamento__secretaria_id', 'departamento': 'unidade__setor__departamento_id', 'setor': 'unidade__setor_id', 'unidade': 'unidade_id'},
    'itemsolicitacaomaterialmunicipal': {'secretaria': 'solicitacao__unidade__setor__departamento__secretaria_id', 'departamento': 'solicitacao__unidade__setor__departamento_id', 'setor': 'solicitacao__unidade__setor_id', 'unidade': 'solicitacao__unidade_id'},
    'recebimentoalmoxarifadomunicipal': {'secretaria': 'deposito__unidade__setor__departamento__secretaria_id', 'departamento': 'deposito__unidade__setor__departamento_id', 'setor': 'deposito__unidade__setor_id', 'unidade': 'deposito__unidade_id'},
    'movimentoalmoxarifadomunicipal': {'secretaria': 'deposito__unidade__setor__departamento__secretaria_id', 'departamento': 'deposito__unidade__setor__departamento_id', 'setor': 'deposito__unidade__setor_id', 'unidade': 'deposito__unidade_id'},
    'inventarioalmoxarifadomunicipal': {'secretaria': 'deposito__unidade__setor__departamento__secretaria_id', 'departamento': 'deposito__unidade__setor__departamento_id', 'setor': 'deposito__unidade__setor_id', 'unidade': 'deposito__unidade_id'},
    'iteminventarioalmoxarifadomunicipal': {'secretaria': 'inventario__deposito__unidade__setor__departamento__secretaria_id', 'departamento': 'inventario__deposito__unidade__setor__departamento_id', 'setor': 'inventario__deposito__unidade__setor_id', 'unidade': 'inventario__deposito__unidade_id'},
    'requisicaomaterialauditada': {'secretaria': 'solicitacao__unidade__setor__departamento__secretaria_id', 'departamento': 'solicitacao__unidade__setor__departamento_id', 'setor': 'solicitacao__unidade__setor_id', 'unidade': 'solicitacao__unidade_id'},
    'veiculomunicipal': {'secretaria': 'secretaria_id', 'departamento': 'unidade__setor__departamento_id', 'setor': 'unidade__setor_id', 'unidade': 'unidade_id'},
    'condutormunicipal': {'secretaria': 'servidor__secretaria_id', 'departamento': 'servidor__departamento_id', 'setor': 'servidor__setor_id', 'unidade': 'servidor__unidade_id'},
    'viagemmunicipal': {'secretaria': 'veiculo__secretaria_id', 'departamento': 'veiculo__unidade__setor__departamento_id', 'setor': 'veiculo__unidade__setor_id', 'unidade': 'veiculo__unidade_id'},
    'abastecimentomunicipal': {'secretaria': 'veiculo__secretaria_id', 'departamento': 'veiculo__unidade__setor__departamento_id', 'setor': 'veiculo__unidade__setor_id', 'unidade': 'veiculo__unidade_id'},
    'manutencaoveiculomunicipal': {'secretaria': 'veiculo__secretaria_id', 'departamento': 'veiculo__unidade__setor__departamento_id', 'setor': 'veiculo__unidade__setor_id', 'unidade': 'veiculo__unidade_id'},
    'documentofrotamunicipal': {'secretaria': 'veiculo__secretaria_id', 'departamento': 'veiculo__unidade__setor__departamento_id', 'setor': 'veiculo__unidade__setor_id', 'unidade': 'veiculo__unidade_id'},
    'eventofrotamunicipal': {'secretaria': 'veiculo__secretaria_id', 'departamento': 'veiculo__unidade__setor__departamento_id', 'setor': 'veiculo__unidade__setor_id', 'unidade': 'veiculo__unidade_id'},
    'protocolomunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'processodigitalmunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'parteprocessomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__departamento_id', 'setor': 'processo__setor_id', 'unidade': 'processo__unidade_id'},
    'tramiteprocessomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__departamento_id', 'setor': 'processo__setor_id', 'unidade': 'processo__unidade_id'},
    'despachoprocessomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__departamento_id', 'setor': 'processo__setor_id', 'unidade': 'processo__unidade_id'},
    'parecerprocessomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__departamento_id', 'setor': 'processo__setor_id', 'unidade': 'processo__unidade_id'},
    'tarefaprocessomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__departamento_id', 'setor': 'processo__setor_id', 'unidade': 'processo__unidade_id'},
    'documentoprocessomunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__departamento_id', 'setor': 'processo__setor_id', 'unidade': 'processo__unidade_id'},
    'versaodocumentoprocessomunicipal': {'secretaria': 'documento__processo__secretaria_id', 'departamento': 'documento__processo__departamento_id', 'setor': 'documento__processo__setor_id', 'unidade': 'documento__processo__unidade_id'},
    'solicitacaoassinaturamunicipal': {'secretaria': 'processo__secretaria_id', 'departamento': 'processo__departamento_id', 'setor': 'processo__setor_id', 'unidade': 'processo__unidade_id'},
    'signatariomunicipal': {'secretaria': 'solicitacao__processo__secretaria_id', 'departamento': 'solicitacao__processo__departamento_id', 'setor': 'solicitacao__processo__setor_id', 'unidade': 'solicitacao__processo__unidade_id'},
    'eventoassinaturamunicipal': {'secretaria': 'solicitacao__processo__secretaria_id', 'departamento': 'solicitacao__processo__departamento_id', 'setor': 'solicitacao__processo__setor_id', 'unidade': 'solicitacao__processo__unidade_id'},
    'publicacaotransparenciamunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'manifestacaoouvidoriamunicipal': {'secretaria': 'secretaria_id'},
    'eventouvidoriamunicipal': {'secretaria': 'manifestacao__secretaria_id'},
    'respostaouvidoriamunicipal': {'secretaria': 'manifestacao__secretaria_id'},
    'pedidoesicmunicipal': {'secretaria': 'secretaria_id'},
    'respostaesicmunicipal': {'secretaria': 'pedido__secretaria_id'},
    'recursoesicmunicipal': {'secretaria': 'pedido__secretaria_id'},
    'eventoesicmunicipal': {'secretaria': 'pedido__secretaria_id'},
    'contribuintemunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'imovelmunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'regratributariamunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'lancamentotributariomunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'parcelatributariamunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'pagamentoarrecadacaomunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'inscricaodividaativamunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
    'notaservicomunicipal': {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'},
}

SCOPE_PATHS.update({
    name: {'secretaria': 'secretaria_id', 'departamento': 'departamento_id', 'setor': 'setor_id', 'unidade': 'unidade_id'}
    for name in (
        'riscocontroleinterno', 'controleinternomunicipal', 'achadocontroleinterno',
        'recomendacaocontroleinterno', 'planoacaointerno', 'evidenciacontroleinterno',
        'eventocontroleinterno',
    )
})
SCOPE_PATHS.update({
    # Saúde records inherit the municipal hierarchy. Keep their list filters
    # aligned with the same scope rules used by point, procurement and assets.
    name: {'secretaria': 'secretaria_id', 'departamento': 'departamento_id',
           'setor': 'setor_id', 'unidade': 'unidade_id'}
    for name in (
        'equipesaudemunicipal', 'profissionalsaudemunicipal', 'agendasaudemunicipal',
        'atendimentosaudemunicipal', 'evolucaoclinicamunicipal',
        'cidadaosaudemunicipal', 'familiaterritorialsaude', 'vinculofamiliasaude', 'prescricaosaudemunicipal',
        'itemprescricaosaude', 'visitaterritorialsaude', 'medicamentomunicipal',
        'lotemedicamentomunicipal', 'dispensacaomedicamentomunicipal',
        'itemdispensacaosaude', 'estabelecimentosanitariomunicipal',
        'inspecaosanitariamunicipal', 'notificacaoepidemiologicamunicipal',
        'solicitacaoregulacaosaude', 'vagaregulacaosaude', 'agendamentoregulacaosaude',
        'tfdmunicipal', 'registroproducaosus',
    )
})
SCOPE_PATHS['unidadesaudemunicipal'] = {
    'secretaria': 'unidade__setor__departamento__secretaria_id',
    'departamento': 'unidade__setor__departamento_id',
    'setor': 'unidade__setor_id',
    'unidade': 'unidade_id',
}
_health_unit_scope = {
    'secretaria': 'unidade_saude__unidade__setor__departamento__secretaria_id',
    'departamento': 'unidade_saude__unidade__setor__departamento_id',
    'setor': 'unidade_saude__unidade__setor_id',
    'unidade': 'unidade_saude__unidade_id',
}
for _health_model in (
    'equipesaudemunicipal', 'profissionalsaudemunicipal', 'agendasaudemunicipal',
    'atendimentosaudemunicipal', 'lotemedicamentomunicipal', 'dispensacaomedicamentomunicipal',
    'registroproducaosus',
):
    SCOPE_PATHS[_health_model] = _health_unit_scope
for _health_model in ('senhafilaatendimento', 'servicofilasaude', 'localatendimentosaude', 'classificacaomanchester', 'alaenfermariasaude', 'leitosaudemunicipal', 'equipamentomonitorsaude', 'telemetriamonitorleito'):
    SCOPE_PATHS[_health_model] = _health_unit_scope
SCOPE_PATHS['eventoalertamonitorsaude'] = {
    key: 'leito__' + path for key, path in _health_unit_scope.items()
}
SCOPE_PATHS['evolucaoclinicamunicipal'] = {
    key: path.replace('unidade_saude', 'atendimento__unidade_saude') for key, path in _health_unit_scope.items()
}
SCOPE_PATHS['familiaterritorialsaude'] = _health_unit_scope
SCOPE_PATHS['vinculofamiliasaude'] = {
    key: path.replace('unidade_saude', 'familia__unidade_saude') for key, path in _health_unit_scope.items()
}
SCOPE_PATHS['prescricaosaudemunicipal'] = {
    key: path.replace('unidade_saude', 'atendimento__unidade_saude') for key, path in _health_unit_scope.items()
}
SCOPE_PATHS['itemprescricaosaude'] = {
    key: path.replace('unidade_saude', 'prescricao__atendimento__unidade_saude') for key, path in _health_unit_scope.items()
}
SCOPE_PATHS['itemdispensacaosaude'] = {
    key: path.replace('unidade_saude', 'dispensacao__unidade_saude') for key, path in _health_unit_scope.items()
}
SCOPE_PATHS['medicamentomunicipal'] = {
    key: path.replace('unidade_saude', 'lotes__unidade_saude') for key, path in _health_unit_scope.items()
}
SCOPE_PATHS['acaoplanoacaointerno'] = {
    'secretaria': 'plano__secretaria_id', 'departamento': 'plano__departamento_id',
    'setor': 'plano__setor_id', 'unidade': 'plano__unidade_id',
}


def _filter_scope(queryset, query):
    # IN removes duplicate scope matches without adding DISTINCT to the
    # outer query, which may need SELECT FOR UPDATE on PostgreSQL.
    return queryset.filter(pk__in=queryset.model.objects.filter(query).values('pk'))


def scoped_queryset(user, queryset, *, capability=None):
    accesses = list(accesses_for(user))
    if capability is not None:
        accesses = [access for access in accesses if (
            access.papel == AcessoMunicipal.Papel.ADMIN_PREFEITURA
            or access.permissoes.filter(capacidade=capability, permitido=True).exists()
        )]
    if not accesses:
        return queryset.none()
    if any(row.escopo == AcessoMunicipal.Escopo.PREFEITURA for row in accesses):
        return queryset
    if queryset.model._meta.model_name == 'decisaopontomunicipal':
        query = Q(pk__in=[])
        for access in accesses:
            field = {
                AcessoMunicipal.Escopo.SECRETARIA: 'secretaria_id',
                AcessoMunicipal.Escopo.DEPARTAMENTO: 'departamento_id',
                AcessoMunicipal.Escopo.SETOR: 'setor_id',
                AcessoMunicipal.Escopo.UNIDADE: 'unidade_id',
            }.get(access.escopo)
            value = getattr(access, field, None) if field else None
            if not value:
                continue
            for relation in ('justificativa__servidor__', 'solicitacao_hora_extra__servidor__'):
                server_field = field.replace('_id', '_id')
                query |= Q(**{relation + server_field: value})
        return _filter_scope(queryset, query)
    if queryset.model._meta.model_name == 'atribuicaojornadamunicipal':
        query = Q(pk__in=[])
        for access in accesses:
            field = {
                AcessoMunicipal.Escopo.SECRETARIA: 'secretaria_id',
                AcessoMunicipal.Escopo.DEPARTAMENTO: 'departamento_id',
                AcessoMunicipal.Escopo.SETOR: 'setor_id',
                AcessoMunicipal.Escopo.UNIDADE: 'unidade_id',
            }.get(access.escopo)
            value = getattr(access, field, None) if field else None
            if value:
                for relation in ('servidor__', 'grupo__'):
                    query |= Q(**{relation + field: value})
        return _filter_scope(queryset, query)
    paths = SCOPE_PATHS.get(queryset.model._meta.model_name)
    if not paths:
        return queryset.none()
    query = Q(pk__in=[])
    scope_fields = {
        AcessoMunicipal.Escopo.SECRETARIA: 'secretaria',
        AcessoMunicipal.Escopo.DEPARTAMENTO: 'departamento',
        AcessoMunicipal.Escopo.SETOR: 'setor',
        AcessoMunicipal.Escopo.UNIDADE: 'unidade',
    }
    for access in accesses:
        field = scope_fields.get(access.escopo)
        path = paths.get(field) if field else None
        value = getattr(access, f'{field}_id', None) if field else None
        if path and value:
            query |= Q(**{path: value})
    return _filter_scope(queryset, query)


def audit_public(user, action, record, changes):
    org = current_organization()
    from platform_core.models import ERPCatalog
    erp = ERPCatalog.objects.filter(code=PRODUCT_CODE).first()
    if not org or not erp:
        raise ValidationError('Contexto de auditoria municipal incompleto.')
    PlatformAudit.objects.create(organization=org, erp=erp, user=user,
        action=action, record=f'{record._meta.label}:{record.pk}', changes=changes)


INITIAL_SECRETARY_CATALOG = (
    ('SEC-ADMIN', 'Administração'),
    ('SEC-FIN', 'Finanças'),
    ('SEC-SAUDE', 'Saúde'),
    ('SEC-EDUCACAO', 'Educação'),
    ('SEC-ASSIST-SOCIAL', 'Assistência Social'),
    ('SEC-OBRAS-INFRA', 'Obras e Infraestrutura'),
    ('SEC-MEIO-AMBIENTE', 'Meio Ambiente'),
    ('SEC-AGRICULTURA', 'Agricultura'),
    ('SEC-TRANSPORTE', 'Transporte'),
    ('SEC-CULTURA', 'Cultura'),
    ('SEC-ESPORTE-LAZER', 'Esporte e Lazer'),
    ('SEC-PLANEJAMENTO', 'Planejamento'),
    ('SEC-LICITACOES-CONTRATOS', 'Licitações e Contratos'),
    ('SEC-SEGURANCA-PUBLICA', 'Segurança Pública'),
    ('SEC-TURISMO', 'Turismo'),
    ('SEC-GOVERNO', 'Governo'),
)


@transaction.atomic
def initialize_secretary_catalog(actor):
    """Add missing standard secretariats, all disabled, for the active tenant."""
    require_municipal_permission(actor, 'structure.manage')
    actor_accesses = list(accesses_for(actor))
    if not any(row.papel == AcessoMunicipal.Papel.ADMIN_PREFEITURA for row in actor_accesses):
        raise PermissionDenied('Somente o Administrador da Prefeitura pode inicializar o catálogo de secretarias.')
    organization = current_organization()
    # Serialize initialization per tenant, including the first insert when no
    # Secretaria rows exist yet; locking the empty child queryset is not enough.
    from platform_core.models import Organization
    Organization.objects.select_for_update().get(pk=organization.pk)
    existing = list(Secretaria.objects.select_for_update())
    codes = {row.codigo.casefold() for row in existing}
    names = {' '.join(row.nome.casefold().split()) for row in existing}
    created = []
    for code, name in INITIAL_SECRETARY_CATALOG:
        if code.casefold() in codes or ' '.join(name.casefold().split()) in names:
            continue
        row = Secretaria(
            organization=organization, codigo=code, nome=name,
            descricao='Item do catálogo municipal inicial.', ativo=False,
        )
        row.save()
        audit_public(actor, 'SECRETARIA_CATALOGO_INICIAL_ADICIONADA', row, {
            'codigo': row.codigo, 'nome': row.nome, 'ativo': False,
        })
        created.append(row)
        codes.add(code.casefold())
        names.add(' '.join(name.casefold().split()))
    return created


def _scope_is_within(actor_accesses, role, escopo, secretaria=None, departamento=None, setor=None, unidade=None):
    if any(a.papel == AcessoMunicipal.Papel.ADMIN_PREFEITURA for a in actor_accesses):
        return True
    target = {'SECRETARIA': secretaria, 'DEPARTAMENTO': departamento, 'SETOR': setor, 'UNIDADE': unidade}.get(escopo)
    for actor in actor_accesses:
        if actor.escopo == AcessoMunicipal.Escopo.PREFEITURA:
            return True
        if actor.escopo == AcessoMunicipal.Escopo.SECRETARIA:
            sec_id = actor.secretaria_id
            target_sec = (secretaria.pk if secretaria else departamento.secretaria_id if departamento else
                          setor.departamento.secretaria_id if setor else unidade.setor.departamento.secretaria_id if unidade else None)
            if target_sec == sec_id:
                return True
        if actor.escopo == AcessoMunicipal.Escopo.DEPARTAMENTO:
            dep_id = actor.departamento_id
            target_dep = departamento.pk if departamento else setor.departamento_id if setor else unidade.setor.departamento_id if unidade else None
            if target_dep == dep_id:
                return True
        if actor.escopo == AcessoMunicipal.Escopo.SETOR:
            target_set = setor.pk if setor else unidade.setor_id if unidade else None
            if target_set == actor.setor_id:
                return True
        if actor.escopo == AcessoMunicipal.Escopo.UNIDADE and unidade and unidade.pk == actor.unidade_id:
            return True
    return False


@transaction.atomic
def assign_municipal_access(actor, *, membership, papel, escopo, secretaria=None, departamento=None,
                            setor=None, unidade=None, funcao=None, permissions=()):
    require_municipal_permission(actor, 'access.manage')
    actor_accesses = list(accesses_for(actor))
    if membership.organization_id != current_organization().pk:
        raise ValidationError('Membership pertence a outra Prefeitura.')
    if not _scope_is_within(actor_accesses, papel, escopo, secretaria, departamento, setor, unidade):
        raise PermissionDenied('O escopo solicitado está fora da sua autoridade municipal.')
    candidate = AcessoMunicipal(membership=membership, papel=papel, escopo=escopo,
        secretaria=secretaria, departamento=departamento, setor=setor, unidade=unidade,
        funcao=funcao, atribuido_por=actor)
    candidate.full_clean()
    same = AcessoMunicipal.objects.filter(membership=membership, papel=papel, escopo=escopo,
        secretaria=secretaria, departamento=departamento, setor=setor, unidade=unidade, funcao=funcao).first()
    if same:
        access = same
    else:
        candidate.save()
        access = candidate
    allowed = set(permissions)
    if not allowed <= PUBLIC_PERMISSION_CODES:
        raise ValidationError('Uma ou mais permissões municipais são inválidas.')
    for capability in allowed:
        required_module = PUBLIC_PERMISSION_MODULES.get(capability)
        if required_module and not product_module_enabled(actor, PRODUCT_CODE, required_module, current_organization()):
            raise ValidationError(f'A permissão {PUBLIC_PERMISSION_LABELS.get(capability, capability)} depende de módulo não contratado.')
    if not any(row.papel == AcessoMunicipal.Papel.ADMIN_PREFEITURA for row in actor_accesses):
        grantable = {code for code in PUBLIC_PERMISSION_CODES if has_municipal_permission(actor, code)}
        if not allowed <= grantable:
            raise PermissionDenied('Não é possível conceder permissões que o seu próprio acesso não possui.')
    for code in PUBLIC_PERMISSION_CODES:
        row = PermissaoMunicipal.objects.filter(acesso=access, capacidade=code).first()
        if code in allowed and row is None:
            PermissaoMunicipal.objects.create(acesso=access, capacidade=code)
        elif row and row.permitido != (code in allowed):
            row.permitido = code in allowed
            row.save(update_fields=['permitido'])
    audit_public(actor, 'ACESSO_MUNICIPAL_CONFIGURADO', access,
        {'membership': membership.pk, 'papel': papel, 'escopo': escopo, 'permissoes': sorted(allowed)})
    return access


@transaction.atomic
def create_municipal_invite(actor, *, data):
    require_municipal_permission(actor, 'invite.create')
    accesses = list(accesses_for(actor))
    if not _scope_is_within(accesses, data['papel'], data['escopo'], data.get('secretaria'),
                            data.get('departamento'), data.get('setor'), data.get('unidade')):
        raise PermissionDenied('O convite está fora do seu escopo municipal.')
    for capability in set(data.get('permissoes') or ()):
        required_module = PUBLIC_PERMISSION_MODULES.get(capability)
        if required_module and not product_module_enabled(actor, PRODUCT_CODE, required_module, current_organization()):
            raise ValidationError(f'A permissão {PUBLIC_PERMISSION_LABELS.get(capability, capability)} depende de módulo não contratado.')
    if not any(row.papel == AcessoMunicipal.Papel.ADMIN_PREFEITURA for row in accesses):
        grantable = {code for code in PUBLIC_PERMISSION_CODES if has_municipal_permission(actor, code)}
        if not set(data.get('permissoes') or ()) <= grantable:
            raise PermissionDenied('Não é possível convidar com permissões que o seu próprio acesso não possui.')
    raw = secrets.token_urlsafe(32)
    invite = ConviteMunicipal(
        organization=current_organization(), email=data['email'].strip().lower(),
        token_digest=ConviteMunicipal.digest(raw), papel=data['papel'], escopo=data['escopo'],
        secretaria=data.get('secretaria'), departamento=data.get('departamento'),
        setor=data.get('setor'), unidade=data.get('unidade'), funcao=data.get('funcao'),
        permissoes=sorted(set(data.get('permissoes') or ())), criado_por=actor,
        expira_em=timezone.now() + timedelta(hours=48),
    )
    invite.full_clean()
    invite.save()
    audit_public(actor, 'CONVITE_MUNICIPAL_CRIADO', invite,
        {'email': invite.email, 'papel': invite.papel, 'escopo': invite.escopo, 'expira_em': invite.expira_em.isoformat()})
    return invite, raw


def municipal_modules(user):
    org = current_organization()
    from platform_core.saas_models import MODULE_LABELS
    from .phase_e_services import public_module_available
    modules = ('gestao_publica', 'estrutura_municipal', 'usuarios_municipais',
               'acessos_municipais', 'convites_municipais', 'planejamento_publico',
               'orcamento_publico', 'contabilidade_pub', 'tesouraria_publica',
               'rh_municipal', 'ponto_digital', 'patrimonio', 'almoxarifado', 'frotas',
               'processo_digital', 'transparencia', 'ouvidoria', 'esic',
               'tributos_municipais', 'gestao_territorial', 'nfse_municipal', 'saude_sus',
               'educacao_municipal', 'controle_interno')
    return [
        {
            'code': code,
            'label': MODULE_LABELS.get(code, code.replace('_', ' ').title()),
            'enabled': (
                public_module_available(org, code) if code in {'processo_digital', 'transparencia', 'ouvidoria', 'esic'}
                else product_module_enabled(user, PRODUCT_CODE, code, org)
            ),
        }
        for code in modules
    ]
