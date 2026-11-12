"""Audited Phase E workflows and explicit public-safe data boundaries."""
import hashlib
import hmac
import secrets
from pathlib import PurePosixPath

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from platform_core.context import current_organization
from platform_core.models import (
    ERPProductModule, ItemContratoSaaS, ModuloOrganizacao, OrganizationERP,
)
from platform_core.saas_models import AssinaturaSaaS
from . import phase_e_models as m
from .public_services import audit_public, require_municipal_permission, scoped_queryset


PROCESS_TRANSITIONS = {
    m.ProcessoDigitalMunicipal.Status.AUTUADO: {m.ProcessoDigitalMunicipal.Status.DISTRIBUIDO, m.ProcessoDigitalMunicipal.Status.EM_ANALISE},
    m.ProcessoDigitalMunicipal.Status.DISTRIBUIDO: {m.ProcessoDigitalMunicipal.Status.EM_ANALISE, m.ProcessoDigitalMunicipal.Status.AGUARDANDO_PARECER},
    m.ProcessoDigitalMunicipal.Status.EM_ANALISE: {m.ProcessoDigitalMunicipal.Status.DISTRIBUIDO, m.ProcessoDigitalMunicipal.Status.AGUARDANDO_PARECER, m.ProcessoDigitalMunicipal.Status.AGUARDANDO_ASSINATURA, m.ProcessoDigitalMunicipal.Status.PENDENTE_INTERESSADO, m.ProcessoDigitalMunicipal.Status.CONCLUIDO},
    m.ProcessoDigitalMunicipal.Status.AGUARDANDO_PARECER: {m.ProcessoDigitalMunicipal.Status.EM_ANALISE, m.ProcessoDigitalMunicipal.Status.PENDENTE_INTERESSADO},
    m.ProcessoDigitalMunicipal.Status.AGUARDANDO_ASSINATURA: {m.ProcessoDigitalMunicipal.Status.EM_ANALISE, m.ProcessoDigitalMunicipal.Status.CONCLUIDO},
    m.ProcessoDigitalMunicipal.Status.PENDENTE_INTERESSADO: {m.ProcessoDigitalMunicipal.Status.EM_ANALISE, m.ProcessoDigitalMunicipal.Status.ARQUIVADO},
    m.ProcessoDigitalMunicipal.Status.CONCLUIDO: {m.ProcessoDigitalMunicipal.Status.ARQUIVADO},
    m.ProcessoDigitalMunicipal.Status.ARQUIVADO: set(),
}


def _org():
    org = current_organization()
    if org is None:
        raise PermissionDenied('Selecione uma Prefeitura ativa para continuar.')
    return org


def _require(actor, capability, module):
    if not public_module_available(_org(), module):
        raise PermissionDenied('O módulo não está contratado e ativo para esta Prefeitura.')
    require_municipal_permission(actor, capability, module=module)
    return _org()


def _audit(actor, action, record, *, changes=None, reason=''):
    audit = m.AuditoriaProcessoDigitalMunicipal(
        organization=record.organization, acao=action,
        entidade=record._meta.label_lower,
        entidade_public_id=getattr(record, 'public_id', None),
        alteracoes=changes or {}, motivo=reason, usuario=actor,
    )
    audit._service_write = True
    audit.save()
    if actor is not None:
        audit_public(actor, action, record, changes or {})
    return audit


def _save_immutable(model_class, **fields):
    row = model_class(**fields)
    row._service_write = True
    row.save()
    return row


def _next_number(org, family, today=None):
    today = today or timezone.localdate()
    year = today.year
    qs = m.ContadorExpedienteMunicipal.objects.select_for_update()
    counter = qs.filter(organization=org, familia=family, exercicio=year).first()
    if counter is None:
        try:
            with transaction.atomic():
                m.ContadorExpedienteMunicipal.objects.create(
                    organization=org, familia=family, exercicio=year, proximo_numero=2,
                )
            sequence = 1
        except IntegrityError:
            counter = qs.get(organization=org, familia=family, exercicio=year)
            sequence = counter.proximo_numero
            counter.proximo_numero += 1
            counter.save(update_fields=['proximo_numero'])
    else:
        sequence = counter.proximo_numero
        counter.proximo_numero += 1
        counter.save(update_fields=['proximo_numero'])
    return year, f'{year}/{sequence:06d}'


def _same_tenant(row, org, field):
    related = getattr(row, field, None)
    if related is None:
        return
    related_org_id = getattr(related, 'organization_id', None)
    if related_org_id not in (None, org.pk):
        raise ValidationError({field: 'O registro relacionado pertence a outra Prefeitura.'})


def _authorized_process(actor, org, process_id, *, lock=False):
    qs = m.ProcessoDigitalMunicipal.objects.filter(organization=org)
    if lock:
        qs = qs.select_for_update()
    row = scoped_queryset(actor, qs).filter(pk=process_id).first()
    if row is None:
        raise ValidationError('O processo não existe ou está fora do seu escopo.')
    if row.sigilo == m.ProcessoDigitalMunicipal.Sigilo.RESTRITO:
        require_municipal_permission(actor, 'process.restricted', module=m.PHASE_E_MODULES['processos'], record=row)
    elif row.sigilo == m.ProcessoDigitalMunicipal.Sigilo.SENSIVEL:
        require_municipal_permission(actor, 'process.sensitive', module=m.PHASE_E_MODULES['processos'], record=row)
    return row


@transaction.atomic
def create_protocol(actor, *, data):
    org = _require(actor, 'protocol.manage', m.PHASE_E_MODULES['processos'])
    for field in ('secretaria', 'departamento', 'setor', 'unidade', 'responsavel'):
        _same_tenant(data, org, field)
    year, sequence = _next_number(org, m.ContadorExpedienteMunicipal.Familia.PROTOCOLO)
    row = m.ProtocoloMunicipal(
        organization=org, numero=f'PRT-{sequence}', exercicio=year,
        criado_por=actor, **data,
    )
    row.save()
    _audit(actor, 'PROTOCOLO_CRIADO', row, changes={'numero': row.numero, 'origem': row.origem, 'status': row.status})
    return row


@transaction.atomic
def create_process(actor, *, data):
    org = _require(actor, 'process.manage', m.PHASE_E_MODULES['processos'])
    for field in ('protocolo', 'secretaria', 'departamento', 'setor', 'unidade', 'responsavel'):
        _same_tenant(data, org, field)
    protocol = data.get('protocolo')
    if protocol and (protocol.organization_id != org.pk or hasattr(protocol, 'processo_digital')):
        raise ValidationError('O protocolo não pertence a esta Prefeitura ou já está vinculado a processo.')
    year, sequence = _next_number(org, m.ContadorExpedienteMunicipal.Familia.PROCESSO)
    row = m.ProcessoDigitalMunicipal(organization=org, numero=f'PROC-{sequence}', exercicio=year, criado_por=actor, **data)
    row.save()
    _save_immutable(m.TramiteProcessoMunicipal,
        organization=org, processo=row, acao=m.TramiteProcessoMunicipal.Acao.AUTUACAO,
        status_novo=row.status, secretaria_destino=row.secretaria, departamento_destino=row.departamento,
        setor_destino=row.setor, unidade_destino=row.unidade, responsavel_destino=row.responsavel,
        descricao='Processo autuado.', registrado_por=actor,
    )
    _audit(actor, 'PROCESSO_AUTUADO', row, changes={'numero': row.numero, 'status': row.status})
    return row


@transaction.atomic
def transition_process(actor, process_id, *, status, data=None):
    data = data or {}
    capability = 'process.close' if status in {
        m.ProcessoDigitalMunicipal.Status.CONCLUIDO,
        m.ProcessoDigitalMunicipal.Status.ARQUIVADO,
    } else 'process.dispatch'
    org = _require(actor, capability, m.PHASE_E_MODULES['processos'])
    row = _authorized_process(actor, org, process_id, lock=True)
    if status not in PROCESS_TRANSITIONS.get(row.status, set()):
        raise ValidationError('A mudança de situação não é permitida a partir do estado atual.')
    destination = {}
    for field in ('secretaria', 'departamento', 'setor', 'unidade', 'responsavel'):
        if field in data:
            _same_tenant({field: data[field]}, org, field)
            destination[field] = data[field]
            setattr(row, field, data[field])
    before = row.status
    row.status = status
    if status == m.ProcessoDigitalMunicipal.Status.CONCLUIDO:
        row.concluido_em = timezone.now()
    row.save()
    _save_immutable(m.TramiteProcessoMunicipal,
        organization=org, processo=row, acao=(
            m.TramiteProcessoMunicipal.Acao.CONCLUSAO if status == m.ProcessoDigitalMunicipal.Status.CONCLUIDO
            else m.TramiteProcessoMunicipal.Acao.ARQUIVAMENTO if status == m.ProcessoDigitalMunicipal.Status.ARQUIVADO
            else m.TramiteProcessoMunicipal.Acao.DISTRIBUICAO
        ), status_anterior=before, status_novo=status,
        secretaria_origem=data.get('secretaria_origem'), secretaria_destino=row.secretaria,
        departamento_origem=data.get('departamento_origem'), departamento_destino=row.departamento,
        setor_origem=data.get('setor_origem'), setor_destino=row.setor,
        unidade_origem=data.get('unidade_origem'), unidade_destino=row.unidade,
        responsavel_origem=data.get('responsavel_origem'), responsavel_destino=row.responsavel,
        descricao=data.get('descricao', ''), registrado_por=actor,
    )
    _audit(actor, 'PROCESSO_SITUACAO_ALTERADA', row, changes={'de': before, 'para': status, **{key: getattr(value, 'pk', None) for key, value in destination.items()}})
    return row


@transaction.atomic
def add_process_note(actor, process_id, *, text, kind='DESPACHO', result=None, reference=''):
    org = _require(actor, 'process.analyze', m.PHASE_E_MODULES['processos'])
    row = _authorized_process(actor, org, process_id, lock=True)
    if not text.strip():
        raise ValidationError('Informe o conteúdo do despacho ou parecer.')
    if kind == 'PARECER':
        record = _save_immutable(m.ParecerProcessoMunicipal,
            organization=org, processo=row,
            resultado=result or m.ParecerProcessoMunicipal.Resultado.INFORMATIVO,
            texto=text, referencia=reference, registrado_por=actor,
        )
        action = m.TramiteProcessoMunicipal.Acao.PARECER
    else:
        record = _save_immutable(m.DespachoProcessoMunicipal,
            organization=org, processo=row, tipo=kind,
            texto=text, registrado_por=actor,
        )
        action = m.TramiteProcessoMunicipal.Acao.DESPACHO
    _save_immutable(m.TramiteProcessoMunicipal,
        organization=org, processo=row, acao=action, status_anterior=row.status,
        status_novo=row.status, secretaria_destino=row.secretaria,
        departamento_destino=row.departamento, setor_destino=row.setor,
        unidade_destino=row.unidade, responsavel_destino=row.responsavel,
        descricao=f'{kind} registrado.', registrado_por=actor,
    )
    _audit(actor, f'PROCESSO_{kind}_REGISTRADO', row, changes={'registro': record.pk})
    return record


@transaction.atomic
def create_process_task(actor, process_id, *, data):
    org = _require(actor, 'process.manage', m.PHASE_E_MODULES['processos'])
    row = _authorized_process(actor, org, process_id)
    if data.get('atribuida_a'):
        _same_tenant({'atribuida_a': data['atribuida_a']}, org, 'atribuida_a')
    task = m.TarefaProcessoMunicipal(organization=org, processo=row, criada_por=actor, **data)
    task.save()
    _audit(actor, 'TAREFA_PROCESSO_CRIADA', task, changes={'processo': row.pk, 'prazo': str(task.prazo_em or '')})
    return task


@transaction.atomic
def upload_process_document(actor, process_id, *, title, category, uploaded_file, content_type=''):
    org = _require(actor, 'process.documents', m.PHASE_E_MODULES['processos'])
    row = _authorized_process(actor, org, process_id)
    if not uploaded_file or uploaded_file.size <= 0 or uploaded_file.size > 25 * 1024 * 1024:
        raise ValidationError('O arquivo precisa ter conteúdo e não pode ultrapassar 25 MB.')
    suffix = PurePosixPath(uploaded_file.name).suffix.lower()
    if suffix not in {'.pdf', '.png', '.jpg', '.jpeg', '.docx', '.xlsx', '.txt'}:
        raise ValidationError('Formato de documento não permitido. Use PDF, imagem, DOCX, XLSX ou TXT.')
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        digest.update(chunk)
    uploaded_file.seek(0)
    document = m.DocumentoProcessoMunicipal(
        organization=org, processo=row, titulo=title, categoria=category,
        criado_por=actor,
    )
    document.save()
    version_number = (document.versoes.order_by('-numero_versao').values_list('numero_versao', flat=True).first() or 0) + 1
    version = _save_immutable(m.VersaoDocumentoProcessoMunicipal,
        organization=org, documento=document, numero_versao=version_number,
        arquivo=uploaded_file, nome_original=PurePosixPath(uploaded_file.name).name[:180],
        content_type=content_type[:100], tamanho_bytes=uploaded_file.size,
        sha256=digest.hexdigest(), enviado_por=actor,
    )
    document.atual = version
    document.save(update_fields=['atual'])
    _audit(actor, 'DOCUMENTO_PROCESSO_VERSAO_ADICIONADA', document, changes={'versao': version_number, 'sha256': version.sha256})
    return document


@transaction.atomic
def request_signature(actor, process_id, *, version_id, memberships):
    org = _require(actor, 'process.signature', m.PHASE_E_MODULES['processos'])
    process = _authorized_process(actor, org, process_id)
    version = scoped_queryset(actor, m.VersaoDocumentoProcessoMunicipal.objects.filter(organization=org, pk=version_id)).first()
    if process is None or version is None or version.documento.processo_id != process.pk:
        raise ValidationError('Processo ou versão documental inexistente ou fora do escopo.')
    memberships = list(memberships)
    if not memberships or len(memberships) > 20:
        raise ValidationError('Selecione entre 1 e 20 signatários.')
    if any(mem.organization_id != org.pk or mem.status != 'ATIVO' or not mem.user.is_active for mem in memberships):
        raise ValidationError('Todos os signatários devem ter membership ativo nesta Prefeitura.')
    request = m.SolicitacaoAssinaturaMunicipal(
        organization=org, processo=process, versao_documento=version,
        solicitado_por=actor,
    )
    request.save()
    for order, membership in enumerate(memberships, start=1):
        m.SignatarioMunicipal.objects.create(
            organization=org, solicitacao=request, membership=membership, ordem=order,
        )
    process.status = m.ProcessoDigitalMunicipal.Status.AGUARDANDO_ASSINATURA
    process.save(update_fields=['status', 'atualizado_em'])
    _save_immutable(m.TramiteProcessoMunicipal,
        organization=org, processo=process, acao=m.TramiteProcessoMunicipal.Acao.ASSINATURA,
        status_anterior='', status_novo=process.status, secretaria_destino=process.secretaria,
        departamento_destino=process.departamento, setor_destino=process.setor,
        unidade_destino=process.unidade, responsavel_destino=process.responsavel,
        descricao='Solicitação de assinatura/ciência registrada. Este registro não declara validade jurídica de assinatura digital.',
        registrado_por=actor,
    )
    _audit(actor, 'SOLICITACAO_ASSINATURA_CRIADA', request, changes={'processo': process.pk, 'signatarios': len(memberships), 'sha256_documento': version.sha256})
    return request


@transaction.atomic
def confirm_signature(actor, signer_id, *, accepted, note=''):
    org = _require(actor, 'process.signature', m.PHASE_E_MODULES['processos'])
    signer = m.SignatarioMunicipal.objects.select_for_update().select_related('membership__user', 'solicitacao__versao_documento').filter(
        organization=org, pk=signer_id, membership__user=actor,
    ).first()
    if signer is None or signer.membership.status != 'ATIVO':
        raise ValidationError('A solicitação não pertence a um acesso ativo deste usuário.')
    if signer.status != m.SignatarioMunicipal.Status.AGUARDANDO:
        raise ValidationError('Esta etapa de assinatura já foi concluída.')
    signer.status = m.SignatarioMunicipal.Status.CONFIRMADA if accepted else m.SignatarioMunicipal.Status.RECUSADA
    signer.confirmado_em = timezone.now()
    confirmation = f'{org.pk}:{signer.pk}:{signer.solicitacao.versao_documento.sha256}:{signer.confirmado_em.isoformat()}:{bool(accepted)}'
    signer.hash_confirmacao = hashlib.sha256(confirmation.encode()).hexdigest() if accepted else ''
    signer.observacao = note[:2000]
    signer.save(update_fields=['status', 'confirmado_em', 'hash_confirmacao', 'observacao'])
    _save_immutable(m.EventoAssinaturaMunicipal,
        organization=org, solicitacao=signer.solicitacao, signatario=signer,
        evento='CONFIRMACAO_REGISTRADA' if accepted else 'RECUSA_REGISTRADA',
        hash_documento=signer.solicitacao.versao_documento.sha256,
        hash_confirmacao=signer.hash_confirmacao, registrado_por=actor,
        observacao='Registro eletrônico de confirmação; não representa assinatura ICP-Brasil nem atesta validade jurídica.',
    )
    if not signer.solicitacao.signatarios.filter(status=m.SignatarioMunicipal.Status.AGUARDANDO).exists():
        signer.solicitacao.status = m.SolicitacaoAssinaturaMunicipal.Status.CONCLUIDA
        signer.solicitacao.concluido_em = timezone.now()
        signer.solicitacao.save(update_fields=['status', 'concluido_em'])
    _audit(actor, 'SIGNATARIO_CONFIRMACAO_REGISTRADA', signer, changes={'aceita': bool(accepted), 'solicitacao': signer.solicitacao_id})
    return signer


def public_module_available(organization, module_code):
    """Fail-closed module check for anonymous municipal intake/public pages."""
    if organization is None or organization.status != organization.Status.ACTIVE:
        return False
    product = OrganizationERP.objects.filter(
        organization=organization, erp__code='gestao-publica', erp__active=True, enabled=True,
    ).exists()
    module = ERPProductModule.objects.filter(
        erp__code='gestao-publica', erp__active=True, code=module_code, active=True,
    ).first()
    if not product or module is None:
        return False
    from platform_core.modules import platform_module_override
    from platform_core.saas_models import AjusteModuloSaaS
    override = platform_module_override(organization, module_code)
    if override == AjusteModuloSaaS.Modo.DENY:
        return False
    if override == AjusteModuloSaaS.Modo.ALLOW:
        return True
    if ModuloOrganizacao.objects.filter(organization=organization, codigo=module_code, habilitado=False).exists():
        return False
    today = timezone.localdate()
    active_items = list(ItemContratoSaaS.objects.filter(
        (Q(contrato__inicio_em__isnull=True) | Q(contrato__inicio_em__lte=today)),
        (Q(contrato__fim_em__isnull=True) | Q(contrato__fim_em__gte=today)),
        contrato__organization=organization, contrato__status='ATIVO',
        erp__code='gestao-publica', erp__active=True, ativo=True,
    ).select_related('plano')[:2])
    if len(active_items) > 1:
        return False
    if active_items:
        return module_code in active_items[0].modulos_snapshot
    subscription = AssinaturaSaaS.objects.filter(organization=organization).select_related('plano', 'plano__erp').first()
    if subscription:
        plan = subscription.plano
        if not plan.erp_id or plan.erp.code != 'gestao-publica':
            return False
        if subscription.status != AssinaturaSaaS.Status.ACTIVE or not plan.ativo:
            return False
        return plan.modulos_produto.filter(modulo=module).exists()
    # New public verticals require explicit plan/contract entitlement. Legacy
    # organization grants are not enough to publish a public endpoint.
    return False


def _issue_public_number(org, family):
    year, sequence = _next_number(org, family)
    prefix = {m.ContadorExpedienteMunicipal.Familia.OUVIDORIA: 'OUV', m.ContadorExpedienteMunicipal.Familia.ESIC: 'SIC'}[family]
    return f'{prefix}-{sequence}'


@transaction.atomic
def submit_ombudsman(*, organization, data, tracking_secret):
    if not public_module_available(organization, m.PHASE_E_MODULES['ouvidoria']):
        raise PermissionDenied('O canal público da Ouvidoria não está habilitado para esta Prefeitura.')
    if data.get('anonima'):
        data = {**data, 'nome': '', 'documento': '', 'email': '', 'telefone': ''}
    else:
        if not data.get('nome') or not data.get('email'):
            raise ValidationError('Informe nome e e-mail ou escolha registrar a manifestação anonimamente.')
    number = _issue_public_number(organization, m.ContadorExpedienteMunicipal.Familia.OUVIDORIA)
    row = m.ManifestacaoOuvidoriaMunicipal(
        organization=organization, protocolo=number,
        chave_acompanhamento_digest=hashlib.sha256(tracking_secret.encode()).hexdigest(),
        **data,
    )
    row.save()
    _audit(None, 'OUVIDORIA_MANIFESTACAO_RECEBIDA', row, changes={'protocolo': number, 'tipo': row.tipo, 'anonima': row.anonima})
    return row


@transaction.atomic
def create_ombudsman_response(actor, manifestation_id, *, response, publish=False):
    org = _require(actor, 'ouvidoria.respond', m.PHASE_E_MODULES['ouvidoria'])
    row = scoped_queryset(actor, m.ManifestacaoOuvidoriaMunicipal.objects.select_for_update()).filter(organization=org, pk=manifestation_id).first()
    if row is None:
        raise ValidationError('Manifestação inexistente ou fora do escopo.')
    if row.status in {m.ManifestacaoOuvidoriaMunicipal.Status.ENCERRADA, m.ManifestacaoOuvidoriaMunicipal.Status.RESPONDIDA}:
        raise ValidationError('A manifestação já foi respondida ou encerrada.')
    answer = _save_immutable(m.RespostaOuvidoriaMunicipal,
        organization=org, manifestacao=row, resposta=response,
        publicar_no_acompanhamento=publish, respondida_por=actor,
    )
    old = row.status
    row.status = m.ManifestacaoOuvidoriaMunicipal.Status.RESPONDIDA
    row.encerrado_em = timezone.now()
    row.save(update_fields=['status', 'encerrado_em'])
    _save_immutable(m.EventoOuvidoriaMunicipal,
        organization=org, manifestacao=row, evento='RESPOSTA_REGISTRADA',
        status_anterior=old, status_novo=row.status, registrado_por=actor,
    )
    _audit(actor, 'OUVIDORIA_RESPONDIDA', row, changes={'resposta': answer.pk, 'publica_no_acompanhamento': publish})
    return answer


@transaction.atomic
def submit_esic(*, organization, data, tracking_secret):
    if not public_module_available(organization, m.PHASE_E_MODULES['esic']):
        raise PermissionDenied('O canal público E-SIC não está habilitado para esta Prefeitura.')
    sequence = _issue_public_number(organization, m.ContadorExpedienteMunicipal.Familia.ESIC)
    from datetime import timedelta
    row = m.PedidoEsicMunicipal(
        organization=organization, protocolo=sequence,
        prazo_em=timezone.localdate() + timedelta(days=20),
        chave_acompanhamento_digest=hashlib.sha256(tracking_secret.encode()).hexdigest(),
        **data,
    )
    row.save()
    _audit(None, 'ESIC_PEDIDO_RECEBIDO', row, changes={'protocolo': row.protocolo})
    return row


@transaction.atomic
def answer_esic(actor, request_id, *, text, document_key=''):
    org = _require(actor, 'esic.respond', m.PHASE_E_MODULES['esic'])
    row = scoped_queryset(actor, m.PedidoEsicMunicipal.objects.select_for_update()).filter(organization=org, pk=request_id).first()
    if row is None:
        raise ValidationError('Pedido inexistente ou fora do escopo.')
    if row.status in {m.PedidoEsicMunicipal.Status.RESPONDIDO, m.PedidoEsicMunicipal.Status.ENCERRADO}:
        raise ValidationError('Este pedido já foi respondido ou encerrado.')
    answer = _save_immutable(m.RespostaEsicMunicipal,
        organization=org, pedido=row, resposta=text, documento_key=document_key,
        respondida_por=actor,
    )
    before = row.status
    row.status = m.PedidoEsicMunicipal.Status.RESPONDIDO
    row.save(update_fields=['status'])
    _save_immutable(m.EventoEsicMunicipal,
        organization=org, pedido=row, evento='RESPOSTA_REGISTRADA',
        status_anterior=before, status_novo=row.status, registrado_por=actor,
    )
    _audit(actor, 'ESIC_PEDIDO_RESPONDIDO', row, changes={'resposta': answer.pk})
    return answer


@transaction.atomic
def file_esic_appeal(*, organization, request_id, text, tracking_secret):
    if not public_module_available(organization, m.PHASE_E_MODULES['esic']):
        raise PermissionDenied('O acompanhamento público E-SIC não está habilitado.')
    digest = hashlib.sha256(tracking_secret.encode()).hexdigest()
    row = m.PedidoEsicMunicipal.objects.select_for_update().filter(
        organization=organization, public_id=request_id,
        chave_acompanhamento_digest=digest,
    ).first()
    if row is None:
        raise ValidationError('Pedido ou chave de acompanhamento inválidos.')
    if row.status != m.PedidoEsicMunicipal.Status.RESPONDIDO:
        raise ValidationError('O recurso somente pode ser registrado após uma resposta.')
    appeal = m.RecursoEsicMunicipal(organization=organization, pedido=row, fundamento=text)
    appeal.save()
    old = row.status
    row.status = m.PedidoEsicMunicipal.Status.EM_RECURSO
    row.save(update_fields=['status'])
    _save_immutable(m.EventoEsicMunicipal,
        organization=organization, pedido=row, recurso=appeal,
        evento='RECURSO_APRESENTADO', status_anterior=old,
        status_novo=row.status,
    )
    _audit(None, 'ESIC_RECURSO_APRESENTADO', appeal, changes={'pedido': row.pk})
    return appeal


@transaction.atomic
def decide_esic_appeal(actor, appeal_id, *, status, response):
    org = _require(actor, 'esic.appeal', m.PHASE_E_MODULES['esic'])
    appeal = scoped_queryset(actor, m.RecursoEsicMunicipal.objects.select_for_update()).filter(organization=org, pk=appeal_id).first()
    if appeal is None:
        raise ValidationError('Recurso inexistente ou fora do escopo.')
    if appeal.status != m.RecursoEsicMunicipal.Status.RECEBIDO:
        raise ValidationError('Este recurso já foi decidido.')
    appeal.status = status
    appeal.resposta = response
    appeal.decidido_por = actor
    appeal.decidido_em = timezone.now()
    appeal.save()
    appeal.pedido.status = m.PedidoEsicMunicipal.Status.RESPONDIDO
    appeal.pedido.save(update_fields=['status'])
    _save_immutable(m.EventoEsicMunicipal,
        organization=org, pedido=appeal.pedido, recurso=appeal,
        evento='RECURSO_DECIDIDO', status_anterior=m.PedidoEsicMunicipal.Status.EM_RECURSO,
        status_novo=appeal.pedido.status, registrado_por=actor,
    )
    _audit(actor, 'ESIC_RECURSO_DECIDIDO', appeal, changes={'status': status})
    return appeal


@transaction.atomic
def create_transparency_publication(actor, *, data):
    org = _require(actor, 'transparency.manage', m.PHASE_E_MODULES['transparencia'])
    row = m.PublicacaoTransparenciaMunicipal(
        organization=org, criado_por=actor, status=m.PublicacaoTransparenciaMunicipal.Status.RASCUNHO,
        **data,
    )
    row.save()
    _audit(actor, 'TRANSPARENCIA_RASCUNHO_CRIADO', row, changes={'categoria': row.categoria})
    return row


@transaction.atomic
def submit_transparency_for_review(actor, publication_id):
    org = _require(actor, 'transparency.manage', m.PHASE_E_MODULES['transparencia'])
    row = scoped_queryset(actor, m.PublicacaoTransparenciaMunicipal.objects.select_for_update()).filter(organization=org, pk=publication_id).first()
    if row is None:
        raise ValidationError('Publicação inexistente ou fora do escopo.')
    if row.status not in {m.PublicacaoTransparenciaMunicipal.Status.RASCUNHO, m.PublicacaoTransparenciaMunicipal.Status.RETIRADO}:
        raise ValidationError('Somente um rascunho ou item retirado pode ser enviado à revisão.')
    row.status = m.PublicacaoTransparenciaMunicipal.Status.EM_REVISAO
    row.versao += 1
    row.save(update_fields=['status', 'versao'])
    _audit(actor, 'TRANSPARENCIA_ENVIADA_REVISAO', row, changes={'versao': row.versao})
    return row


@transaction.atomic
def publish_transparency(actor, publication_id):
    org = _require(actor, 'transparency.publish', m.PHASE_E_MODULES['transparencia'])
    row = scoped_queryset(actor, m.PublicacaoTransparenciaMunicipal.objects.select_for_update()).filter(organization=org, pk=publication_id).first()
    if row is None:
        raise ValidationError('Publicação inexistente ou fora do escopo.')
    if row.status != m.PublicacaoTransparenciaMunicipal.Status.EM_REVISAO:
        raise ValidationError('Somente conteúdo submetido à revisão pode ser publicado.')
    if row.criado_por_id == actor.pk:
        raise PermissionDenied('A revisão/publicação deve ser realizada por usuário diferente do autor do rascunho.')
    row.full_clean()
    row.status = m.PublicacaoTransparenciaMunicipal.Status.PUBLICADO
    row.revisado_por = actor
    row.publicado_por = actor
    row.revisado_em = row.publicado_em = timezone.now()
    row.save()
    _audit(actor, 'TRANSPARENCIA_PUBLICADA', row, changes={'versao': row.versao, 'categoria': row.categoria})
    return row


@transaction.atomic
def withdraw_transparency(actor, publication_id, *, reason):
    org = _require(actor, 'transparency.publish', m.PHASE_E_MODULES['transparencia'])
    row = scoped_queryset(actor, m.PublicacaoTransparenciaMunicipal.objects.select_for_update()).filter(organization=org, pk=publication_id).first()
    if row is None:
        raise ValidationError('Publicação inexistente ou fora do escopo.')
    if row.status != m.PublicacaoTransparenciaMunicipal.Status.PUBLICADO:
        raise ValidationError('Somente conteúdo publicado pode ser retirado.')
    row.status = m.PublicacaoTransparenciaMunicipal.Status.RETIRADO
    row.versao += 1
    row.save(update_fields=['status', 'versao'])
    _audit(actor, 'TRANSPARENCIA_RETIRADA', row, changes={'versao': row.versao}, reason=reason)
    return row


def create_tracking_secret():
    return secrets.token_urlsafe(32)


def tracking_secret_matches(raw, digest):
    if not raw or not digest:
        return False
    return hmac.compare_digest(hashlib.sha256(raw.encode()).hexdigest(), digest)
