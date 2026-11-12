"""One module/capability policy for routes and services; legacy defaults preserved."""
from django.core.exceptions import PermissionDenied
from .access import membership_for, has_erp_access
from .context import current_organization
from .models import (
    ERPProductModule, ItemContratoSaaS, ModuloOrganizacao,
    OrganizationERP, MembershipERP, PermissaoMembership,
)


def active_contract_items(organization, erp_code=None, *, today=None):
    """Return only contract lines valid today; callers must fail closed on conflicts."""
    from django.db.models import Q
    from django.utils import timezone
    today = today or timezone.localdate()
    rows = ItemContratoSaaS.objects.filter(
        contrato__organization=organization, contrato__status='ATIVO', ativo=True,
        erp__active=True,
    ).filter(
        Q(contrato__inicio_em__isnull=True) | Q(contrato__inicio_em__lte=today),
    ).filter(
        Q(contrato__fim_em__isnull=True) | Q(contrato__fim_em__gte=today),
    )
    if erp_code:
        rows = rows.filter(erp__code=erp_code)
    return rows.select_related('erp', 'plano', 'contrato')

REPORTING_RANK = {'BASICO': 1, 'AVANCADO': 2, 'COMPLETO': 3}
STRICT_PUBLIC_MODULES = {
    'estrutura_municipal', 'usuarios_municipais', 'acessos_municipais',
    'convites_municipais', 'planejamento_publico', 'orcamento_publico',
    'contabilidade_pub', 'tesouraria_publica',
    'compras_publicas', 'licitacoes_publicas',
    'registro_precos', 'contratos_publicos', 'ponto_digital',
    'patrimonio', 'almoxarifado', 'frotas',
    'processo_digital', 'transparencia', 'ouvidoria', 'esic',
    'tributos_municipais', 'gestao_territorial', 'nfse_municipal',
    'saude_sus',
    'educacao_municipal',
    'controle_interno',
}


def analytics_level(organization=None, erp_code=None):
    """Return analytics depth for one contracted ERP; legacy callers may omit it."""
    organization = organization or current_organization()
    if organization is None:
        return None
    from .saas_models import AssinaturaSaaS
    if erp_code:
        from django.db.models import Q
        from django.utils import timezone
        from .models import ItemContratoSaaS
        today = timezone.localdate()
        active_lines = list(active_contract_items(organization, erp_code, today=today)[:2])
        if len(active_lines) > 1:
            # Conflicting active contracts must never accidentally expose richer analytics.
            return 'BASICO'
        if active_lines:
            return active_lines[0].nivel_indicadores_snapshot
        # Once a product was moved to contract control, an expired, suspended,
        # or inactive line must not fall back to an unrelated legacy plan.
        if ItemContratoSaaS.objects.filter(
            contrato__organization=organization, erp__code=erp_code,
            contrato__status__in=['ATIVO', 'SUSPENSO', 'ENCERRADO'],
        ).exists():
            return 'BASICO'
    subscription = (AssinaturaSaaS.objects.filter(organization=organization)
                    .select_related('plano').first())
    if subscription:
        plan = subscription.plano
        if erp_code and plan.erp_id and plan.erp.code != erp_code:
            return 'BASICO'
        if erp_code and plan.erp_id is None and erp_code != 'assistencia':
            return 'BASICO'
        return plan.nivel_indicadores
    return 'COMPLETO'


def analytics_at_least(level, organization=None, erp_code=None):
    current = analytics_level(organization, erp_code=erp_code)
    return current is not None and REPORTING_RANK.get(current, 0) >= REPORTING_RANK[level]

CAPABILITIES = {
    'migracao': ('', ('ADMIN', 'GESTOR')),
    'fiscal_consultar': ('fiscal', ('ADMIN', 'GESTOR', 'FINANCEIRO', 'CONTABILIDADE')),
    'fiscal_emitir': ('fiscal', ('ADMIN', 'GESTOR', 'FINANCEIRO')),
    'fiscal_cancelar': ('fiscal', ('ADMIN', 'GESTOR', 'FINANCEIRO')),
    'fiscal_configurar': ('fiscal', ('ADMIN', 'GESTOR')),
    'fiscal_gerenciar_credenciais': ('fiscal', ('ADMIN',)),
    'estoque': ('estoque', ('ADMIN', 'GESTOR', 'ESTOQUE', 'TECNICO')),
    'estoque_escrever': ('estoque', ('ADMIN', 'GESTOR', 'ESTOQUE')),
    'inventario_aprovar': ('estoque', ('ADMIN', 'GESTOR')),
    'compras': ('compras', ('ADMIN', 'GESTOR', 'ESTOQUE', 'COMPRAS')),
    'pdv': ('pdv', ('ADMIN', 'GESTOR', 'PDV')),
    'operar_pdv': ('pdv', ('ADMIN', 'GESTOR', 'PDV')),
    'central_vendedor': ('', ('ADMIN', 'GESTOR', 'PDV', 'VENDEDOR')),
    'pdv_cliente_cadastrar': ('pdv', ('ADMIN', 'GESTOR', 'PDV', 'VENDEDOR')),
    'desconto_supervisor': ('pdv', ('ADMIN', 'GESTOR')),
    'devolucao': ('pdv', ('ADMIN', 'GESTOR')),
    'caixa_supervisor': ('pdv', ('ADMIN', 'GESTOR', 'FINANCEIRO')),
    'rh': ('rh', ('ADMIN', 'GESTOR', 'RH')),
    'ponto': ('ponto', ('ADMIN', 'GESTOR', 'TECNICO', 'ESTOQUE', 'PDV', 'VENDEDOR', 'COMPRAS', 'FINANCEIRO', 'RH', 'PLANEJADOR', 'ATENDIMENTO', 'CONTABILIDADE')),
    'ajuste_ponto': ('ponto', ('ADMIN', 'GESTOR', 'RH')),
    'config_empresa': ('', ('ADMIN',)),
    'executivo': ('', ('ADMIN','GESTOR')),
    'credito_cliente': ('', ('ADMIN','GESTOR')),
    'financeiro': ('financeiro', ('ADMIN', 'GESTOR', 'FINANCEIRO', 'CONTABILIDADE')),
    'financeiro_escrever': ('financeiro', ('ADMIN', 'GESTOR', 'FINANCEIRO')),
    'contratos': ('contratos', ('ADMIN', 'GESTOR')),
    'orcamentos': ('pdv', ('ADMIN', 'GESTOR')),
    'planejamento': ('assistencia', ('ADMIN', 'GESTOR', 'TECNICO', 'PLANEJADOR', 'ATENDIMENTO')),
    'operacional': ('assistencia', ('ADMIN', 'GESTOR', 'TECNICO')),
    'gestao': ('assistencia', ('ADMIN', 'GESTOR')),
    'portal': ('portal', ('CLIENTE',)),
    'relatorios': ('', ('ADMIN', 'GESTOR', 'TECNICO', 'ESTOQUE', 'FINANCEIRO', 'CONTABILIDADE')),
}

MODULE_IMPLIED_BY = {
    'estoque_basico': {'estoque_completo', 'estoque'},
    'estoque_completo': {'estoque'},
    'estoque': {'estoque_basico', 'estoque_completo'},
    'financeiro_basico': {'financeiro_completo', 'financeiro'},
    'financeiro_completo': {'financeiro'},
    'financeiro': {'financeiro_basico', 'financeiro_completo'},
    'rh_asst_basico': {'rh_asst_completo', 'rh', 'ponto'},
    'rh_asst_completo': {'rh'},
    'rh': {'rh_asst_basico', 'rh_asst_completo', 'ponto'},
    'ponto': {'rh_asst_basico', 'rh_asst_completo'},
    'documentos_basicos': {'documentos_completos', 'documentos'},
    'documentos_completos': {'documentos'},
    'documentos': {'documentos_basicos', 'documentos_completos'},
    'relatorios_basicos': {'relatorios_completos'},
}

MODULE_EXPANSION_IMPLICATIONS = {
    'estoque_completo': {'estoque_basico', 'estoque'},
    'estoque_basico': {'estoque'},
    'estoque': {'estoque_basico', 'estoque_completo'},
    'financeiro_completo': {'financeiro_basico', 'financeiro'},
    'financeiro_basico': {'financeiro'},
    'financeiro': {'financeiro_basico', 'financeiro_completo'},
    'rh_asst_completo': {'rh_asst_basico', 'rh', 'ponto'},
    'rh_asst_basico': {'rh', 'ponto'},
    'rh': {'rh_asst_basico', 'rh_asst_completo', 'ponto'},
    'ponto': {'rh_asst_basico'},
    'documentos_completos': {'documentos_basicos', 'documentos'},
    'documentos_basicos': {'documentos'},
    'documentos': {'documentos_basicos', 'documentos_completos'},
    'relatorios_completos': {'relatorios_basicos'},
}

LEGACY_COMPAT_MODULE_CODES = frozenset({'estoque', 'financeiro', 'ponto', 'rh', 'documentos'})


def platform_module_overrides(organization):
    """Return {codigo: modo} manual platform adjustments for one organization."""
    if organization is None:
        return {}
    from .saas_models import AjusteModuloSaaS
    return dict(
        AjusteModuloSaaS.objects.filter(organization=organization).values_list('codigo', 'modo')
    )


def platform_module_override(organization, code):
    """Return manual platform override ('LIBERADO' / 'BLOQUEADO' / None) for one module."""
    if organization is None or not code:
        return None
    from .saas_models import AjusteModuloSaaS
    return (
        AjusteModuloSaaS.objects.filter(organization=organization, codigo=code)
        .values_list('modo', flat=True)
        .first()
    )


def expand_declared_modules(codes):
    """Expand plan/contract declarations into their implied module and legacy alias codes."""
    expanded = set(codes or ())
    for code in tuple(expanded):
        expanded.update(MODULE_EXPANSION_IMPLICATIONS.get(code, ()))
    return expanded


def effective_legacy_plan_codes(organization, plan):
    """Return plan + core codes adjusted by explicit VERTYX Admin client overrides."""
    from .saas_models import AjusteModuloSaaS, CORE_SAAS_MODULES
    overrides = platform_module_overrides(organization)
    allows = {code for code, mode in overrides.items() if mode == AjusteModuloSaaS.Modo.ALLOW}
    denies = {code for code, mode in overrides.items() if mode == AjusteModuloSaaS.Modo.DENY}
    base = (set(plan.modulos or ()) | set(CORE_SAAS_MODULES)) if plan is not None else set(CORE_SAAS_MODULES)
    return (base - denies) | allows


def baseline_effective_modules(organization):
    """Return modules granted by plan/contracts/legacy defaults before manual client overrides."""
    if organization is None:
        return set()
    from .saas_models import AssinaturaSaaS, CORE_SAAS_MODULES

    enabled = set()
    active_erps = set(
        OrganizationERP.objects.filter(
            organization=organization, erp__active=True, enabled=True,
        ).values_list('erp__code', flat=True)
    )
    contracted_erps = set(
        ItemContratoSaaS.objects.filter(
            contrato__organization=organization,
            contrato__status__in=['ATIVO', 'SUSPENSO', 'ENCERRADO'],
        ).values_list('erp__code', flat=True)
    )
    for erp_code in active_erps & contracted_erps:
        active_lines = list(active_contract_items(organization, erp_code)[:2])
        if len(active_lines) == 1:
            enabled.update(active_lines[0].modulos_snapshot or ())

    subscription = (
        AssinaturaSaaS.objects.filter(organization=organization)
        .select_related('plano', 'plano__erp')
        .first()
    )
    if subscription is not None:
        plan = subscription.plano
        if plan.erp_id:
            if plan.erp.code not in contracted_erps and plan.erp.code in active_erps:
                enabled.update(plan.modulos_produto.values_list('modulo__code', flat=True))
        else:
            enabled.update(expand_declared_modules(set(plan.modulos or ()) | set(CORE_SAAS_MODULES)))
    else:
        if 'gestao-publica' in active_erps and 'gestao-publica' not in contracted_erps:
            enabled.add('gestao_publica')
        if 'assistencia' in active_erps and 'assistencia' not in contracted_erps:
            assist_codes = set(
                ERPProductModule.objects.filter(
                    erp__code='assistencia', erp__active=True, active=True,
                ).values_list('code', flat=True)
            )
            enabled.update(assist_codes | set(CORE_SAAS_MODULES))
    return enabled


def module_enabled(code):
    if not code:
        return True
    organization = current_organization()
    if organization is None:
        return False
    from .saas_models import AjusteModuloSaaS, AssinaturaSaaS, CORE_SAAS_MODULES
    overrides = platform_module_overrides(organization)
    if overrides.get(code) == AjusteModuloSaaS.Modo.DENY:
        return False
    allows = {k for k, v in overrides.items() if v == AjusteModuloSaaS.Modo.ALLOW}
    denies = {k for k, v in overrides.items() if v == AjusteModuloSaaS.Modo.DENY}
    subscription = AssinaturaSaaS.objects.filter(organization=organization).select_related('plano').first()
    if subscription is not None:
        core = set(CORE_SAAS_MODULES) - denies
        plan_codes = effective_legacy_plan_codes(organization, subscription.plano)
        entitled = code in plan_codes or bool(MODULE_IMPLIED_BY.get(code, set()) & plan_codes)
        if not entitled:
            return False
        # The tenant settings table may contain an old false row from before
        # the core policy. Plan-level core availability is no longer optional.
        if code in core or code in allows:
            return True
        # Tenant-level module settings may restrict a contracted feature, but
        # can never grant a feature omitted from the subscription.
        row = ModuloOrganizacao.objects.filter(organization=organization, codigo=code).first()
        return row.habilitado if row else True
    if overrides.get(code) == AjusteModuloSaaS.Modo.ALLOW or (MODULE_IMPLIED_BY.get(code, set()) & allows):
        return True
    # No SaaS subscription and no disabled flag means legacy access, preserving
    # installations predating the commercial-plan entitlement model.
    return not ModuloOrganizacao.objects.filter(organization=organization, codigo=code, habilitado=False).exists()


def product_module_enabled(user, erp_code, module_code, organization=None):
    """Fail-closed check for product -> membership -> module entitlement.

    Legacy tenant plans remain compatible for the two already operational
    verticals. New product plans/contracts must explicitly include the module.
    This function deliberately does not infer entitlement from a role or price.
    """
    organization = organization or current_organization()
    membership = membership_for(user, organization)
    if membership is None:
        return False
    product_granted = OrganizationERP.objects.filter(
        organization=organization, erp__code=erp_code, erp__active=True, enabled=True,
    ).exists()
    member_granted = MembershipERP.objects.filter(
        membership=membership, erp__code=erp_code, erp__active=True, enabled=True,
    ).exists()
    if not (product_granted and member_granted):
        return False

    module = ERPProductModule.objects.filter(
        erp__code=erp_code, erp__active=True, code=module_code, active=True,
    ).first()
    if module is None:
        return False

    from .saas_models import AjusteModuloSaaS
    override = platform_module_override(organization, module_code)
    if override == AjusteModuloSaaS.Modo.DENY:
        return False

    items = active_contract_items(organization, erp_code)
    active_items = list(items[:2])
    if len(active_items) > 1:
        # Until a contract-version precedence rule is activated, conflicting
        # active lines fail closed instead of guessing which plan wins.
        return False
    item = active_items[0] if active_items else None
    if item is None and ItemContratoSaaS.objects.filter(
        contrato__organization=organization, erp__code=erp_code,
        contrato__status__in=['ATIVO', 'SUSPENSO', 'ENCERRADO'],
    ).exists():
        return False
    if override == AjusteModuloSaaS.Modo.ALLOW:
        return True
    if item:
        # A contract snapshot is authoritative, including an intentionally
        # empty list; later plan edits cannot silently expand that contract.
        available = module_code in item.modulos_snapshot
    else:
        from .saas_models import AssinaturaSaaS
        subscription = AssinaturaSaaS.objects.filter(organization=organization).select_related('plano', 'plano__erp').first()
        if subscription:
            plan = subscription.plano
            if plan.erp_id:
                if plan.erp.code != erp_code:
                    return False
                plan_modules = set(plan.modulos_produto.values_list('modulo__code', flat=True))
                available = module_code in plan_modules
            else:
                # Compatibility only for existing products; a legacy plan is
                # never silently reused to grant a future vertical.
                available = (
                    erp_code in {'assistencia', 'gestao-publica'}
                    and not (erp_code == 'gestao-publica' and module_code in STRICT_PUBLIC_MODULES)
                    and module_enabled(module_code,)
                )
        else:
            # Installations predating SaaS subscriptions keep explicit product
            # grants and organization-level module overrides.
            available = not (
                erp_code == 'gestao-publica' and module_code in STRICT_PUBLIC_MODULES
            ) and not ModuloOrganizacao.objects.filter(
                organization=organization, codigo=module_code, habilitado=False,
            ).exists()
    if not available:
        return False
    row = ModuloOrganizacao.objects.filter(organization=organization, codigo=module_code).first()
    return True if row is None else row.habilitado

def has_capability(user, code, roles=None):
    membership = membership_for(user)
    if membership is None or not has_erp_access(user):
        return False
    if code.startswith('fiscal_') and membership.role not in {'ADMIN', 'GESTOR', 'FINANCEIRO', 'CONTABILIDADE'}:
        return False
    if code=='pdv':
        return has_capability(user,'operar_pdv')
    if code=='pdv_cliente_cadastrar' and not has_capability(user,'operar_pdv'):
        return False
    if code=='operar_pdv':
        if membership.role not in {'ADMIN','GESTOR','PDV','VENDEDOR'} or not module_enabled('pdv'):
            return False
        override=PermissaoMembership.objects.filter(membership=membership,capacidade='operar_pdv').first()
        if override:
            return override.permitido
        # Preserve explicit legacy restrictions for existing cash operators.
        # A seller requires the NEW explicit grant; legacy pdv never grants it.
        if membership.role=='VENDEDOR':
            return False
        legacy=PermissaoMembership.objects.filter(membership=membership,capacidade='pdv').first()
        return legacy.permitido if legacy else membership.role in CAPABILITIES['operar_pdv'][1]
    if membership.role in {'VENDEDOR','PDV'} and code in {
        'estoque','estoque_escrever','inventario_aprovar','compras','desconto_supervisor',
        'devolucao','caixa_supervisor','rh','ajuste_ponto','config_empresa','executivo',
        'credito_cliente','financeiro','financeiro_escrever','contratos','orcamentos',
        'planejamento','operacional','gestao','relatorios',
    }:
        return False
    if membership.role == 'TECNICO' and code in {
        'estoque_escrever','inventario_aprovar','compras','pdv','desconto_supervisor',
        'devolucao','caixa_supervisor','rh','ajuste_ponto','config_empresa','executivo',
        'credito_cliente','financeiro','financeiro_escrever','contratos','orcamentos','gestao',
    }:
        return False
    module, defaults = CAPABILITIES.get(code, ('', roles or ()))
    if not module_enabled(module):
        return False
    override = PermissaoMembership.objects.filter(membership=membership, capacidade=code).first()
    return override.permitido if override else membership.role in (defaults if code in CAPABILITIES else roles or ())

def require_capability(user, code, roles=None):
    if not has_capability(user, code, roles):
        raise PermissionDenied('Módulo ou capacidade não autorizada nesta organização.')
    return membership_for(user)

def required_modules_for_path(path, resolver_match=None):
    """Return all plan entitlements required by a route, from most specific to general."""
    name = getattr(resolver_match, 'url_name', '') or ''
    municipal_modules = (
        ('/gestao-publica/tributos/', 'tributos_municipais'),
        ('/gestao-publica/territorio/', 'gestao_territorial'),
        ('/gestao-publica/nfse/', 'nfse_municipal'),
        ('/gestao-publica/saude/', 'saude_sus'),
        ('/gestao-publica/processos/', 'processo_digital'),
        ('/gestao-publica/transparencia/', 'transparencia'),
        ('/gestao-publica/ouvidoria/', 'ouvidoria'),
        ('/gestao-publica/esic/', 'esic'),
        ('/gestao-publica/publico/', None),
        ('/gestao-publica/compras/', 'compras_publicas'),
        ('/gestao-publica/licitacoes/', 'licitacoes_publicas'),
        ('/gestao-publica/registro-precos/', 'registro_precos'),
        ('/gestao-publica/contratos/', 'contratos_publicos'),
        ('/gestao-publica/ponto/', 'ponto_digital'),
        ('/gestao-publica/ponto-digital/', 'ponto_digital'),
        ('/gestao-publica/patrimonio/', 'patrimonio'),
        ('/gestao-publica/almoxarifado/', 'almoxarifado'),
        ('/gestao-publica/frotas/', 'frotas'),
        ('/gestao-publica/rh-municipal/', 'rh_municipal'),
        ('/gestao-publica/planejamento/', 'planejamento_publico'),
        ('/gestao-publica/orcamento/', 'orcamento_publico'),
        ('/gestao-publica/contabilidade/', 'contabilidade_pub'),
        ('/gestao-publica/tesouraria/', 'tesouraria_publica'),
    )
    for prefix, module in municipal_modules:
        if path.startswith(prefix):
            return ('gestao_publica', module) if module else ('gestao_publica',)
    if path.startswith('/gestao-publica/rh/'):
        return ('gestao_publica', 'rh_municipal')
    if path.startswith('/gestao-publica/'):
        return ('gestao_publica',)
    if path.startswith('/vendedor/'):
        return ('pwa_vendedor',)
    if path.startswith('/gestao/executiva/'):
        return ('relatorios_completos',)
    if path.startswith('/clientes/'):
        return ('clientes',)
    if path.startswith('/tecnico/'):
        return ('assistencia', 'pwa_tecnico')
    if path.startswith('/estoque/fornecedores/'):
        return ('compras',)
    if path.startswith(('/inventarios/', '/transferencias/', '/estoque/lotes/',
                        '/estoque/reservas/', '/estoque/necessidades/',
                        '/estoque/ocorrencias/')):
        return ('estoque_completo',)
    if path.startswith('/estoque/relatorios/'):
        return ('estoque_basico', 'relatorios_basicos')
    if path.startswith(('/configuracoes/estoque', '/depositos/')):
        return ('estoque_basico',)
    if path.startswith('/estoque/'):
        return ('produtos', 'estoque_basico')
    if path.startswith(('/pdv/', '/vendas/')):
        return ('pdv',)
    if path.startswith('/compras/'):
        return ('compras',)
    if path.startswith('/bancos/'):
        return ('financeiro_completo',)
    if path.startswith('/financeiro/'):
        if path.startswith('/financeiro/movimentacoes/'):
            return ('financeiro_basico', 'relatorios_basicos')
        return ('financeiro_basico',)
    if path.startswith(('/rh/', '/ponto/')):
        if any(marker in path for marker in ('horas-extras', '/ajustes/', '/fechamento/')):
            return ('rh_asst_completo',)
        return ('rh_asst_basico',)
    if path.startswith('/documentos/'):
        if name in {'documento_novo', 'documento_editar', 'documento_acao'}:
            return ('documentos_completos',)
        return ('documentos_basicos',)
    if path.startswith('/relatorios/'):
        return ('relatorios_completos' if path.rstrip('/') != '/relatorios' else 'relatorios_basicos',)
    mapping = [('/portal/', 'portal'), ('/contratos/', 'contratos'), ('/fiscal/', 'fiscal'),
               ('/contas/', 'assistencia'),
               ('/ordens/', 'assistencia'), ('/chamados/', 'assistencia'),
               ('/preventivas/', 'assistencia'), ('/planejamento/', 'assistencia'),
               ('/equipamentos/', 'assistencia'), ('/painel-operacional/', 'assistencia')]
    code = next((module for prefix, module in mapping if path.startswith(prefix)), '')
    # Compatibility for integrations that still expect the former single-key API.
    return (code,) if code else ()


def module_for_path(path):
    """Backward-compatible single-key lookup; middleware uses all requirements."""
    required = required_modules_for_path(path)
    return required[0] if required else ''
