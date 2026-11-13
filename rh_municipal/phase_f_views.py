"""Phase F municipal tax and territorial screens, gated by product/module/permission/scope."""
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.decorators.http import require_GET

from platform_core.context import current_organization
from . import cidadao_tributario_services as ct_svc
from . import phase_f_models as m
from . import phase_f_services as svc
from .phase_f_forms import (
    ContributorForm, InstallmentForm, ManualPaymentForm, NfseDraftForm,
    PropertyForm, TaxLaunchForm, TaxRuleForm,
)
from .phase_e_services import public_module_available
from .public_services import has_municipal_permission, require_municipal_permission, scoped_queryset


def _allowed(user, capability, module):
    org = current_organization()
    return bool(public_module_available(org, module) and has_municipal_permission(user, capability, module=module))


def _render(request, *, title, subtitle, module_code, **extra):
    from .public_views import _context
    flags = {
        'can_view_contributors': _allowed(request.user, 'contribuinte.view', m.PHASE_F_MODULES['tributos']) or _allowed(request.user, 'contribuinte.manage', m.PHASE_F_MODULES['tributos']),
        'can_manage_contributors': _allowed(request.user, 'contribuinte.manage', m.PHASE_F_MODULES['tributos']),
        'can_view_properties': _allowed(request.user, 'imovel.view', m.PHASE_F_MODULES['territorio']) or _allowed(request.user, 'imovel.manage', m.PHASE_F_MODULES['territorio']),
        'can_manage_properties': _allowed(request.user, 'imovel.manage', m.PHASE_F_MODULES['territorio']),
        'can_view_tax': _allowed(request.user, 'tributo.view', m.PHASE_F_MODULES['tributos']) or _allowed(request.user, 'tributo.manage', m.PHASE_F_MODULES['tributos']),
        'can_manage_tax': _allowed(request.user, 'tributo.manage', m.PHASE_F_MODULES['tributos']),
        'can_view_collections': _allowed(request.user, 'arrecadacao.view', m.PHASE_F_MODULES['tributos']) or _allowed(request.user, 'arrecadacao.manage', m.PHASE_F_MODULES['tributos']),
        'can_manage_collections': _allowed(request.user, 'arrecadacao.manage', m.PHASE_F_MODULES['tributos']),
        'can_manage_debt': _allowed(request.user, 'divida.manage', m.PHASE_F_MODULES['tributos']),
        'can_view_debt': _allowed(request.user, 'divida.view', m.PHASE_F_MODULES['tributos']) or _allowed(request.user, 'divida.manage', m.PHASE_F_MODULES['tributos']),
        'can_view_nfse': _allowed(request.user, 'nfse.view', m.PHASE_F_MODULES['nfse']) or _allowed(request.user, 'nfse.manage', m.PHASE_F_MODULES['nfse']),
        'can_manage_nfse': _allowed(request.user, 'nfse.manage', m.PHASE_F_MODULES['nfse']),
    }
    context = _context(request, **flags, **extra)
    context.update({'title': title, 'subtitle': subtitle, 'module_code': module_code})
    return render(request, 'rh_municipal/phase_f_module.html', context)


def _execute(request, callback, success):
    try:
        callback()
    except (PermissionDenied, ValidationError, IntegrityError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            detail = '; '.join(exc.messages)
        elif isinstance(exc, PermissionDenied):
            detail = str(exc)
        elif isinstance(exc, IntegrityError):
            detail = 'O registro conflitou com outro existente; nenhuma alteração foi concluída.'
        else:
            detail = 'Revise os dados informados.'
        messages.error(request, detail)
        return False
    messages.success(request, success)
    return True


def _require_any(request, pairs):
    if not any(_allowed(request.user, capability, module) for capability, module in pairs):
        raise PermissionDenied('Este módulo não está contratado ou seu acesso não foi autorizado.')


@login_required
@require_http_methods(['GET', 'POST'])
def tributary_home(request):
    org = current_organization()
    pairs = [
        ('contribuinte.view', m.PHASE_F_MODULES['tributos']), ('contribuinte.manage', m.PHASE_F_MODULES['tributos']),
        ('tributo.view', m.PHASE_F_MODULES['tributos']), ('tributo.manage', m.PHASE_F_MODULES['tributos']),
        ('arrecadacao.view', m.PHASE_F_MODULES['tributos']), ('arrecadacao.manage', m.PHASE_F_MODULES['tributos']),
        ('divida.view', m.PHASE_F_MODULES['tributos']), ('divida.manage', m.PHASE_F_MODULES['tributos']),
    ]
    _require_any(request, pairs)
    initial = {'request_id': uuid.uuid4()}
    context = {
        'contributors': scoped_queryset(request.user, m.ContribuinteMunicipal.objects.filter(organization=org)).order_by('nome')[:50],
        'properties': scoped_queryset(request.user, m.ImovelMunicipal.objects.filter(organization=org)).select_related('proprietario').order_by('inscricao')[:50],
        'launches': scoped_queryset(request.user, m.LancamentoTributarioMunicipal.objects.filter(organization=org)).select_related('contribuinte', 'regra', 'imovel').order_by('-pk')[:50],
        'installments': scoped_queryset(request.user, m.ParcelaTributariaMunicipal.objects.filter(organization=org)).select_related('lancamento', 'lancamento__contribuinte').order_by('-pk')[:50],
        'payments': scoped_queryset(request.user, m.PagamentoArrecadacaoMunicipal.objects.filter(organization=org)).select_related('parcela').order_by('-pk')[:30],
        'debts': scoped_queryset(request.user, m.InscricaoDividaAtivaMunicipal.objects.filter(organization=org)).select_related('contribuinte', 'lancamento').order_by('-pk')[:30],
        'dams': scoped_queryset(request.user, m.GuiaArrecadacaoDAM.objects.filter(organization=org)).select_related('contribuinte', 'lancamento').order_by('-emitida_em')[:40],
        'certificates': scoped_queryset(request.user, m.CertidaoTributariaMunicipal.objects.filter(organization=org)).select_related('contribuinte', 'imovel').order_by('-emitida_em')[:30],
        'itbis': scoped_queryset(request.user, m.SolicitacaoITBIMunicipal.objects.filter(organization=org)).select_related('imovel', 'transmitente', 'adquirente', 'lancamento').order_by('-criado_em')[:30],
        'itbi_rules': scoped_queryset(request.user, m.RegraTributariaMunicipal.objects.filter(organization=org, tributo=m.RegraTributariaMunicipal.Tributo.ITBI, ativa=True)).order_by('-versao'),
        'rules': scoped_queryset(request.user, m.RegraTributariaMunicipal.objects.filter(organization=org)).order_by('tributo', '-versao')[:40],
        'contributor_form': ContributorForm(organization=org, actor=request.user),
        'rule_form': TaxRuleForm(organization=org, actor=request.user),
        'launch_form': TaxLaunchForm(organization=org, actor=request.user),
        'installment_form': InstallmentForm(organization=org, actor=request.user),
        'payment_form': ManualPaymentForm(organization=org, actor=request.user, initial=initial),
    }
    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'contributor':
            require_municipal_permission(request.user, 'contribuinte.manage', module=m.PHASE_F_MODULES['tributos'])
            form = ContributorForm(request.POST, organization=org, actor=request.user)
            context['contributor_form'] = form
            if form.is_valid() and _execute(request, lambda: svc.create_contributor(request.user, data=form.cleaned_data), 'Contribuinte cadastrado.'):
                return redirect('rh_municipal_public:tributary_home')
        elif action == 'rule':
            require_municipal_permission(request.user, 'tributo.manage', module=m.PHASE_F_MODULES['tributos'])
            form = TaxRuleForm(request.POST, organization=org, actor=request.user)
            context['rule_form'] = form
            if form.is_valid() and _execute(request, lambda: svc.save_tax_rule(request.user, data=form.cleaned_data), 'Regra parametrizada registrada. Ela não representa validação legal.'):
                return redirect('rh_municipal_public:tributary_home')
        elif action == 'launch':
            require_municipal_permission(request.user, 'tributo.manage', module=m.PHASE_F_MODULES['tributos'])
            form = TaxLaunchForm(request.POST, organization=org, actor=request.user)
            context['launch_form'] = form
            if form.is_valid():
                data = form.cleaned_data
                callback = lambda: svc.create_tax_launch(request.user, taxpayer_id=data['taxpayer'].pk,
                    tax_type=data['tax_type'], base_amount=data['base_amount'], due_date=data['due_date'],
                    rule_id=data['rule'].pk, exercise=data['exercise'],
                    property_id=data['property'].pk if data['property'] else None,
                    competence=data['competence'])
                if _execute(request, callback, 'Lançamento parametrizado criado.'):
                    return redirect('rh_municipal_public:tributary_home')
        elif action == 'installments':
            require_municipal_permission(request.user, 'tributo.manage', module=m.PHASE_F_MODULES['tributos'])
            form = InstallmentForm(request.POST, organization=org, actor=request.user)
            context['installment_form'] = form
            if form.is_valid():
                d = form.cleaned_data
                if _execute(request, lambda: svc.create_installments(request.user, launch_id=d['launch'].pk,
                        count=d['count'], first_due=d['first_due'], interval_days=d['interval_days']), 'Parcelas criadas sem substituir registros anteriores.'):
                    return redirect('rh_municipal_public:tributary_home')
        elif action == 'payment':
            require_municipal_permission(request.user, 'arrecadacao.manage', module=m.PHASE_F_MODULES['tributos'])
            form = ManualPaymentForm(request.POST, organization=org, actor=request.user)
            context['payment_form'] = form
            if form.is_valid():
                d = form.cleaned_data
                if _execute(request, lambda: svc.record_manual_payment(request.user,
                        installment_id=d['installment'].pk, amount=d['amount'], request_id=d['request_id'],
                        reference=d['reference']), 'Recebimento manual registrado e auditado.'):
                    return redirect('rh_municipal_public:tributary_home')
        elif action == 'debt':
            require_municipal_permission(request.user, 'divida.manage', module=m.PHASE_F_MODULES['tributos'])
            launch_id = request.POST.get('launch_id')
            if _execute(request, lambda: svc.register_active_debt(request.user, launch_id=launch_id), 'Inscrição em dívida ativa registrada sem acréscimos presumidos.'):
                return redirect('rh_municipal_public:tributary_home')
        elif action == 'cancel_debt':
            require_municipal_permission(request.user, 'divida.manage', module=m.PHASE_F_MODULES['tributos'])
            if _execute(request, lambda: svc.cancel_active_debt(request.user, debt_id=request.POST.get('debt_id'), reason=request.POST.get('reason')), 'Inscrição cancelada com justificativa auditada.'):
                return redirect('rh_municipal_public:tributary_home')
        elif action == 'emit_dam':
            require_municipal_permission(request.user, 'tributo.manage', module=m.PHASE_F_MODULES['tributos'])
            if _execute(request, lambda: ct_svc.emit_dam(
                org, launch_id=request.POST.get('launch_id'),
                installment_id=request.POST.get('installment_id') or None, actor=request.user,
            ), 'Guia DAM emitida com código de barras FEBRABAN Segmento 8 e Pix.'):
                return redirect('rh_municipal_public:tributary_home')
        elif action == 'issue_certidao':
            require_municipal_permission(request.user, 'tributo.manage', module=m.PHASE_F_MODULES['tributos'])
            if _execute(request, lambda: ct_svc.issue_tax_certificate(
                org, contribuinte_id=request.POST.get('contribuinte_id'),
                imovel_id=request.POST.get('imovel_id') or None,
                finalidade=request.POST.get('finalidade', 'Fins Gerais e Licitações'),
                actor=request.user,
            ), 'Certidão Tributária (CND/CPEN) emitida com chave criptográfica.'):
                return redirect('rh_municipal_public:tributary_home')
        elif action == 'evaluate_itbi':
            require_municipal_permission(request.user, 'tributo.manage', module=m.PHASE_F_MODULES['tributos'])
            if _execute(request, lambda: ct_svc.evaluate_itbi_transfer(
                request.user,
                property_id=request.POST.get('property_id'),
                seller_id=request.POST.get('seller_id'),
                buyer_id=request.POST.get('buyer_id'),
                valor_venal=request.POST.get('valor_venal'),
                valor_transacao=request.POST.get('valor_transacao'),
                rule_id=request.POST.get('rule_id'),
                registry_office=request.POST.get('cartorio_matricula', ''),
            ), 'Avaliação de ITBI e Guia DAM geradas conforme regra vigente.'):
                return redirect('rh_municipal_public:tributary_home')
        else:
            raise PermissionDenied('Ação tributária não autorizada.')
    return _render(request, title='Tributos, DAM, ITBI e Certidões (CND)',
        subtitle='Gestão Tributária Municipal parametrizada (IPTU, ISS, ITBI, Taxas, DAM FEBRABAN/Pix, Dívida Ativa e Certidão Negativa).',
        module_code='tributos_municipais', **context)


@login_required
@require_http_methods(['GET', 'POST'])
def territory_home(request):
    org = current_organization()
    _require_any(request, [('imovel.view', m.PHASE_F_MODULES['territorio']), ('imovel.manage', m.PHASE_F_MODULES['territorio'])])
    properties = scoped_queryset(request.user, m.ImovelMunicipal.objects.filter(organization=org)).select_related('proprietario', 'possuidor', 'secretaria', 'unidade').order_by('inscricao')[:100]
    form = PropertyForm(organization=org, actor=request.user)
    if request.method == 'POST':
        require_municipal_permission(request.user, 'imovel.manage', module=m.PHASE_F_MODULES['territorio'])
        form = PropertyForm(request.POST, organization=org, actor=request.user)
        if form.is_valid() and _execute(request, lambda: svc.create_property(request.user, data=form.cleaned_data), 'Imóvel cadastrado no cadastro territorial municipal.'):
            return redirect('rh_municipal_public:territory_home')
    return _render(request, title='Gestão Territorial',
        subtitle='Cadastro municipal multifinalitário compartilhável com o IPTU. Mapas, recadastramento e planta de valores exigem projeto e dados oficiais próprios.',
        module_code='gestao_territorial', properties=properties, property_form=form)


@login_required
@require_http_methods(['GET', 'POST'])
def nfse_home(request):
    org = current_organization()
    _require_any(request, [('nfse.view', m.PHASE_F_MODULES['nfse']), ('nfse.manage', m.PHASE_F_MODULES['nfse'])])
    notes = list(scoped_queryset(request.user, m.NotaServicoMunicipal.objects.filter(organization=org)).select_related('prestador', 'tomador').order_by('-pk')[:100])
    note_rows = [{'note': n, 'rps': ct_svc.build_nfse_rps_preview(org, n)} for n in notes]
    form = NfseDraftForm(organization=org, actor=request.user)
    if request.method == 'POST':
        require_municipal_permission(request.user, 'nfse.manage', module=m.PHASE_F_MODULES['nfse'])
        form = NfseDraftForm(request.POST, organization=org, actor=request.user)
        if form.is_valid() and _execute(request, lambda: svc.create_nfse_draft(request.user, data=form.cleaned_data), 'Rascunho NFS-e salvo com XML RPS ABRASF. Nenhuma transmissão externa foi realizada.'):
            return redirect('rh_municipal_public:nfse_home')
    return _render(request, title='NFS-e Municipal & Prévia RPS/ISS',
        subtitle='Preparação de rascunhos com prévia XML ABRASF e cálculo ISS parametrizado. Transmissão oficial bloqueada até credenciamento municipal.',
        module_code='nfse_municipal', notes=notes, note_rows=note_rows, nfse_form=form)


@require_GET
def territory_manifest(request):
    response = JsonResponse({
        'id': '/gestao-publica/territorio/', 'name': 'VERTYX Campo Territorial',
        'short_name': 'VERTYX Campo', 'start_url': '/gestao-publica/territorio/',
        'scope': '/gestao-publica/territorio/', 'display': 'standalone',
        'background_color': '#f4f7fb', 'theme_color': '#123b67',
        'icons': [{'src': '/static/pwa/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
                  {'src': '/static/pwa/icon-512.png', 'sizes': '512x512', 'type': 'image/png'}],
    })
    response['Cache-Control'] = 'public, max-age=3600'
    return response


@require_GET
def territory_worker(request):
    # Network-only worker. It never caches authenticated pages, forms or records.
    script = "self.addEventListener('install',()=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',e=>{if(e.request.method==='GET')e.respondWith(fetch(e.request,{cache:'no-store'}));});"
    response = HttpResponse(script, content_type='application/javascript; charset=utf-8')
    response['Cache-Control'] = 'no-store'
    response['Service-Worker-Allowed'] = '/gestao-publica/territorio/'
    return response
