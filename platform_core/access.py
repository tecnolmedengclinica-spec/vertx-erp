from django.core.exceptions import PermissionDenied
from .context import current_organization
from .models import OrganizationMembership, OrganizationERP, MembershipERP


def membership_for(user, organization=None):
    organization = organization or current_organization()
    if organization is None or not user.is_authenticated or not user.is_active or organization.status != 'ATIVA':
        return None
    return OrganizationMembership.objects.filter(user=user, organization=organization, status='ATIVO').first()


def has_erp_access(user, organization=None, code='assistencia'):
    membership = membership_for(user, organization)
    if membership is None:
        return False
    return OrganizationERP.objects.filter(organization=membership.organization, erp__code=code, erp__active=True, enabled=True).exists() and MembershipERP.objects.filter(membership=membership, erp__code=code, erp__active=True, enabled=True).exists()


PRODUCT_ROUTE_PREFIXES = (
    # Municipal routes are a separate product and never fall through to the
    # business ERP.
    ('/gestao-publica/', 'gestao-publica'),
    ('/loja-hospitalar/', 'loja-hospitalar'),
    # Explicit compatibility map for the currently shipped assistance ERP.
    # PDV and seller experiences remain here until a separately approved
    # product entitlement migration moves organizations to Comercio e PDV.
    ('/assistencia/', 'assistencia'),
    ('/gestao/', 'assistencia'),
    ('/clientes/', 'assistencia'),
    ('/equipamentos/', 'assistencia'),
    ('/ordens/', 'assistencia'),
    ('/chamados/', 'assistencia'),
    ('/preventivas/', 'assistencia'),
    ('/planejamento/', 'assistencia'),
    ('/tecnico/', 'assistencia'),
    ('/portal/', 'assistencia'),
    ('/portal-acessos/', 'assistencia'),
    ('/conhecimento/', 'assistencia'),
    ('/pdv/', 'assistencia'),
    ('/vendedor/', 'assistencia'),
    ('/vendas/', 'assistencia'),
    ('/estoque/', 'assistencia'),
    ('/depositos/', 'assistencia'),
    ('/inventarios/', 'assistencia'),
    ('/transferencias/', 'assistencia'),
    ('/compras/', 'assistencia'),
    ('/orcamentos/', 'assistencia'),
    ('/categorias-pecas/', 'assistencia'),
    ('/checklists/', 'assistencia'),
    ('/configuracoes/', 'assistencia'),
    ('/empresa/', 'assistencia'),
    ('/financeiro/', 'assistencia'),
    ('/bancos/', 'assistencia'),
    ('/relatorios/', 'assistencia'),
    ('/documentos/', 'assistencia'),
    ('/fiscal/', 'assistencia'),
    ('/rh/', 'assistencia'),
    ('/ponto/', 'assistencia'),
    ('/integracoes/', 'assistencia'),
    ('/contratos/', 'assistencia'),
    ('/painel-operacional/', 'assistencia'),
    # Operational notifications are served by the business ERP and still
    # enforce the user's active membership and role in the view.
    ('/notificacoes/', 'assistencia'),
    ('/pesquisa/', 'assistencia'),
    ('/contas/', 'assistencia'),
    ('/media/', 'assistencia'),
)


def erp_code_for_path(path):
    """Return an explicitly registered product or None for an unknown route."""
    return next((code for prefix, code in PRODUCT_ROUTE_PREFIXES if path.startswith(prefix)), None)


def require_erp(user, organization=None, roles=None, code='assistencia'):
    membership = membership_for(user, organization)
    if membership is None or not has_erp_access(user, organization, code=code) or roles is not None and membership.role not in roles:
        raise PermissionDenied('Organização, acesso ao ERP ou permissão insuficiente.')
    return membership


def effective_role(user):
    membership = membership_for(user)
    return membership.role if membership else ''
