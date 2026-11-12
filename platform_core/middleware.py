from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from .context import tenant_context
from .models import OrganizationMembership
from .access import erp_code_for_path, require_erp

PUBLIC_COMMERCIAL_PATHS = {'/', '/planos/', '/demonstracao/', '/contato/', '/solicitacao-recebida/', '/suporte/', '/termos/', '/privacidade/', '/login/', '/cadastro/', '/cadastro/confirmacao/', '/cadastro/primeiro-acesso/', '/robots.txt'}


def get_upgrade_info_for_path(path):
    from .modules import required_modules_for_path
    from .saas_models import PlanoSaaS

    required = required_modules_for_path(path, None)
    if not required:
        return None

    MODULE_MAP = {
        'relatorios_completos': ('Gestão Executiva & Métricas Avançadas', 'profissional'),
        'compras': ('Gestão de Compras & Cotações', 'profissional'),
        'estoque_completo': ('Controle de Lotes, Validades & Inventário', 'profissional'),
        'financeiro_completo': ('Contas Bancárias & Conciliação Avançada', 'profissional'),
        'contratos': ('Gestão de Contratos de Assistência', 'profissional'),
        'portal': ('Portal do Cliente', 'profissional'),
        'pwa_tecnico': ('Aplicativo Mobile do Técnico', 'profissional'),
        'rh_asst_basico': ('Gestão de Equipe & Ponto Eletrônico', 'profissional'),
        'rh_asst_completo': ('Horas Extras & Fechamento de Ponto Avançado', 'empresarial'),
        'pwa_vendedor': ('Aplicativo Mobile do Vendedor & Metas', 'empresarial'),
    }

    feature_name = None
    target_plan_slug = 'profissional'

    for mod in required:
        if mod in MODULE_MAP:
            name, plan_slug = MODULE_MAP[mod]
            feature_name = name
            target_plan_slug = plan_slug
            if plan_slug == 'empresarial':
                break

    if not feature_name:
        feature_name = 'Recurso Avançado do Sistema'
        target_plan_slug = 'profissional'

    plan = PlanoSaaS.objects.filter(slug=target_plan_slug, publico_ativo=True, ativo=True).first()
    return {
        'recurso_nome': feature_name,
        'plano': plan,
        'upgrade_url': f'/vertyx/empresa/plano/?upgrade={target_plan_slug}' if plan else '/vertyx/empresa/plano/',
    }


def _forbidden(request, message, title='Acesso Não Permitido'):
    from django.shortcuts import render
    from django.http import HttpResponse
    upgrade_info = get_upgrade_info_for_path(request.path)
    try:
        response = render(request, '403.html', {
            'exception': message,
            'titulo': title,
            'upgrade_info': upgrade_info,
        }, status=403)
    except Exception as exc:
        target_link = upgrade_info['upgrade_url'] if (upgrade_info and upgrade_info.get('upgrade_url')) else '/planos/'
        target_label = f"Ver Plano {upgrade_info['plano'].nome}" if (upgrade_info and upgrade_info.get('plano')) else "Conhecer Planos e Preços"
        fallback_html = f"""<!DOCTYPE html>
<html lang="pt-br" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} · VERTYX</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
  <style>
    body {{ background: #0b111d; color: #f8fafc; font-family: Inter, system-ui, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; }}
    .card-err {{ background: #111827; border: 1px solid #1f293d; border-radius: 16px; padding: 40px; max-width: 620px; width: 100%; text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.4); }}
    .icon-badge {{ width: 72px; height: 72px; border-radius: 50%; background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.35); color: #f59e0b; display: inline-flex; align-items: center; justify-content: center; font-size: 2rem; margin-bottom: 20px; }}
    .badge-pill {{ background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); font-size: 0.78rem; font-weight: 700; padding: 6px 14px; border-radius: 99px; display: inline-block; margin-bottom: 16px; text-transform: uppercase; }}
    h1 {{ font-size: 1.5rem; font-weight: 700; color: #ffffff; margin-bottom: 12px; }}
    p {{ color: #94a3b8; font-size: 0.95rem; line-height: 1.6; margin-bottom: 24px; }}
    .upgrade-box {{ background: rgba(37, 99, 235, 0.1); border: 1px solid rgba(37, 99, 235, 0.3); border-radius: 12px; padding: 18px; margin-bottom: 24px; text-align: left; }}
    .upgrade-box h2 {{ font-size: 1rem; font-weight: 700; color: #60a5fa; margin-bottom: 4px; }}
    .upgrade-box p {{ font-size: 0.85rem; color: #cbd5e1; margin-bottom: 0; }}
    .btn-plan {{ background: #2563eb; color: #fff; font-weight: 700; border: none; padding: 10px 24px; border-radius: 8px; text-decoration: none; display: inline-block; }}
    .btn-plan:hover {{ background: #1d4ed8; color: #fff; }}
    .btn-back {{ background: transparent; color: #94a3b8; border: 1px solid #334155; padding: 10px 20px; border-radius: 8px; text-decoration: none; display: inline-block; margin-left: 10px; font-weight: 600; }}
    .btn-back:hover {{ color: #fff; border-color: #64748b; }}
  </style>
</head>
<body>
  <div class="card-err">
    <div class="icon-badge"><i class="bi bi-shield-lock-fill"></i></div>
    <div><span class="badge-pill"><i class="bi bi-exclamation-octagon me-1"></i> HTTP 403 · Acesso Restrito</span></div>
    <h1>{title}</h1>
    <p>{message}</p>
    <div class="upgrade-box">
      <h2><i class="bi bi-stars me-1"></i> Recurso sob Plano VERTYX</h2>
      <p>Este recurso não faz parte do pacote contratado da sua organização. Conheça nossos planos para liberá-lo imediatamente.</p>
    </div>
    <div>
      <a href="{target_link}" class="btn-plan"><i class="bi bi-arrow-up-circle-fill me-1"></i> {target_label}</a>
      <a href="/assistencia/" class="btn-back"><i class="bi bi-house-door me-1"></i> Voltar ao Início</a>
    </div>
  </div>
</body>
</html>"""
        response = HttpResponse(fallback_html, status=403)
    response['Cache-Control'] = 'private, no-store'
    return response


class OrganizationMiddleware:
    """Seleciona apenas memberships válidos; a sessão nunca é autoridade por si só."""
    def __init__(self, get_response):
        self.get_response = get_response

    def process_view(self, request, view_func, view_args, view_kwargs):
        # SaaS administration has its own platform capability. Tenant role
        # routing is not an authority for these routes; the SaaS view guard is.
        if request.path.startswith('/saas/'):
            return None
        membership = getattr(request, 'membership', None)
        product_specific_route = request.path.startswith(('/gestao-publica/', '/loja-hospitalar/'))
        if membership and membership.role in {'VENDEDOR','PDV'} and request.path not in PUBLIC_COMMERCIAL_PATHS and not product_specific_route:
            from operacao.seller_access import route_allowed
            if not route_allowed(request.resolver_match,request.user):
                return _forbidden(request, 'Acesso restrito à Central de Vendas.', 'Central de Vendas')
        if membership and membership.role == 'TECNICO' and request.path not in PUBLIC_COMMERCIAL_PATHS and not product_specific_route:
            from operacao.technician_access import route_allowed
            if request.path != '/' and not route_allowed(request.resolver_match):
                return _forbidden(request, 'Acesso restrito à operação técnica. Volte para Meus atendimentos.', 'Operação Técnica')

    def __call__(self, request):
        request.organization = None
        request.membership = None
        if request.user.is_authenticated:
            memberships = OrganizationMembership.objects.filter(user=request.user, status='ATIVO', organization__status='ATIVA').select_related('organization')
            selected = request.session.get('organization_id')
            membership = memberships.filter(organization_id=selected).first() if isinstance(selected, int) else None
            if membership is None:
                request.session.pop('organization_id', None)
                candidates = list(memberships[:2])
                if len(candidates) == 1:
                    membership = candidates[0]
                    request.session['organization_id'] = membership.organization_id
            if membership:
                request.organization = membership.organization
                request.membership = membership
        # Todo endpoint fora de plataforma/login/admin/static é Assistência e exige ERP.
        public = request.path.startswith(('/vertyx/', '/contas/login/', '/contas/logout/', '/admin/', '/static/', '/saas/', '/api/bancos/webhook/', '/primeiro-acesso/', '/gestao-publica/convites/aceitar/', '/gestao-publica/publico/', '/gestao-publica/saude/', '/gestao-publica/cidadao/')) or request.path in PUBLIC_COMMERCIAL_PATHS | {'/health/', '/manifest.webmanifest', '/manifest-pdv.webmanifest', '/sw.js','/pdv/manifest.webmanifest','/vendedor/manifest.webmanifest','/tecnico/manifest.webmanifest','/pdv/sw.js','/vendedor/sw.js','/tecnico/sw.js','/pdv/entrar/','/vendedor/entrar/','/gestao-publica/','/gestao-publica/login/','/gestao-publica/manifest.webmanifest','/gestao-publica/sw.js'}
        if request.path.startswith('/admin/') and request.user.is_authenticated and not request.user.is_superuser:
            return _forbidden(request, 'O Django Admin pertence à operação VERTYX. Use a administração da organização no ERP.', 'Administração VERTYX')
        if not public:
            if not request.user.is_authenticated:
                login='operacao:pdv_login' if request.path.startswith('/pdv/') else 'operacao:seller_login' if request.path.startswith('/vendedor/') else 'contas:login'
                return redirect_to_login(request.get_full_path(), reverse(login))
            if request.organization is None:
                return redirect('platform_core:home')
            if request.membership.role in ['PAINEL', 'PAINEL_TV'] and not request.path.startswith('/painel-operacional/'):
                return _forbidden(request, 'Acesso restrito ao painel operacional.', 'Painel Operacional')
            import re
            equipment_qr = bool(re.fullmatch(r'/equipamentos/qr/[0-9a-fA-F-]{36}/', request.path))
            if request.membership.role == 'CLIENTE' and not request.path.startswith('/portal/') and not equipment_qr:
                return _forbidden(request, 'Acesso restrito ao portal do cliente.', 'Portal do Cliente')
            product_code = erp_code_for_path(request.path)
            if product_code is None:
                return _forbidden(request, 'Esta rota ainda não está associada a um produto VERTYX autorizado.', 'Rota Não Autorizada')
            try:
                require_erp(request.user, request.organization, code=product_code)
            except PermissionDenied:
                return _forbidden(request, 'Produto ou acesso do usuário não autorizado nesta organização.', 'Permissão Insuficiente')
            with tenant_context(request.organization):
                if not public:
                    from .modules import product_module_enabled, required_modules_for_path
                    required = required_modules_for_path(request.path, request.resolver_match)
                    if any(not product_module_enabled(request.user, product_code, code, request.organization) for code in required):
                        return _forbidden(request, 'Módulo não contratado ou desabilitado nesta organização.', 'Módulo Não Contratado')
                response = self.get_response(request)
        else:
            with tenant_context(request.organization):
                response = self.get_response(request)
        if request.path != '/health/' and not request.path.startswith(('/static/', '/sw.js', '/manifest.webmanifest', '/manifest-pdv.webmanifest')):
            response['Cache-Control'] = 'private, no-store'
        return response
