from datetime import timedelta
import hmac
import ipaddress
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import SetPasswordForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import (
    ERPProductModule, ERPProductResource, ERPCatalog, MembershipERP, ModuloOrganizacao, Organization,
    OrganizationERP, OrganizationMembership,
)
from .context import tenant_context
from .saas_models import (
    AdministradorPlataforma, AssinaturaSaaS, AuditoriaSaaS,
    ContratoSaaS, HistoricoPlanoSaaS, ItemContratoSaaS, PlanoSaaS,
    PrimeiroAcessoSaaS,
)
from .saas_forms import (
    AdminInicialForm, AlterarPlanoForm, BootstrapPrefeituraForm, ContratoSaaSForm,
    DadosEmpresaForm, ERPCatalogForm, ERPProductModuleForm, ERPProductResourceForm,
    PrimeiroAdministradorPlataformaForm,
    NovaPrefeituraHomologacaoForm,
    EditarClienteForm, EditarModulosClienteForm, PlanoSaaSForm,
)
from .saas_services import (
    alterar_plano, alterar_status_cliente, atualizar_cliente,
    atualizar_modulos_cliente, alterar_status_contrato,
    client_module_catalog_context, criar_cliente, mrr_amount, plano_snapshot, registrar,
    restaurar_modulos_cliente, verificar_limite,
    bootstrap_administrador_prefeitura, metricas_uso, metricas_uso_por_produto, reemitir_primeiro_acesso,
    uso_quota, criar_prefeitura_homologacao,
)


def _platform_admin(user):
    return bool(user.is_authenticated and user.is_active and AdministradorPlataforma.objects.filter(user=user, active=True).exists())


def _guard(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('contas:login')}?next={quote(request.get_full_path())}")
    if not _platform_admin(request.user):
        response = HttpResponseForbidden('Acesso restrito à administração da plataforma VERTYX.')
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'no-referrer'
        return response
    return None


def _guard_bootstrap(request):
    """Bootstrap is a platform operation, never a tenant membership action."""
    if not request.user.is_authenticated:
        return redirect(f"{reverse('contas:login')}?next={quote(request.get_full_path())}")
    if not request.user.is_active or not (
        request.user.is_superuser or _platform_admin(request.user)
    ):
        response = HttpResponseForbidden('Somente a administração interna VERTYX pode conceder o primeiro acesso municipal.')
        response['Cache-Control'] = 'private, no-store'
        response['Referrer-Policy'] = 'no-referrer'
        return response
    return None


def _render(request, template, context=None, status=200):
    response = render(request, template, context or {}, status=status)
    response['Cache-Control'] = 'private, no-store'
    # `origin` redacts paths (including one-time access tokens) while keeping
    # same-origin POST requests' Origin header valid for Django CSRF checks.
    response['Referrer-Policy'] = 'origin'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def _audit_redirect_error(request, error, form=None):
    if isinstance(error, ValidationError):
        text = '; '.join(error.messages)
    elif isinstance(error, IntegrityError):
        text = 'Não foi possível salvar: CNPJ, identificador ou vínculo duplicado.'
    else:
        text = 'Não foi possível concluir a operação.'
    messages.error(request, text)
    return form


@require_GET
def manifest(request):
    response = JsonResponse({
        'id': '/saas/', 'name': 'VERTYX Admin', 'short_name': 'VERTYX Admin',
        'description': 'Administração da plataforma VERTYX', 'lang': 'pt-BR',
        'start_url': '/saas/', 'scope': '/saas/', 'display': 'standalone',
        'background_color': '#f4f7fb', 'theme_color': '#10264b',
        'icons': [{'src': '/static/pwa/saas-icon.svg', 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any'}],
    }, content_type='application/manifest+json')
    response['Cache-Control'] = 'public, max-age=300'
    return response


@require_GET
def service_worker(request):
    response = HttpResponse(
        """'use strict';
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>{
  if(event.request.method!=='GET') return;
  event.respondWith(fetch(event.request,{cache:'no-store'}));
});
""", content_type='application/javascript; charset=utf-8')
    response['Service-Worker-Allowed'] = '/saas/'
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


PLATFORM_BOOTSTRAP_ADVISORY_LOCK = 0x5652545958424F4F


def _has_active_platform_admin():
    return AdministradorPlataforma.objects.filter(active=True, user__is_active=True).exists()


def _bootstrap_request_is_authorized(request):
    """Fail closed: local loopback in DEBUG, otherwise an operator-held secret."""
    configured_token = getattr(settings, 'PLATFORM_BOOTSTRAP_TOKEN', '')
    if configured_token:
        return True  # POST is checked separately so errors can be shown safely.
    if not settings.DEBUG:
        return False
    try:
        return ipaddress.ip_address(request.META.get('REMOTE_ADDR', '')).is_loopback
    except ValueError:
        return False


@require_http_methods(['GET', 'POST'])
def bootstrap_primeiro_administrador(request):
    """One-time, guarded enrollment for the first platform administrator."""
    if _has_active_platform_admin():
        return _render(request, 'platform_core/saas/first_platform_admin_closed.html', status=410)
    # A signed-in tenant account is never an authority to bootstrap the
    # platform, even if it happens to browse the setup URL on localhost.
    if request.user.is_authenticated:
        return HttpResponseForbidden('Saia da conta da organização antes da configuração inicial da plataforma.')
    if not _bootstrap_request_is_authorized(request):
        # Do not disclose whether the empty bootstrap is available remotely.
        return HttpResponse(status=404, headers={
            'Cache-Control': 'private, no-store',
            'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
        })

    form = PrimeiroAdministradorPlataformaForm(request.POST or None)
    if request.method == 'POST':
        token_configured = bool(getattr(settings, 'PLATFORM_BOOTSTRAP_TOKEN', ''))
        if token_configured and not hmac.compare_digest(
            request.POST.get('chave_bootstrap', ''), settings.PLATFORM_BOOTSTRAP_TOKEN,
        ):
            form.add_error('chave_bootstrap', 'Chave de ativação inválida.')
        if form.is_valid():
            try:
                with transaction.atomic():
                    # PostgreSQL transaction advisory lock serializes first-time
                    # enrollment even while the platform-admin table is empty.
                    if connection.vendor == 'postgresql':
                        with connection.cursor() as cursor:
                            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [PLATFORM_BOOTSTRAP_ADVISORY_LOCK])
                    if _has_active_platform_admin():
                        return _render(request, 'platform_core/saas/first_platform_admin_closed.html', status=410)
                    User = get_user_model()
                    name_parts = form.cleaned_data['nome'].strip().split()
                    user = User(
                        username=form.cleaned_data['username'],
                        email=form.cleaned_data['email'],
                        first_name=name_parts[0][:150] if name_parts else '',
                        last_name=' '.join(name_parts[1:])[:150],
                        is_active=True,
                        is_staff=False,
                        is_superuser=False,
                    )
                    user.set_password(form.cleaned_data['senha'])
                    user.save()
                    AdministradorPlataforma.objects.create(
                        user=user, created_by=user, active=True,
                    )
                    AuditoriaSaaS.objects.create(
                        actor=user,
                        action='BOOTSTRAP_PRIMEIRO_ADMINISTRADOR_PLATAFORMA',
                        record=f'administrador-plataforma:{user.pk}',
                        details={'user_id': user.pk},
                    )
            except IntegrityError:
                form.add_error(None, 'Não foi possível concluir o primeiro acesso. Recarregue a página e revise os dados.')
            else:
                return redirect('platform_core:saas_first_admin_complete')

    return _render(request, 'platform_core/saas/first_platform_admin_bootstrap.html', {
        'form': form,
        'requires_bootstrap_token': bool(getattr(settings, 'PLATFORM_BOOTSTRAP_TOKEN', '')),
    })


@require_GET
def primeiro_administrador_concluido(request):
    if _has_active_platform_admin():
        return _render(request, 'platform_core/saas/first_platform_admin_complete.html')
    return _render(request, 'platform_core/saas/first_platform_admin_closed.html', status=410)


@require_http_methods(['GET', 'POST'])
def primeiro_acesso(request, token):
    digest = PrimeiroAcessoSaaS.digest(token)
    row = PrimeiroAcessoSaaS.objects.select_related('user', 'organization').filter(token_digest=digest).first()
    valid = bool(row and row.usado_em is None and row.expira_em > timezone.now()
                 and not row.user.has_usable_password() and row.access_is_active())
    if not valid:
        return _render(request, 'platform_core/saas/first_access_expired.html', status=410)
    form = SetPasswordForm(row.user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            locked = PrimeiroAcessoSaaS.objects.select_for_update().select_related('user').get(pk=row.pk)
            if locked.usado_em or locked.expira_em <= timezone.now():
                return _render(request, 'platform_core/saas/first_access_expired.html', status=410)
            user = get_user_model().objects.select_for_update().get(pk=locked.user_id)
            # Recheck grants after locking: a revoked invitation must not set a password.
            list(OrganizationMembership.objects.select_for_update().filter(
                user=user, organization_id=locked.organization_id,
            ))
            locked.user = user
            if user.has_usable_password() or not locked.access_is_active():
                return _render(request, 'platform_core/saas/first_access_expired.html', status=410)
            locked_form = SetPasswordForm(user, request.POST)
            if not locked_form.is_valid():
                return _render(request, 'platform_core/saas/first_access.html',
                               {'form': locked_form, 'organization': locked.organization}, status=400)
            locked_form.save()
            locked.usado_em = timezone.now()
            locked.save(update_fields=['usado_em'])
            registrar(None, 'PRIMEIRO_ACESSO_CONCLUIDO', f'primeiro-acesso:{locked.pk}', organization=locked.organization,
                      details={'user_id': user.pk})
        pending=request.session.get('public_saas_pending_access')
        if pending and pending.get('token_id')==row.pk:
            request.session.pop('public_saas_pending_access',None)
        return _render(request, 'platform_core/saas/first_access_complete.html', {'email': row.user.email})
    return _render(request, 'platform_core/saas/first_access.html',
                   {'form': form, 'organization': row.organization})


@require_GET
def inicio(request):
    denied = _guard(request)
    if denied:
        return denied
    orgs = Organization.objects.all()
    subscriptions = AssinaturaSaaS.objects.select_related('organization', 'plano')
    near_limit = []
    mrr = 0
    for sub in subscriptions:
        if sub.status == AssinaturaSaaS.Status.ACTIVE:
            mrr += mrr_amount(sub)
        limit = sub.plano.limites.get('usuarios')
        if limit:
            used = sub.organization.memberships.filter(status='ATIVO').count()
            if used >= max(1, int(limit * .8)):
                near_limit.append({'organization': sub.organization, 'plan': sub.plano, 'used': used, 'limit': limit})
    counts = {
        'total': orgs.count(),
        'ativas': orgs.filter(status=Organization.Status.ACTIVE).filter(
            Q(assinatura_saas__isnull=True) | Q(assinatura_saas__status=AssinaturaSaaS.Status.ACTIVE)
        ).count(),
        'suspensas': orgs.filter(status=Organization.Status.SUSPENDED).count(),
        'implantacao': AssinaturaSaaS.objects.filter(status=AssinaturaSaaS.Status.IMPLANTACAO).count(),
        'planos_ativos': PlanoSaaS.objects.filter(ativo=True).count(),
        'planos_inativos': PlanoSaaS.objects.filter(ativo=False).count(),
    }
    by_plan = PlanoSaaS.objects.annotate(clientes=Count('assinaturas')).order_by('nome')
    usage_rows = []
    sub_map = {sub.organization_id: sub for sub in subscriptions}
    for org in orgs[:100]:
        sub = sub_map.get(org.id)
        usage_rows.append({'organization': org, 'plan': sub.plano if sub else None,
                           'metrics': metricas_uso(org)})
    return _render(request, 'platform_core/saas/dashboard.html', {
        'counts': counts, 'mrr': mrr, 'by_plan': by_plan,
        'near_limit': near_limit[:8], 'financeiro_saas_preparado': True,
        'usage_rows': usage_rows,
    })


@require_http_methods(['GET', 'POST'])
def bootstrap_gestao_publica(request):
    denied = _guard_bootstrap(request)
    if denied:
        return denied

    organization_id = request.POST.get('organization') if request.method == 'POST' else request.GET.get('organization')
    form = BootstrapPrefeituraForm(
        request.POST or None,
        organization_id=organization_id,
        initial={'organization': organization_id} if request.method == 'GET' and organization_id else None,
    )
    organization = form.organization
    readiness = None
    if organization:
        erp = ERPCatalog.objects.filter(code='gestao-publica', active=True).first()
        product_enabled = bool(erp and OrganizationERP.objects.filter(
            organization=organization, erp=erp, enabled=True,
        ).exists())
        from .modules import module_enabled
        with tenant_context(organization):
            contract_enabled = module_enabled('gestao_publica')
        enabled_memberships = MembershipERP.objects.filter(
            membership__organization=organization,
            membership__status=OrganizationMembership.Status.ACTIVE,
            membership__user__is_active=True,
            erp__code='gestao-publica', erp__active=True, enabled=True,
        ).count()
        readiness = {
            'product_enabled': product_enabled,
            'contract_enabled': contract_enabled,
            'enabled_memberships': enabled_memberships,
            'ready': product_enabled and contract_enabled,
        }

    if request.method == 'POST' and form.is_valid():
        try:
            access = bootstrap_administrador_prefeitura(
                actor=request.user,
                organization_id=form.cleaned_data['organization'].pk,
                membership_id=form.cleaned_data['membership'].pk,
            )
        except (ValidationError, PermissionDenied) as exc:
            if isinstance(exc, PermissionDenied):
                return HttpResponseForbidden(str(exc))
            messages.error(request, '; '.join(exc.messages))
        else:
            messages.success(request, f"Primeiro acesso concedido para {access.membership.user.get_full_name() or access.membership.user.email} com escopo Prefeitura.")
            return redirect(f"{reverse('platform_core:saas_municipal_bootstrap')}?organization={access.membership.organization_id}")

    organizations = Organization.objects.filter(status=Organization.Status.ACTIVE).order_by('razao_social', 'pk')
    existing_accesses = []
    if organization:
        from rh_municipal.models import AcessoMunicipal
        existing_accesses = list(AcessoMunicipal.all_objects.filter(
            membership__organization=organization,
        ).select_related('membership__user').order_by('membership__user__email')[:25])
    return _render(request, 'platform_core/saas/municipal_bootstrap.html', {
        'form': form, 'organization': organization, 'readiness': readiness,
        'organizations': organizations, 'existing_accesses': existing_accesses,
    }, status=400 if request.method == 'POST' and form.errors else 200)


@require_http_methods(['GET', 'POST'])
def nova_prefeitura_homologacao(request):
    denied = _guard_bootstrap(request)
    if denied:
        return denied
    form = NovaPrefeituraHomologacaoForm(request.POST or None)
    created = None
    pending = None
    public_id = request.GET.get('criada', '')
    if public_id:
        try:
            organization = Organization.objects.get(public_id=public_id)
        except (Organization.DoesNotExist, ValueError):
            organization = None
        if organization:
            candidate = request.session.get('saas_pending_access', {}).get(str(organization.public_id))
            if candidate and timezone.now().timestamp() - candidate.get('created_at', 0) < 1200:
                token_row = PrimeiroAcessoSaaS.objects.filter(
                    pk=candidate.get('token_id'), organization=organization,
                    user_id=candidate.get('user_id'), usado_em__isnull=True,
                    expira_em__gt=timezone.now(),
                ).select_related('user').first()
                if token_row:
                    created = organization
                    pending = {
                        'user': token_row.user,
                        'expires': token_row.expira_em,
                        'url': request.build_absolute_uri(reverse(
                            'platform_core:saas_first_access', args=[candidate['token']],
                        )),
                    }
    if request.method == 'POST' and form.is_valid():
        try:
            result = criar_prefeitura_homologacao(
                actor=request.user,
                razao_social=form.cleaned_data['razao_social'],
                nome_fantasia=form.cleaned_data['nome_fantasia'],
                administrador_nome=form.cleaned_data['administrador_nome'],
                administrador_email=form.cleaned_data['administrador_email'],
            )
        except (ValidationError, IntegrityError) as exc:
            _audit_redirect_error(request, exc, form)
        else:
            access_by_org = request.session.get('saas_pending_access', {})
            access_by_org[str(result['organization'].public_id)] = {
                'token': result['token'], 'token_id': result['token_id'],
                'created_at': timezone.now().timestamp(), 'user_id': result['user'].pk,
            }
            request.session['saas_pending_access'] = access_by_org
            messages.success(request, 'Prefeitura criada com acesso municipal explícito. Nenhuma senha foi definida.')
            return redirect(f"{reverse('platform_core:saas_municipal_new')}?criada={result['organization'].public_id}")
    return _render(request, 'platform_core/saas/municipal_new.html', {
        'form': form, 'created': created, 'pending_access': pending,
    }, status=400 if request.method == 'POST' and form.errors else 200)


@require_GET
def clientes(request):
    denied = _guard(request)
    if denied:
        return denied
    qs = Organization.objects.select_related('assinatura_saas__plano').prefetch_related('memberships__user').order_by('-created_at')
    term = request.GET.get('q', '').strip()
    if term:
        qs = qs.filter(Q(razao_social__icontains=term) | Q(nome_fantasia__icontains=term) | Q(cnpj__icontains=term) | Q(email__icontains=term))
    rows = []
    for organization in qs[:300]:
        admins = [m.user for m in organization.memberships.all() if m.status == 'ATIVO' and m.role == 'ADMIN']
        rows.append({'organization': organization, 'subscription': getattr(organization, 'assinatura_saas', None),
                     'admin': admins[0] if admins else None})
    return _render(request, 'platform_core/saas/clients.html', {'rows': rows, 'q': term})


@require_http_methods(['GET', 'POST'])
def novo_cliente(request, step=1):
    denied = _guard(request)
    if denied:
        return denied
    if step == 1:
        form = DadosEmpresaForm(request.POST or None)
        if request.method == 'POST' and form.is_valid():
            request.session['saas_company_draft'] = form.cleaned_data
            return redirect('platform_core:saas_new_client_step2')
        return _render(request, 'platform_core/saas/new_client_company.html', {'form': form, 'step': 1}, status=400 if request.method == 'POST' else 200)
    draft = request.session.get('saas_company_draft')
    if not isinstance(draft, dict):
        messages.info(request, 'Comece pelas informações da empresa.')
        return redirect('platform_core:saas_new_client')
    form = AdminInicialForm(request.POST or None, prefix='admin')
    if request.method == 'POST':
        plan_id = request.POST.get('plano')
        try:
            plan = PlanoSaaS.objects.get(pk=plan_id, ativo=True)
        except (PlanoSaaS.DoesNotExist, ValueError, TypeError):
            plan = None
        if plan is None:
            form.add_error(None, 'Selecione um plano ativo.')
        if form.is_valid() and plan:
            try:
                result = criar_cliente(actor=request.user, company=draft, admin=form.cleaned_data, plan_id=plan.pk)
            except (ValidationError, IntegrityError) as exc:
                _audit_redirect_error(request, exc)
            else:
                pending = request.session.get('saas_pending_access', {})
                pending[str(result['organization'].public_id)] = {
                    'token': result['token'], 'token_id': result['token_id'],
                    'created_at': timezone.now().timestamp(), 'user_id': result['user'].pk,
                }
                request.session['saas_pending_access'] = pending
                request.session.pop('saas_company_draft', None)
                messages.success(request, 'Cliente criado. Compartilhe o link de primeiro acesso com o administrador inicial.')
                return redirect('platform_core:saas_client_detail', public_id=result['organization'].public_id)
    plans = PlanoSaaS.objects.filter(ativo=True).order_by('nome')
    return _render(request, 'platform_core/saas/new_client_plan.html', {
        'form': form, 'plans': plans, 'draft': draft, 'step': 2,
        'module_labels': dict(ModuloOrganizacao._meta.get_field('codigo').choices),
    }, status=400 if request.method == 'POST' else 200)


@require_GET
def cliente_detalhe(request, public_id):
    denied = _guard(request)
    if denied:
        return denied
    organization = get_object_or_404(Organization.objects.prefetch_related('memberships__user'), public_id=public_id)
    subscription = AssinaturaSaaS.objects.filter(organization=organization).select_related('plano').first()
    admins = [m.user for m in organization.memberships.all() if m.status == 'ATIVO' and m.role == 'ADMIN']
    modules = ModuloOrganizacao.objects.filter(organization=organization).order_by('codigo')
    audits = AuditoriaSaaS.objects.filter(organization=organization)[:20]
    success = None
    pending = request.session.get('saas_pending_access', {}).get(str(organization.public_id))
    if pending and timezone.now().timestamp() - pending.get('created_at', 0) < 1200:
        token_row = PrimeiroAcessoSaaS.objects.filter(pk=pending.get('token_id'), organization=organization,
                     user_id=pending.get('user_id'), usado_em__isnull=True, expira_em__gt=timezone.now()).first()
        if token_row:
            token_url = request.build_absolute_uri(reverse('platform_core:saas_first_access', args=[pending['token']]))
            admin = token_row.user
            text = (f"Olá, {admin.get_full_name() if admin else 'administrador'}. A empresa {organization.display_name} foi cadastrada no VERTYX. "
                    f"Acesse {token_url} com o usuário {admin.email if admin else ''} e defina sua própria senha. "
                    "O link é individual, de uso único e expira em 48 horas. Nenhuma senha foi criada ou enviada.")
            success = {'token_id': token_row.pk, 'url': token_url, 'message': text,
                       'expires': token_row.expira_em, 'issued': token_row.criado_em,
                       'reissued': bool(pending.get('reissued')),
                       'invalidated_id': pending.get('invalidated_token_id'),
                       'share_url': f"https://api.whatsapp.com/send?text={quote(text)}"}
        else:
            request.session.get('saas_pending_access', {}).pop(str(organization.public_id), None)
            request.session.modified = True
    initial_access = PrimeiroAcessoSaaS.objects.filter(
        organization=organization, usado_em__isnull=True,
    ).select_related('user').order_by('-criado_em').first()
    can_reissue = bool(initial_access and initial_access.user.is_active
                       and not initial_access.user.has_usable_password()
                       and initial_access.user.memberships.filter(
                           organization=organization, role='ADMIN', status='ATIVO').exists())
    quota_usage = metricas_uso(organization)
    product_quota_usage = metricas_uso_por_produto(organization)
    module_catalog = client_module_catalog_context(organization)
    return _render(request, 'platform_core/saas/client_detail.html', {
        'organization': organization, 'subscription': subscription,
        'admin': admins[0] if admins else None, 'modules': modules,
        'module_catalog': module_catalog,
        'audits': audits, 'created_access': success, 'can_reissue_access': can_reissue,
        'quota_usage': quota_usage,
        'product_quota_usage': product_quota_usage,
    })


@require_POST
def reemitir_acesso(request, public_id):
    denied = _guard(request)
    if denied:
        return denied
    organization = get_object_or_404(Organization, public_id=public_id)
    try:
        result = reemitir_primeiro_acesso(actor=request.user, organization=organization)
    except ValidationError as exc:
        _audit_redirect_error(request, exc)
        return redirect('platform_core:saas_client_detail', public_id=public_id)
    pending = request.session.get('saas_pending_access', {})
    pending[str(organization.public_id)] = {
        'token': result['token'], 'token_id': result['token_id'],
        'created_at': timezone.now().timestamp(), 'user_id': result['user'].pk,
        'reissued': True, 'invalidated_token_id': result['invalidated_token_id'],
    }
    request.session['saas_pending_access'] = pending
    request.session.modified = True
    messages.success(request, f"Novo acesso emitido em {timezone.localtime(result['created_at']):%d/%m/%Y %H:%M}. O link anterior foi invalidado.")
    return redirect('platform_core:saas_client_detail', public_id=public_id)


@require_POST
def compartilhar_acesso(request, public_id):
    denied = _guard(request)
    if denied:
        return denied
    organization = get_object_or_404(Organization, public_id=public_id)
    pending = request.session.get('saas_pending_access', {}).get(str(organization.public_id))
    if not pending or timezone.now().timestamp() - pending.get('created_at', 0) >= 1200:
        messages.error(request, 'O compartilhamento expirou. Gere um novo link de primeiro acesso.')
        return redirect('platform_core:saas_client_detail', public_id=public_id)
    token = PrimeiroAcessoSaaS.objects.filter(pk=pending.get('token_id'), organization=organization,
             user_id=pending.get('user_id'), usado_em__isnull=True, expira_em__gt=timezone.now()).first()
    if not token:
        messages.error(request, 'O link de primeiro acesso já foi usado ou expirou.')
        return redirect('platform_core:saas_client_detail', public_id=public_id)
    admin = token.user
    link = request.build_absolute_uri(reverse('platform_core:saas_first_access', args=[pending['token']]))
    message = (f"Olá, {admin.get_full_name() or admin.email}. A empresa {organization.display_name} foi cadastrada no VERTYX. "
               f"Acesse {link} com o usuário {admin.email} e defina sua própria senha. "
               "O link é individual, de uso único e expira em 48 horas. Nenhuma senha foi criada ou enviada.")
    registrar(request.user, 'LINK_WHATSAPP_ABERTO', f'primeiro-acesso:{token.pk}', organization=organization,
              details={'user_id': admin.pk, 'canal': 'compartilhamento manual', 'confirmado_envio': False})
    # The raw one-use token is removed from the server-side session after the
    # hand-off; the browser receives only the WhatsApp compose URL.
    pending_access = request.session.get('saas_pending_access', {})
    pending_access.pop(str(organization.public_id), None)
    request.session['saas_pending_access'] = pending_access
    request.session.modified = True
    response = redirect(f"https://api.whatsapp.com/send?text={quote(message)}")
    response['Referrer-Policy'] = 'no-referrer'
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET', 'POST'])
def editar_cliente(request, public_id):
    denied = _guard(request)
    if denied:
        return denied
    organization = get_object_or_404(Organization, public_id=public_id)
    form = EditarClienteForm(request.POST or None, instance=organization)
    if request.method == 'POST' and form.is_valid():
        try:
            atualizar_cliente(actor=request.user, organization=organization, values=form.cleaned_data)
        except (ValidationError, IntegrityError) as exc:
            _audit_redirect_error(request, exc, form)
        else:
            messages.success(request, 'Cadastro atualizado; o histórico do cliente foi preservado.')
            return redirect('platform_core:saas_client_detail', public_id=organization.public_id)
    return _render(request, 'platform_core/saas/edit_client.html', {
        'form': form,
        'organization': organization,
        'subscription': AssinaturaSaaS.objects.filter(organization=organization).select_related('plano').first(),
        'module_catalog': client_module_catalog_context(organization),
        'title': 'Editar cliente',
        'back': reverse('platform_core:saas_client_detail', args=[organization.public_id]),
    }, status=400 if request.method == 'POST' else 200)


@require_POST
def modulos_cliente(request, public_id):
    denied = _guard(request)
    if denied:
        return denied
    organization = get_object_or_404(Organization, public_id=public_id)
    target = (
        reverse('platform_core:saas_client_edit', args=[organization.public_id])
        if request.POST.get('next') == 'edit'
        else reverse('platform_core:saas_client_detail', args=[organization.public_id])
    )
    if request.POST.get('acao') == 'restaurar':
        try:
            restaurar_modulos_cliente(
                actor=request.user,
                organization=organization,
                reason=request.POST.get('motivo', ''),
            )
        except (ValidationError, IntegrityError) as exc:
            _audit_redirect_error(request, exc)
        else:
            messages.success(request, 'Módulos restaurados para o padrão do plano/contrato do cliente.')
        return redirect(target)

    form = EditarModulosClienteForm(request.POST)
    if not form.is_valid():
        return HttpResponse('Informe módulos ou produtos ERP válidos antes de salvar.', status=400)
    selected_erp_codes = (
        form.cleaned_data.get('erps', [])
        if request.POST.get('sync_erps') == '1'
        else None
    )
    try:
        summary = atualizar_modulos_cliente(
            actor=request.user,
            organization=organization,
            selected_modules=form.cleaned_data.get('modulos', []),
            reason=form.cleaned_data.get('motivo', ''),
            selected_erp_codes=selected_erp_codes,
        )
    except (ValidationError, IntegrityError) as exc:
        _audit_redirect_error(request, exc)
    else:
        messages.success(
            request,
            (
                f"Módulos do cliente salvos: {len(summary['enabled'])} ativo(s)"
                f" ({len(summary['added_manual'])} liberado(s) manualmente, "
                f"{len(summary['removed_manual'])} removido(s) manualmente)."
            ),
        )
    return redirect(target)


@require_http_methods(['GET', 'POST'])
def alterar_plano_cliente(request, public_id):
    denied = _guard(request)
    if denied:
        return denied
    organization = get_object_or_404(Organization, public_id=public_id)
    subscription = get_object_or_404(AssinaturaSaaS.objects.select_related('plano'), organization=organization)
    form = AlterarPlanoForm(request.POST or None)
    form.fields['plano'].queryset = form.fields['plano'].queryset.exclude(pk=subscription.plano_id)
    before = set(subscription.plano.modulos)
    if request.method == 'POST' and form.is_valid():
        chosen = form.cleaned_data['plano']
        current_usage = metricas_uso(organization)
        quota_warnings = []
        quota_unavailable = []
        labels = {'usuarios': 'usuários', 'filiais': 'filiais', 'vendas_mes': 'vendas no mês',
                  'ordens_mes': 'OS no mês', 'armazenamento_mb': 'armazenamento'}
        for key, metric in current_usage.items():
            new_limit = chosen.limites.get(key)
            if new_limit is not None and not metric['available']:
                quota_unavailable.append(f"{labels[key]}: medição não disponível; a operação será bloqueada se não houver como confirmar a capacidade do plano.")
            elif new_limit is not None:
                used_value = metric['used'] / (1024 * 1024) if key == 'armazenamento_mb' else metric['used']
                if used_value > new_limit:
                    quota_warnings.append(f"{labels[key]}: utilização atual acima do novo limite ({used_value:.2f} / {new_limit} MB)." if key == 'armazenamento_mb'
                                          else f"{labels[key]}: utilização atual acima do novo limite ({used_value} / {new_limit}).")
        if (quota_warnings or quota_unavailable) and request.POST.get('confirmar_limites') != 'sim':
            return _render(request, 'platform_core/saas/change_plan.html', {
                'organization': organization, 'subscription': subscription, 'form': form,
                'added': sorted(set(chosen.modulos) - before), 'removed': sorted(before - set(chosen.modulos)),
                'module_labels': dict(ModuloOrganizacao._meta.get_field('codigo').choices),
                'current_plan': plano_snapshot(subscription.plano),
                'plans_data': [plano_snapshot(plan) for plan in PlanoSaaS.objects.filter(ativo=True).exclude(pk=subscription.plano_id)],
                'new_limits': chosen.limites, 'quota_warnings': quota_warnings,
                'quota_unavailable': quota_unavailable,
                'confirm_quota_overage': True,
            }, status=200)
        try:
            changed = alterar_plano(actor=request.user, organization=organization,
                                    plan_id=form.cleaned_data['plano'].pk, reason=form.cleaned_data['motivo'])
        except (ValidationError, IntegrityError) as exc:
            _audit_redirect_error(request, exc, form)
        else:
            messages.success(request, 'Plano alterado. Módulos e limites foram sincronizados sem mudar permissões individuais.')
            return redirect('platform_core:saas_client_detail', public_id=changed.organization.public_id)
    selected = form.cleaned_data.get('plano') if form.is_bound and not form.errors else None
    after = set(selected.modulos) if selected else set()
    return _render(request, 'platform_core/saas/change_plan.html', {
        'organization': organization, 'subscription': subscription, 'form': form,
        'added': sorted(after - before), 'removed': sorted(before - after),
        'module_labels': dict(ModuloOrganizacao._meta.get_field('codigo').choices),
        'current_plan': plano_snapshot(subscription.plano),
        'plans_data': [plano_snapshot(plan) for plan in PlanoSaaS.objects.filter(ativo=True).exclude(pk=subscription.plano_id)],
        'new_limits': selected.limites if selected else {},
        'quota_warnings': [],
        'quota_unavailable': [],
    }, status=400 if request.method == 'POST' and form.errors else 200)


@require_POST
def status_cliente(request, public_id, action):
    denied = _guard(request)
    if denied:
        return denied
    if action not in ('ativar', 'suspender', 'reativar'):
        return HttpResponseForbidden('Ação inválida.')
    organization = get_object_or_404(Organization, public_id=public_id)
    if request.POST.get('confirmar') != 'sim':
        messages.error(request, 'Confirme a operação para continuar.')
        return redirect('platform_core:saas_client_detail', public_id=public_id)
    try:
        locked = alterar_status_cliente(actor=request.user, organization=organization, activate=action in ('ativar', 'reativar'))
    except (ValidationError, IntegrityError) as exc:
        _audit_redirect_error(request, exc)
    else:
        messages.success(request, 'Acesso suspenso; dados e usuários foram preservados.' if action == 'suspender' else 'Assinatura ativada; acesso liberado e dados preservados.')
    return redirect('platform_core:saas_client_detail', public_id=public_id)


@require_GET
def planos(request):
    denied = _guard(request)
    if denied:
        return denied
    rows = PlanoSaaS.objects.select_related('plano_comercial', 'erp').prefetch_related('modulos_produto__modulo__erp').annotate(clientes=Count('assinaturas')).order_by('nome')
    return _render(request, 'platform_core/saas/plans.html', {'plans': rows, 'module_labels': dict(ModuloOrganizacao._meta.get_field('codigo').choices)})


@require_GET
def catalogo_produtos(request):
    denied = _guard(request)
    if denied:
        return denied
    products = ERPCatalog.objects.prefetch_related('modules__resources').order_by('name', 'pk')
    return _render(request, 'platform_core/saas/catalog.html', {'products': products})


@require_http_methods(['GET', 'POST'])
def produto_form(request, pk=None):
    denied = _guard(request)
    if denied:
        return denied
    instance = get_object_or_404(ERPCatalog, pk=pk) if pk else ERPCatalog()
    form = ERPCatalogForm(request.POST or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        creating = instance.pk is None
        try:
            product = form.save()
            registrar(request.user, 'ERP_CATALOGO_CRIADO' if creating else 'ERP_CATALOGO_EDITADO',
                      f'erp:{product.pk}', details={'codigo': product.code, 'campos': form.changed_data})
        except (ValidationError, IntegrityError) as exc:
            _audit_redirect_error(request, exc, form)
        else:
            messages.success(request, 'Produto do catálogo salvo. Vínculos com organizações não foram alterados.')
            return redirect('platform_core:saas_catalog')
    return _render(request, 'platform_core/saas/form.html', {
        'form': form, 'title': 'Novo produto ERP' if not pk else 'Editar produto ERP',
        'back': reverse('platform_core:saas_catalog'),
    }, status=400 if request.method == 'POST' and form.errors else 200)


@require_http_methods(['GET', 'POST'])
def modulo_produto_form(request, product_pk=None, pk=None):
    denied = _guard(request)
    if denied:
        return denied
    module = get_object_or_404(ERPProductModule.objects.select_related('erp'), pk=pk) if pk else None
    product = get_object_or_404(ERPCatalog, pk=product_pk) if product_pk else (module.erp if module else None)
    form = ERPProductModuleForm(request.POST or None, instance=module, product=product)
    if request.method == 'POST' and form.is_valid():
        try:
            saved = form.save()
            registrar(request.user, 'ERP_MODULO_CATALOGO_SALVO', f'modulo:{saved.pk}',
                      details={'produto': saved.erp.code, 'codigo': saved.code})
        except (ValidationError, IntegrityError) as exc:
            _audit_redirect_error(request, exc, form)
        else:
            messages.success(request, 'Módulo do produto salvo.')
            return redirect('platform_core:saas_catalog')
    return _render(request, 'platform_core/saas/form.html', {
        'form': form, 'title': 'Novo módulo do produto' if module is None else 'Editar módulo do produto',
        'back': reverse('platform_core:saas_catalog'),
    }, status=400 if request.method == 'POST' and form.errors else 200)


@require_http_methods(['GET', 'POST'])
def recurso_produto_form(request, module_pk=None, pk=None):
    denied = _guard(request)
    if denied:
        return denied
    resource = get_object_or_404(ERPProductResource.objects.select_related('module__erp'), pk=pk) if pk else None
    module = get_object_or_404(ERPProductModule.objects.select_related('erp'), pk=module_pk) if module_pk else (resource.module if resource else None)
    form = ERPProductResourceForm(request.POST or None, instance=resource, product=module.erp if module else None, module=module)
    if request.method == 'POST' and form.is_valid():
        try:
            saved = form.save()
            registrar(request.user, 'ERP_RECURSO_CATALOGO_SALVO', f'recurso:{saved.pk}',
                      details={'produto': saved.module.erp.code, 'modulo': saved.module.code, 'codigo': saved.code})
        except (ValidationError, IntegrityError) as exc:
            _audit_redirect_error(request, exc, form)
        else:
            messages.success(request, 'Recurso do módulo salvo.')
            return redirect('platform_core:saas_catalog')
    return _render(request, 'platform_core/saas/form.html', {
        'form': form, 'title': 'Novo recurso' if resource is None else 'Editar recurso',
        'back': reverse('platform_core:saas_catalog'),
    }, status=400 if request.method == 'POST' and form.errors else 200)


@require_http_methods(['GET', 'POST'])
def contratos(request):
    denied = _guard(request)
    if denied:
        return denied
    form = ContratoSaaSForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            contract = form.save(actor=request.user)
            registrar(request.user, 'CONTRATO_SAAS_RASCUNHO_CRIADO', f'contrato:{contract.pk}',
                      organization=contract.organization,
                      details={'itens': list(contract.itens.values_list('erp__code', 'plano__slug'))})
        except (ValidationError, IntegrityError) as exc:
            _audit_redirect_error(request, exc, form)
        else:
            messages.success(request, 'Rascunho comercial criado. Ele não habilita produtos nem acessos até a ativação autorizada.')
            return redirect('platform_core:saas_contracts')
    rows = ContratoSaaS.objects.select_related('organization', 'criado_por').prefetch_related('itens__erp', 'itens__plano').order_by('-criado_em', '-pk')
    return _render(request, 'platform_core/saas/contracts.html', {
        'form': form, 'contracts': rows,
    }, status=400 if request.method == 'POST' and form.errors else 200)


@require_POST
def contrato_status(request, pk, action):
    denied = _guard(request)
    if denied:
        return denied
    if request.POST.get('confirmar') != 'sim':
        messages.error(request, 'Confirme a operação de contrato para continuar.')
        return redirect('platform_core:saas_contracts')
    try:
        contract = alterar_status_contrato(actor=request.user, contract_id=pk, action=action)
    except (ValidationError, IntegrityError) as exc:
        messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else 'O contrato mudou em outra operação; recarregue a tela.')
    else:
        label = 'ativado' if action == 'ativar' else 'suspenso'
        messages.success(request, f'Contrato #{contract.pk} {label}. Nenhuma permissão individual foi concedida ou removida.')
    return redirect('platform_core:saas_contracts')


@require_http_methods(['GET', 'POST'])
def plano_form(request, pk=None):
    denied = _guard(request)
    if denied:
        return denied
    instance = get_object_or_404(PlanoSaaS, pk=pk) if pk else PlanoSaaS()
    linked = (instance.assinaturas.count() + instance.itens_contrato_saas.filter(ativo=True).count()) if instance.pk else 0
    creating = instance.pk is None
    form = PlanoSaaSForm(request.POST or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        try:
            candidate = form.save(commit=False)
            if linked and instance.pk:
                protected = ('erp_id', 'preco', 'periodicidade', 'modulos', 'limites', 'plano_comercial', 'nivel_indicadores')
                old_product_modules = sorted(instance.modulos_produto.values_list('modulo_id', flat=True))
                new_product_modules = sorted(form.cleaned_data.get('modulos_produto').values_list('pk', flat=True))
                if any(getattr(candidate, key) != getattr(instance, key) for key in protected) or old_product_modules != new_product_modules:
                    raise ValidationError('Este plano já possui clientes. Para preservar os contratos atuais, crie uma nova versão do plano.')
            before = plano_snapshot(instance) if instance.pk else {}
            candidate.save()
            form.save_product_modules(candidate)
            registrar(request.user, 'PLANO_CRIADO' if creating else 'PLANO_EDITADO', f'plano:{candidate.pk}',
                      details={'anterior': before, 'posterior': plano_snapshot(candidate)})
        except (ValidationError, IntegrityError) as exc:
            _audit_redirect_error(request, exc, form)
        else:
            messages.success(request, 'Plano salvo. A contratação pública só fica ativa quando o vínculo e o preço são compatíveis.')
            return redirect('platform_core:saas_plans')
    return _render(request, 'platform_core/saas/form.html', {
        'form': form, 'title': 'Novo plano' if not pk else 'Editar plano',
        'back': reverse('platform_core:saas_plans'), 'linked': linked,
        'module_form': True,
    }, status=400 if request.method == 'POST' and form.errors else 200)
