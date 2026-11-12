"""Phase E screens; tenant, module, capability and organizational scope checked server-side."""
import hashlib
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from platform_core.context import current_organization, tenant_context
from platform_core.models import Organization
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


@require_GET
def citizen_portal_inbox(request):
    """Central de Atendimento ao Cidadão e Inbox de Protocolos (Edital Poço Redondo)."""
    from .phase_g_views import get_tenant_branding
    branding = get_tenant_branding(request)
    return render(request, 'rh_municipal/citizen_portal_inbox.html', {
        'title': 'Central do Cidadão',
        'subtitle': 'Atendimento Digital e Acompanhamento de Protocolos',
        'branding': branding,
    })
