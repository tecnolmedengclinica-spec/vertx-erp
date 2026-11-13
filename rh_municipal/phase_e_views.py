"""Phase E screens; tenant, module, capability and organizational scope checked server-side."""
import hashlib
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from platform_core.context import current_organization, tenant_context
from platform_core.models import Organization
from . import cidadao_tributario_services as ct_svc
from . import phase_e_models as m
from . import phase_e_services as svc
from .phase_e_forms import (
    EsicAppealForm, EsicResponseForm, OmbudsmanResponseForm,
    ProcessDocumentForm, ProcessForm, ProcessNoteForm, ProcessTaskForm,
    ProcessTransitionForm, ProtocolForm, PublicEsicForm,
    PublicManifestationForm, SignatureRequestForm, TrackingForm,
    TransparencyForm,
)
from .public_services import has_municipal_permission, require_municipal_permission, scoped_queryset


def _org():
    return current_organization()


def _can(actor, capability, module, record=None):
    return svc.public_module_available(_org(), module) and has_municipal_permission(
        actor, capability, module=module, record=record,
    )


def _require(actor, capability, module, record=None):
    if not svc.public_module_available(_org(), module):
        raise PermissionDenied('O módulo não está contratado e ativo para esta Prefeitura.')
    return require_municipal_permission(actor, capability, module=module, record=record)


def _context(*, title, subtitle, module, **extra):
    return {
        'organization': _org(), 'title': title, 'subtitle': subtitle,
        'module_code': module,
        'can_create_protocol': _can(_user_context.get(), 'protocol.manage', 'processo_digital') if _user_context.get() else False,
        **extra,
    }


# Context is passed explicitly by views; a request-local contextvar avoids
# leaking the previous request's user when templates are rendered concurrently.
from contextvars import ContextVar
_user_context = ContextVar('phase_e_view_user', default=None)


def _render(request, template, *, title, subtitle, module, **extra):
    token = _user_context.set(request.user)
    try:
        context = _context(title=title, subtitle=subtitle, module=module, **extra)
        context['can_create_protocol'] = _can(request.user, 'protocol.manage', 'processo_digital')
        context['can_manage_process'] = _can(request.user, 'process.manage', 'processo_digital')
        context['can_manage_transparency'] = _can(request.user, 'transparency.manage', 'transparencia')
        context['can_publish_transparency'] = _can(request.user, 'transparency.publish', 'transparencia')
        context['can_manage_ouvidoria'] = _can(request.user, 'ouvidoria.manage', 'ouvidoria')
        context['can_respond_ouvidoria'] = _can(request.user, 'ouvidoria.respond', 'ouvidoria')
        context['can_manage_esic'] = _can(request.user, 'esic.manage', 'esic')
        context['can_respond_esic'] = _can(request.user, 'esic.respond', 'esic')
        return render(request, template, context)
    finally:
        _user_context.reset(token)


@login_required
@require_GET
def processes_home(request):
    can_protocol = _can(request.user, 'protocol.view', 'processo_digital')
    can_process = _can(request.user, 'process.view', 'processo_digital')
    if not can_protocol and not can_process:
        raise PermissionDenied('Acesso ao Protocolo/Processo Digital não autorizado.')
    org = _org()
    protocols = scoped_queryset(request.user, m.ProtocoloMunicipal.objects.filter(organization=org)) if can_protocol else m.ProtocoloMunicipal.objects.none()
    processes = scoped_queryset(request.user, m.ProcessoDigitalMunicipal.objects.filter(organization=org)).select_related('secretaria', 'departamento', 'unidade') if can_process else m.ProcessoDigitalMunicipal.objects.none()
    return _render(request, 'rh_municipal/phase_e_module.html', title='Processo Digital e Protocolo',
                   subtitle='Recebimento, distribuição, análise, tramitação e encerramento com histórico.',
                   module='processo_digital', protocols=protocols[:50], processes=processes[:50],
                   protocol_form=ProtocolForm(actor=request.user) if _can(request.user, 'protocol.manage', 'processo_digital') else None,
                   process_form=ProcessForm(actor=request.user) if _can(request.user, 'process.manage', 'processo_digital') else None,
                   can_view_protocol=can_protocol, can_view_process=can_process)


@login_required
@require_POST
def processes_create(request):
    action = request.POST.get('action')
    if action == 'protocol':
        _require(request.user, 'protocol.manage', 'processo_digital')
        form = ProtocolForm(request.POST, actor=request.user)
        if form.is_valid():
            try:
                svc.create_protocol(request.user, data=form.cleaned_data)
                return redirect('rh_municipal_public:processes_home')
            except (PermissionDenied, ValidationError) as exc:
                form.add_error(None, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        return _render(request, 'rh_municipal/phase_e_module.html', title='Processo Digital e Protocolo', subtitle='Confira os campos do protocolo.', module='processo_digital', protocol_form=form, protocols=scoped_queryset(request.user, m.ProtocoloMunicipal.objects.all())[:50], processes=scoped_queryset(request.user, m.ProcessoDigitalMunicipal.objects.all())[:50], can_view_protocol=True, can_view_process=True, process_form=None)
    if action == 'process':
        _require(request.user, 'process.manage', 'processo_digital')
        form = ProcessForm(request.POST, actor=request.user)
        if form.is_valid():
            try:
                row = svc.create_process(request.user, data=form.cleaned_data)
                return redirect('rh_municipal_public:process_detail', pk=row.pk)
            except (PermissionDenied, ValidationError) as exc:
                form.add_error(None, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        return _render(request, 'rh_municipal/phase_e_module.html', title='Processo Digital e Protocolo', subtitle='Confira os campos do processo.', module='processo_digital', process_form=form, protocol_form=None, protocols=scoped_queryset(request.user, m.ProtocoloMunicipal.objects.all())[:50], processes=scoped_queryset(request.user, m.ProcessoDigitalMunicipal.objects.all())[:50], can_view_protocol=True, can_view_process=True)
    raise PermissionDenied('Ação de cadastro inválida.')


@login_required
@require_GET
def process_detail(request, pk):
    _require(request.user, 'process.view', 'processo_digital')
    row = get_object_or_404(scoped_queryset(request.user, m.ProcessoDigitalMunicipal.objects.filter(organization=_org())).select_related('protocolo', 'secretaria', 'departamento', 'setor', 'unidade', 'responsavel'), pk=pk)
    if row.sigilo == m.ProcessoDigitalMunicipal.Sigilo.RESTRITO:
        _require(request.user, 'process.restricted', 'processo_digital', record=row)
    elif row.sigilo == m.ProcessoDigitalMunicipal.Sigilo.SENSIVEL:
        _require(request.user, 'process.sensitive', 'processo_digital', record=row)
    context = {
        'process': row,
        'tramitacoes': scoped_queryset(request.user, row.tramitacoes.all()).order_by('-ocorrido_em')[:100],
        'despachos': scoped_queryset(request.user, row.despachos.all()).order_by('-registrado_em')[:50],
        'pareceres': scoped_queryset(request.user, row.pareceres.all()).order_by('-registrado_em')[:50],
        'documentos': scoped_queryset(request.user, row.documentos.all()).prefetch_related('versoes')[:50],
        'tarefas': scoped_queryset(request.user, row.tarefas.all()).order_by('-criada_em')[:50],
        'transition_form': ProcessTransitionForm(actor=request.user, initial={'status': row.status}),
        'note_form': ProcessNoteForm(), 'task_form': ProcessTaskForm(actor=request.user),
        'document_form': ProcessDocumentForm(),
        'signature_form': SignatureRequestForm(actor=request.user, process=row) if _can(request.user, 'process.signature', 'processo_digital', row) else None,
        'pending_signatures': scoped_queryset(request.user, m.SignatarioMunicipal.objects.filter(membership__user=request.user, status=m.SignatarioMunicipal.Status.AGUARDANDO)),
        'can_dispatch': _can(request.user, 'process.dispatch', 'processo_digital', row),
        'can_analyze': _can(request.user, 'process.analyze', 'processo_digital', row),
        'can_documents': _can(request.user, 'process.documents', 'processo_digital', row),
        'can_signature': _can(request.user, 'process.signature', 'processo_digital', row),
    }
    return _render(request, 'rh_municipal/phase_e_process_detail.html', title=f'Processo {row.numero}',
                   subtitle=row.titulo, module='processo_digital', **context)


@login_required
@require_POST
def process_transition(request, pk):
    _require(request.user, 'process.dispatch', 'processo_digital')
    form = ProcessTransitionForm(request.POST, actor=request.user)
    if form.is_valid():
        try:
            svc.transition_process(request.user, pk, status=form.cleaned_data['status'], data=form.cleaned_data)
        except (PermissionDenied, ValidationError) as exc:
            return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
        return redirect('rh_municipal_public:process_detail', pk=pk)
    return HttpResponse('Revise os dados de tramitação.', status=400)


@login_required
@require_POST
def process_note(request, pk):
    form = ProcessNoteForm(request.POST)
    if not form.is_valid():
        return HttpResponse('Revise o texto do despacho/parecer.', status=400)
    try:
        svc.add_process_note(request.user, pk, text=form.cleaned_data['texto'], kind=form.cleaned_data['tipo'], result=form.cleaned_data.get('resultado'), reference=form.cleaned_data.get('referencia', ''))
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:process_detail', pk=pk)


@login_required
@require_POST
def process_task(request, pk):
    form = ProcessTaskForm(request.POST, actor=request.user)
    if not form.is_valid():
        return HttpResponse('Revise os dados da tarefa.', status=400)
    try:
        svc.create_process_task(request.user, pk, data=form.cleaned_data)
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:process_detail', pk=pk)


@login_required
@require_POST
def process_upload(request, pk):
    form = ProcessDocumentForm(request.POST, request.FILES)
    if not form.is_valid():
        return HttpResponse('Selecione um arquivo permitido, com título e categoria.', status=400)
    try:
        svc.upload_process_document(request.user, pk, title=form.cleaned_data['titulo'], category=form.cleaned_data['categoria'], uploaded_file=form.cleaned_data['arquivo'], content_type=getattr(form.cleaned_data['arquivo'], 'content_type', ''))
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:process_detail', pk=pk)


@login_required
@require_POST
def process_signature_request(request, pk):
    _require(request.user, 'process.signature', 'processo_digital')
    process = get_object_or_404(scoped_queryset(request.user, m.ProcessoDigitalMunicipal.objects.filter(organization=_org())), pk=pk)
    form = SignatureRequestForm(request.POST, actor=request.user, process=process)
    if not form.is_valid():
        return HttpResponse('Selecione uma versão do documento e signatários válidos.', status=400)
    try:
        svc.request_signature(request.user, pk, version_id=form.cleaned_data['versao'].pk, memberships=form.cleaned_data['signatarios'])
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:process_detail', pk=pk)


@login_required
@require_POST
def process_signature_confirm(request, signer_id):
    accepted = request.POST.get('confirmar') == 'sim'
    try:
        svc.confirm_signature(request.user, signer_id, accepted=accepted, note=request.POST.get('observacao', ''))
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:processes_home')


@login_required
@require_GET
def process_document_download(request, version_id):
    _require(request.user, 'process.documents', 'processo_digital')
    version = get_object_or_404(scoped_queryset(request.user, m.VersaoDocumentoProcessoMunicipal.objects.filter(organization=_org())).select_related('documento__processo'), pk=version_id)
    process = version.documento.processo
    if process.sigilo == m.ProcessoDigitalMunicipal.Sigilo.RESTRITO:
        _require(request.user, 'process.restricted', 'processo_digital', record=process)
    elif process.sigilo == m.ProcessoDigitalMunicipal.Sigilo.SENSIVEL:
        _require(request.user, 'process.sensitive', 'processo_digital', record=process)
    response = FileResponse(version.arquivo.open('rb'), as_attachment=True, filename=Path(version.nome_original).name)
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return response


@login_required
@require_GET
def ombudsman_home(request):
    can_view = _can(request.user, 'ouvidoria.view', 'ouvidoria')
    can_manage = _can(request.user, 'ouvidoria.manage', 'ouvidoria')
    can_respond = _can(request.user, 'ouvidoria.respond', 'ouvidoria')
    if not (can_view or can_manage or can_respond):
        raise PermissionDenied('Acesso à Ouvidoria não autorizado.')
    rows = scoped_queryset(request.user, m.ManifestacaoOuvidoriaMunicipal.objects.filter(organization=_org()).select_related('secretaria', 'responsavel'))
    return _render(request, 'rh_municipal/phase_e_module.html', title='Ouvidoria', subtitle='Manifestações sigilosas com classificação, encaminhamento, prazo e histórico.', module='ouvidoria', rows=rows[:100], can_view=True, manifestation_form=None)


@login_required
@require_POST
def ombudsman_respond(request, pk):
    _require(request.user, 'ouvidoria.respond', 'ouvidoria')
    form = OmbudsmanResponseForm(request.POST)
    if not form.is_valid():
        return HttpResponse('Informe uma resposta válida.', status=400)
    try:
        svc.create_ombudsman_response(request.user, pk, response=form.cleaned_data['resposta'], publish=form.cleaned_data['publicar_no_acompanhamento'])
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:ouvidoria_home')


@login_required
@require_GET
def esic_home(request):
    can_view = _can(request.user, 'esic.view', 'esic')
    can_manage = _can(request.user, 'esic.manage', 'esic')
    can_respond = _can(request.user, 'esic.respond', 'esic')
    can_appeal = _can(request.user, 'esic.appeal', 'esic')
    if not (can_view or can_manage or can_respond or can_appeal):
        raise PermissionDenied('Acesso ao E-SIC não autorizado.')
    rows = scoped_queryset(request.user, m.PedidoEsicMunicipal.objects.filter(organization=_org()).select_related('secretaria', 'responsavel'))
    return _render(request, 'rh_municipal/phase_e_module.html', title='E-SIC', subtitle='Pedidos de acesso à informação, respostas e recursos com histórico.', module='esic', rows=rows[:100], can_view=True)


@login_required
@require_POST
def esic_respond(request, pk):
    _require(request.user, 'esic.respond', 'esic')
    form = EsicResponseForm(request.POST)
    if not form.is_valid():
        return HttpResponse('Informe uma resposta válida.', status=400)
    try:
        svc.answer_esic(request.user, pk, text=form.cleaned_data['resposta'], document_key=form.cleaned_data['documento_key'])
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:esic_home')


@login_required
@require_POST
def esic_decide_appeal(request, pk):
    _require(request.user, 'esic.appeal', 'esic')
    status = request.POST.get('status')
    response_text = request.POST.get('resposta', '')
    if status not in m.RecursoEsicMunicipal.Status.values or not response_text.strip():
        return HttpResponse('Informe decisão e fundamento.', status=400)
    try:
        svc.decide_esic_appeal(request.user, pk, status=status, response=response_text)
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:esic_home')


@login_required
@require_GET
def transparency_home(request):
    can_view = _can(request.user, 'transparency.view', 'transparencia')
    can_manage = _can(request.user, 'transparency.manage', 'transparencia')
    can_publish = _can(request.user, 'transparency.publish', 'transparencia')
    if not (can_view or can_manage or can_publish):
        raise PermissionDenied('Acesso à transparência não autorizado.')
    rows = scoped_queryset(request.user, m.PublicacaoTransparenciaMunicipal.objects.filter(organization=_org()))
    return _render(request, 'rh_municipal/phase_e_module.html', title='Transparência', subtitle='Prepare snapshots públicos, revise-os e publique apenas conteúdo aprovado.', module='transparencia', rows=rows[:100], publication_form=__form_transparency(request.user) if can_manage else None, can_view=True)


def __form_transparency(actor):
    return TransparencyForm(actor=actor)


@login_required
@require_POST
def transparency_create(request):
    _require(request.user, 'transparency.manage', 'transparencia')
    form = TransparencyForm(request.POST, actor=request.user)
    if not form.is_valid():
        return _render(request, 'rh_municipal/phase_e_module.html', title='Transparência', subtitle='Revise o conteúdo público.', module='transparencia', rows=scoped_queryset(request.user, m.PublicacaoTransparenciaMunicipal.objects.all())[:100], publication_form=form, can_view=True)
    try:
        svc.create_transparency_publication(request.user, data=form.cleaned_data)
    except (PermissionDenied, ValidationError) as exc:
        form.add_error(None, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        return _render(request, 'rh_municipal/phase_e_module.html', title='Transparência', subtitle='Revise o conteúdo público.', module='transparencia', rows=scoped_queryset(request.user, m.PublicacaoTransparenciaMunicipal.objects.all())[:100], publication_form=form, can_view=True)
    return redirect('rh_municipal_public:transparencia_home')


@login_required
@require_POST
def transparency_submit(request, pk):
    try:
        svc.submit_transparency_for_review(request.user, pk)
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:transparencia_home')


@login_required
@require_POST
def transparency_publish(request, pk):
    try:
        svc.publish_transparency(request.user, pk)
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:transparencia_home')


@login_required
@require_POST
def transparency_withdraw(request, pk):
    try:
        svc.withdraw_transparency(request.user, pk, reason=request.POST.get('motivo', ''))
    except (PermissionDenied, ValidationError) as exc:
        return HttpResponse('; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc), status=400)
    return redirect('rh_municipal_public:transparencia_home')


def _rate_limit(request, org, channel):
    remote = request.META.get('REMOTE_ADDR', 'unknown')
    digest = hashlib.sha256(f'{settings.SECRET_KEY}:{org.pk}:{channel}:{remote}'.encode()).hexdigest()
    key = f'public-intake:{digest}'
    if cache.add(key, 1, timeout=3600):
        return True
    try:
        return cache.incr(key) <= 8
    except ValueError:
        return False


@require_POST
def public_ombudsman_submit(request, slug):
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    if not svc.public_module_available(org, 'ouvidoria'):
        raise Http404
    if not _rate_limit(request, org, 'ouvidoria'):
        return HttpResponse('Limite temporário de envios atingido. Tente novamente mais tarde.', status=429)
    form = PublicManifestationForm(request.POST)
    if form.is_valid():
        tracking = svc.create_tracking_secret()
        try:
            with tenant_context(org):
                row = svc.submit_ombudsman(organization=org, data=form.cleaned_data, tracking_secret=tracking)
        except (PermissionDenied, ValidationError) as exc:
            form.add_error(None, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        else:
            response = render(request, 'rh_municipal/phase_e_public_success.html', {'organization': org, 'protocol': row.protocolo, 'tracking_secret': tracking, 'channel': 'Ouvidoria'})
            response['Cache-Control'] = 'private, no-store'
            response['Referrer-Policy'] = 'no-referrer'
            return response
    return render(request, 'rh_municipal/phase_e_public_form.html', {'organization': org, 'title': 'Fale com a Ouvidoria', 'form': form, 'channel': 'ouvidoria'}, status=400 if request.method == 'POST' else 200)


@require_POST
def public_esic_submit(request, slug):
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    if not svc.public_module_available(org, 'esic'):
        raise Http404
    if not _rate_limit(request, org, 'esic'):
        return HttpResponse('Limite temporário de envios atingido. Tente novamente mais tarde.', status=429)
    form = PublicEsicForm(request.POST)
    if form.is_valid():
        tracking = svc.create_tracking_secret()
        try:
            with tenant_context(org):
                row = svc.submit_esic(organization=org, data=form.cleaned_data, tracking_secret=tracking)
        except (PermissionDenied, ValidationError) as exc:
            form.add_error(None, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        else:
            response = render(request, 'rh_municipal/phase_e_public_success.html', {'organization': org, 'protocol': row.protocolo, 'tracking_secret': tracking, 'channel': 'E-SIC'})
            response['Cache-Control'] = 'private, no-store'
            response['Referrer-Policy'] = 'no-referrer'
            return response
    return render(request, 'rh_municipal/phase_e_public_form.html', {'organization': org, 'title': 'Pedido de informação E-SIC', 'form': form, 'channel': 'esic'}, status=400)


@require_GET
def public_ombudsman_form(request, slug):
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    if not svc.public_module_available(org, 'ouvidoria'):
        raise Http404
    return render(request, 'rh_municipal/phase_e_public_form.html', {'organization': org, 'title': 'Fale com a Ouvidoria', 'form': PublicManifestationForm(), 'channel': 'ouvidoria'})


@require_GET
def public_esic_form(request, slug):
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    if not svc.public_module_available(org, 'esic'):
        raise Http404
    return render(request, 'rh_municipal/phase_e_public_form.html', {'organization': org, 'title': 'Pedido de informação E-SIC', 'form': PublicEsicForm(), 'channel': 'esic'})


@require_GET
def public_citizen_portal(request, slug):
    """Public entry lists only tenant-contracted citizen-facing endpoints."""
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    services = [
        {'code': 'transparencia', 'title': 'Transparência pública', 'description': 'Consulte publicações revisadas e liberadas pelo órgão.', 'url': reverse('rh_municipal_public:public_transparency', args=[slug])},
        {'code': 'ouvidoria', 'title': 'Ouvidoria', 'description': 'Envie manifestação e acompanhe o protocolo com sua chave privada.', 'url': reverse('rh_municipal_public:public_ombudsman_form', args=[slug])},
        {'code': 'esic', 'title': 'E-SIC', 'description': 'Registre pedido de acesso à informação e acompanhe a resposta.', 'url': reverse('rh_municipal_public:public_esic_form', args=[slug])},
        {'code': 'processo_digital', 'title': 'Central do Cidadão (gov.br)', 'description': 'Acesse seus protocolos digitais, requerimentos e acompanhamento online via gov.br.', 'url': reverse('rh_municipal_public:public_citizen_central', args=[slug])},
        {'code': 'tributos_municipais', 'title': 'Portal Tributário & CND', 'description': 'Consulte guias DAM (IPTU/ISS/ITBI) e valide a autenticidade de Certidões Negativas (CND).', 'url': reverse('rh_municipal_public:public_certificate_validate', args=[slug])},
    ]
    available = [row for row in services if svc.public_module_available(org, row['code'])]
    response = render(request, 'rh_municipal/phase_j_portal.html', {'organization': org, 'services': available})
    response['Cache-Control'] = 'public, max-age=60'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response


@require_GET
def public_tracking_form(request, slug):
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    channels = [code for code in ('ouvidoria', 'esic') if svc.public_module_available(org, code)]
    if not channels:
        raise Http404
    response = render(request, 'rh_municipal/phase_j_tracking_form.html', {
        'organization': org,
        'channels': [(channel, 'Ouvidoria' if channel == 'ouvidoria' else 'E-SIC') for channel in channels],
    })
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@require_POST
def public_tracking_select(request, slug):
    channel = request.POST.get('channel', '')
    if channel not in {'ouvidoria', 'esic'}:
        raise Http404
    return public_tracking(request, slug, channel)


@require_POST
def public_tracking(request, slug, channel):
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    if channel not in {'ouvidoria', 'esic'} or not svc.public_module_available(org, channel):
        raise Http404
    form = TrackingForm(request.POST)
    if not form.is_valid():
        return HttpResponse('Informe protocolo e chave de acompanhamento.', status=400)
    try:
        with tenant_context(org):
            if channel == 'ouvidoria':
                row = m.ManifestacaoOuvidoriaMunicipal.objects.filter(organization=org, protocolo=form.cleaned_data['protocolo']).first()
            else:
                row = m.PedidoEsicMunicipal.objects.filter(organization=org, protocolo=form.cleaned_data['protocolo']).first()
            if not row or not svc.tracking_secret_matches(form.cleaned_data['chave'], row.chave_acompanhamento_digest):
                raise Http404
            answers = []
            if channel == 'ouvidoria':
                answers = list(row.respostas.filter(publicar_no_acompanhamento=True).order_by('respondida_em'))
            elif row.status in {m.PedidoEsicMunicipal.Status.RESPONDIDO, m.PedidoEsicMunicipal.Status.ENCERRADO, m.PedidoEsicMunicipal.Status.EM_RECURSO}:
                answers = list(row.respostas.order_by('respondida_em'))
            safe = {'protocol': row.protocolo, 'status': row.status, 'received': row.recebido_em, 'answers': answers,
                    'public_id': getattr(row, 'public_id', None), 'tracking_secret': form.cleaned_data['chave']}
    except Http404:
        return HttpResponse('Protocolo ou chave de acompanhamento inválidos.', status=404)
    response = render(request, 'rh_municipal/phase_e_tracking.html', {'organization': org, 'channel': channel, **safe})
    response['Cache-Control'] = 'private, no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_POST
def public_esic_appeal(request, slug, public_id):
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    if not svc.public_module_available(org, 'esic'):
        raise Http404
    form = EsicAppealForm(request.POST)
    key = request.POST.get('chave', '')
    if not form.is_valid():
        return HttpResponse('Informe os fundamentos do recurso.', status=400)
    try:
        with tenant_context(org):
            row = m.PedidoEsicMunicipal.objects.filter(organization=org, public_id=public_id).first()
            if not row or not svc.tracking_secret_matches(key, row.chave_acompanhamento_digest):
                raise Http404
            svc.file_esic_appeal(organization=org, request_id=public_id, text=form.cleaned_data['fundamento'], tracking_secret=key)
    except (Http404, PermissionDenied, ValidationError) as exc:
        return HttpResponse('Não foi possível protocolar o recurso com os dados informados.', status=404 if isinstance(exc, Http404) else 400)
    return HttpResponse('Recurso protocolado. Guarde o protocolo e a chave de acompanhamento.', status=201)


@require_GET
def public_transparency(request, slug):
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    if not svc.public_module_available(org, 'transparencia'):
        raise Http404
    rows = m.PublicacaoTransparenciaMunicipal.all_objects.filter(
        organization=org, status=m.PublicacaoTransparenciaMunicipal.Status.PUBLICADO,
    ).order_by('-publicado_em')
    response = render(request, 'rh_municipal/phase_e_transparency_public.html', {'organization': org, 'publications': rows[:100]})
    response['Cache-Control'] = 'public, max-age=60'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def _resolve_citizen_org(request, slug=None):
    explicit_slug = (slug or request.GET.get('org') or request.POST.get('org') or '').strip()
    if explicit_slug:
        return get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=explicit_slug)
    org = current_organization()
    if org:
        return org
    last_slug = request.session.get('citizen_last_org_slug') if hasattr(request, 'session') else None
    if last_slug:
        org = Organization.objects.filter(slug=last_slug, status=Organization.Status.ACTIVE).first()
    return org


@require_GET
def public_govbr_start(request, slug):
    """Start Login Único gov.br OIDC + PKCE or local homologation login for municipality."""
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    target_url, _is_local = ct_svc.build_govbr_authorize_flow(org, request)
    return redirect(target_url)


@require_http_methods(['GET', 'POST'])
def public_govbr_homolog(request, slug):
    """Homologation gov.br identity verification screen when Serpro client_id is in sandbox mode."""
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    ct_svc.require_local_govbr_demo(request)
    cfg = ct_svc.get_or_init_govbr_config(org)
    if not cfg.ativo or not cfg.modo_homologacao_local or cfg.ambiente != 'HOMOLOGACAO':
        raise PermissionDenied('Simulação gov.br indisponível.')
    state = request.GET.get('state') or request.POST.get('state') or ''
    if request.method == 'POST':
        sess_oidc = request.session.get(f'govbr_oidc_{org.pk}', {})
        if not state or not sess_oidc.get('state') or sess_oidc.get('state') != state:
            return HttpResponse('Estado OIDC inválido ou expirado.', status=400)
        try:
            ct_svc.authenticate_govbr_citizen(
                org, request,
                cpf=request.POST.get('cpf', ''),
                nome=request.POST.get('nome', ''),
                email=request.POST.get('email', ''),
                telefone=request.POST.get('telefone', ''),
                nivel=request.POST.get('nivel', 'PRATA'),
                origem='GOVBR_HOMOLOGACAO_LOCAL',
            )
        except ValidationError as exc:
            return render(request, 'rh_municipal/public_govbr_login.html', {
                'organization': org,
                'govbr_config': cfg,
                'state': state,
                'error': '; '.join(exc.messages),
            }, status=400)
        request.session.pop(f'govbr_oidc_{org.pk}', None)
        return redirect('rh_municipal_public:public_citizen_central', slug=org.slug)

    return render(request, 'rh_municipal/public_govbr_login.html', {
        'organization': org,
        'govbr_config': cfg,
        'state': state,
    })


@require_GET
def public_govbr_callback(request, slug):
    # Fail closed until authorization-code exchange and JWT validation are implemented.
    return HttpResponse('Integração oficial gov.br ainda não homologada.', status=503)


@require_POST
def public_govbr_logout(request, slug):
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    ct_svc.clear_govbr_session(org, request)
    messages.info(request, 'Sessão gov.br encerrada com segurança.')
    return redirect('rh_municipal_public:public_citizen_central', slug=org.slug)


@require_http_methods(['GET', 'POST'])
def public_certificate_validate(request, slug):
    """Validação pública de autenticidade de Certidão Tributária (CND / CPEN) e emissão rápida."""
    org = get_object_or_404(Organization.objects.filter(status=Organization.Status.ACTIVE), slug=slug)
    if not svc.public_module_available(org, 'tributos_municipais'):
        raise Http404
    numero = request.GET.get('numero') or request.POST.get('numero') or ''
    codigo = request.GET.get('codigo') or request.POST.get('codigo') or ''
    result = ct_svc.validate_tax_certificate_public(org, numero=numero, codigo_autenticacao=codigo) if (numero or codigo) else None
    govbr_sess = ct_svc.get_active_govbr_session(org, request)
    response = render(request, 'rh_municipal/public_cnd_validate.html', {
        'organization': org,
        'numero': numero,
        'codigo': codigo,
        'result': result,
        'govbr_session': govbr_sess,
    })
    response['Cache-Control'] = 'private, no-store'
    return response


@require_http_methods(['GET', 'POST'])
def public_citizen_central(request, slug):
    return _render_citizen_portal_inbox(request, slug=slug)


@require_http_methods(['GET', 'POST'])
def citizen_portal_inbox(request):
    """Central de Atendimento ao Cidadão e Caixa de Protocolos Gov.br conectada ao banco real."""
    return _render_citizen_portal_inbox(request, slug=None)


def _render_citizen_portal_inbox(request, slug=None):
    import json
    org = _resolve_citizen_org(request, slug=slug)
    if not org or not any(svc.public_module_available(org, module) for module in ('processo_digital', 'saude_sus', 'tributos_municipais')):
        raise Http404('Nenhuma Prefeitura ativa encontrada.')

    request.session['citizen_last_org_slug'] = org.slug
    govbr_sess = ct_svc.get_active_govbr_session(org, request)
    govbr_cfg = ct_svc.get_or_init_govbr_config(org)

    # Branding strictly reflecting the resolved tenant org
    from .models import PerfilPrefeitura
    perfil = PerfilPrefeitura.all_objects.filter(organization=org).first()
    branding = {
        'orgao_gestor': 'Atendimento ao Cidadão',
        'unidade_nome': 'Portal do Cidadão',
        'cor_primaria': '#0284c7',
        'cor_secundaria': '#10b981',
        'nome_institucional': (perfil.nome_institucional if perfil and perfil.nome_institucional else org.nome_fantasia or org.razao_social),
        'municipio': org.cidade or 'Município',
        'uf': org.uf or 'BR',
        'municipio_uf': f'{org.cidade} - {org.uf}' if org.cidade and org.uf else (org.nome_fantasia or org.razao_social),
    }

    if request.method == 'POST':
        action = request.POST.get('action', '')
        target_redirect = reverse('rh_municipal_public:public_citizen_central', args=[org.slug])
        if not govbr_sess:
            messages.error(request, 'Faça login com sua conta gov.br (CPF) antes de protocolar ou emitir documentos.')
            return redirect(target_redirect)
        try:
            if action == 'create_protocol':
                proc, _ = ct_svc.create_citizen_digital_protocol(
                    org,
                    cpf=govbr_sess.cpf,
                    nome=govbr_sess.nome,
                    email=govbr_sess.email,
                    telefone=govbr_sess.telefone,
                    assunto=request.POST.get('assunto', ''),
                    descricao=request.POST.get('descricao', ''),
                    secretaria_id=request.POST.get('secretaria_id') or None,
                    user=request.user,
                )
                messages.success(request, f'Protocolo digital {proc.numero} aberto com sucesso no sistema da Prefeitura!')
            elif action == 'book_appointment':
                appt, ticket = ct_svc.book_citizen_sus_appointment(
                    org,
                    cpf=govbr_sess.cpf,
                    nome=govbr_sess.nome,
                    unit_id=request.POST.get('unidade_id'),
                    procedure_code=request.POST.get('procedimento', '0301010064'),
                    complaint=request.POST.get('queixa', ''),
                    user=request.user,
                )
                messages.success(request, f'Consulta SUS agendada! Senha da fila na UBS: {ticket.codigo_formatado}.')
            elif action == 'emit_cnd':
                from platform_core.context import tenant_context
                from .phase_f_models import ContribuinteMunicipal
                with tenant_context(org):
                    contrib = ContribuinteMunicipal.objects.filter(organization=org, documento=govbr_sess.cpf).first()
                if contrib is None:
                    raise ValidationError('CPF sem cadastro tributário no município para emissão de certidão.')
                cert = ct_svc.issue_tax_certificate(
                    org, contribuinte_id=contrib.pk,
                    finalidade=request.POST.get('finalidade', 'Portal do Cidadão gov.br'),
                    actor=request.user,
                )
                messages.success(request, f'Certidão {cert.numero} emitida! Chave: {cert.codigo_autenticacao}.')
            elif action == 'emit_dam':
                from .phase_f_models import LancamentoTributarioMunicipal
                if not LancamentoTributarioMunicipal.all_objects.filter(organization=org, pk=request.POST.get('launch_id'), contribuinte__documento=govbr_sess.cpf).exists():
                    raise PermissionDenied('Lançamento fora do cadastro autorizado.')
                dam = ct_svc.emit_dam(
                    org, launch_id=request.POST.get('launch_id'),
                    installment_id=request.POST.get('installment_id') or None,
                    actor=request.user,
                )
                messages.success(request, f'Guia DAM {dam.numero_dam} emitida com linha digitável e Pix!')
            else:
                messages.error(request, 'Ação do Portal do Cidadão desconhecida.')
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        return redirect(target_redirect)

    portal_data = ct_svc.load_citizen_portal_data(org, citizen_cpf=govbr_sess.cpf if govbr_sess else None)
    active_orgs = list(Organization.objects.filter(
        status=Organization.Status.ACTIVE, erp_accesses__erp__code='gestao-publica',
        erp_accesses__enabled=True,
    ).distinct().order_by('nome_fantasia')[:30])

    # Serialize timeline data safely for JS inspection modal
    timeline_map = {}
    for proc in portal_data['open_processes'] + portal_data['archived_processes']:
        timeline_map[proc.numero] = {
            'numero': proc.numero,
            'titulo': proc.titulo,
            'status': proc.get_status_display(),
            'secretaria': proc.secretaria.nome if proc.secretaria else 'Protocolo Geral',
            'tramites': [
                {
                    'acao': tr.get_acao_display(),
                    'data': timezone.localtime(tr.ocorrido_em).strftime('%d/%m/%Y %H:%M'),
                    'descricao': tr.descricao,
                }
                for tr in proc.tramitacoes.all().order_by('ocorrido_em')
            ],
        }

    response = render(request, 'rh_municipal/citizen_portal_inbox.html', {
        'title': 'Central do Cidadão',
        'subtitle': 'Atendimento Digital, Tributos e Acompanhamento via gov.br',
        'organization': org,
        'active_orgs': active_orgs,
        'branding': branding,
        'govbr_session': govbr_sess,
        'govbr_config': govbr_cfg,
        'cpf_masked': ct_svc._mask_cpf(govbr_sess.cpf) if govbr_sess else '',
        'initials': ''.join(part[0] for part in govbr_sess.nome.split()[:2]).upper() if govbr_sess else 'GB',
        'timeline_json': json.dumps(timeline_map, ensure_ascii=False), 'timeline_data': timeline_map,
        **portal_data,
    })
    response['Cache-Control'] = 'private, no-store'
    return response
