from platform_core.access import has_erp_access, effective_role, require_erp

ROLES = {
    'estoque': ('ADMIN', 'GESTOR', 'ESTOQUE', 'TECNICO'),
    'estoque_escrever': ('ADMIN', 'GESTOR', 'ESTOQUE'),
    'orcamentos': ('ADMIN', 'GESTOR'),
    'contratos': ('ADMIN', 'GESTOR'),
    'planejamento': ('ADMIN', 'GESTOR', 'TECNICO', 'PLANEJADOR', 'ATENDIMENTO'),
    'financeiro': ('ADMIN', 'GESTOR', 'FINANCEIRO', 'CONTABILIDADE'),
    'financeiro_escrever': ('ADMIN', 'GESTOR', 'FINANCEIRO'),
    'relatorios': ('ADMIN', 'GESTOR', 'TECNICO', 'ESTOQUE', 'FINANCEIRO', 'CONTABILIDADE'),
}


def allowed(user, permission):
    from platform_core.modules import has_capability, product_module_enabled, current_organization
    if not has_capability(user, permission, ROLES.get(permission, ())):
        return False
    if permission in {'financeiro', 'financeiro_escrever'}:
        org = current_organization()
        if org and not product_module_enabled(user, 'assistencia', 'financeiro_basico', org):
            return False
    return True


def require(user, permission):
    from platform_core.modules import require_capability
    return require_capability(user, permission, ROLES.get(permission,()))
