"""Real database services for Health Queue (Totem/SmartTV/Console), Manchester Triage,
ACS Georeferencing Map, Portaria 3.493 BI Panel, and Real-Time Beds & Vital-Signs Monitor.
"""
from datetime import timedelta
from decimal import Decimal
import hashlib
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from platform_core.context import current_organization, tenant_context
from platform_core.models import Organization

from . import phase_g_models as m
from .phase_e_services import public_module_available
from .phase_g_services import _audit, _fernet, _health_hierarchy
from .public_services import has_municipal_permission, require_municipal_permission, scoped_queryset

def monitor_token_digest(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


MANCHESTER_WEIGHTS = {
    'VERMELHO': 1,
    'LARANJA': 2,
    'AMARELO': 3,
    'VERDE': 4,
    'AZUL': 5,
}

MANCHESTER_META = {
    'VERMELHO': {'label': 'Emergência (0 min)', 'css': 'red', 'sla': 0},
    'LARANJA': {'label': 'Muito Urgente (10 min)', 'css': 'orange', 'sla': 10},
    'AMARELO': {'label': 'Urgente (60 min)', 'css': 'yellow', 'sla': 60},
    'VERDE': {'label': 'Pouco Urgente (120 min)', 'css': 'green', 'sla': 120},
    'AZUL': {'label': 'Não Urgente (240 min)', 'css': 'blue', 'sla': 240},
}


def resolve_health_organization(request, slug=None):
    """Only the selected, active tenant with an active user membership is valid."""
    org = current_organization()
    user = getattr(request, 'user', None)
    explicit = (slug or request.GET.get('org') or request.POST.get('org') or '').strip()
    if not user or not user.is_authenticated or org is None:
        raise PermissionDenied('Selecione uma Prefeitura autorizada.')
    if explicit and explicit != org.slug:
        raise PermissionDenied('Prefeitura fora do contexto autorizado.')
    if not user.memberships.filter(organization=org, status='ATIVO', organization__status='ATIVA').exists():
        raise PermissionDenied('Vínculo municipal ativo obrigatório.')
    return org


def resolve_health_unit(org, unit_id=None, user=None):
    if not org:
        raise PermissionDenied('Prefeitura não identificada.')
    qs = m.UnidadeSaudeMunicipal.objects.filter(organization=org, ativo=True).select_related('unidade')
    if user is not None:
        qs = scoped_queryset(user, qs)
    unit = qs.filter(pk=unit_id).first() if unit_id else qs.order_by('id').first()
    if unit is None:
        raise PermissionDenied('Unidade fora do escopo autorizado ou indisponível.')
    return unit


def _authorize(actor, org, capability, record=None):
    from .phase_g_services import _require, _record_allowed
    selected = _require(actor, capability)
    if selected.pk != org.pk:
        raise PermissionDenied('Prefeitura fora do contexto autorizado.')
    if record is not None:
        _record_allowed(actor, capability, record)



def _require_active_clinician(actor, org, unit):
    _authorize(actor, org, 'saude.records.create', unit)
    _authorize(actor, org, 'saude.records.sensitive', unit)
    if not m.ProfissionalSaudeMunicipal.objects.filter(
        organization=org, unidade_saude=unit, ativo=True, servidor__usuario=actor,
    ).exists():
        raise PermissionDenied('Vínculo profissional ativo obrigatório nesta unidade.')


def ensure_default_queue_catalog(org, unit):
    """Bootstrap standard SUS services/rooms for a unit when empty so Totem/Panel work immediately."""
    if not org or not unit:
        return [], []
    with tenant_context(org):
        if not m.ServicoFilaSaude.objects.filter(organization=org, unidade_saude=unit).exists():
            specs = [
                ('CLN', 'Consulta Médica', 'Atendimento Clínico Geral e Retorno', 'stethoscope', 1),
                ('PRE', 'Prioridade Legal', 'Idosos 60+, Gestantes, PCD e Autismo', 'heart-pulse', 2),
                ('TRI', 'Triagem Enfermagem', 'Classificação de Risco e Acolhimento', 'thermometer', 3),
                ('VAC', 'Sala de Vacinação', 'Imunização Rotina e Campanhas PNI', 'syringe', 4),
                ('ODO', 'Odontologia', 'Saúde Bucal e Urgência Odontológica', 'smile', 5),
                ('FAR', 'Farmácia Básica', 'Retirada de Receituário RENAME', 'pill', 6),
            ]
            hier = _health_hierarchy(unit)
            for cod, nome, desc, icon, ordem in specs:
                m.ServicoFilaSaude.objects.create(
                    organization=org, unidade_saude=unit,
                    codigo=cod, nome=nome, descricao=desc, icone=icon, ordem=ordem, **hier,
                )
        if not m.LocalAtendimentoSaude.objects.filter(organization=org, unidade_saude=unit).exists():
            loc_specs = [
                ('TRIAGEM', 1, 'Sala de Triagem 01'),
                ('CONSULTORIO', 1, 'Consultório 01'),
                ('CONSULTORIO', 2, 'Consultório 02'),
                ('SALA_VACINA', 1, 'Sala de Vacinas'),
                ('ODONTOLOGIA', 1, 'Consultório Odontológico'),
                ('FARMACIA', 1, 'Balcão Farmácia Básica'),
            ]
            hier = _health_hierarchy(unit)
            for tipo, num, nome in loc_specs:
                m.LocalAtendimentoSaude.objects.create(
                    organization=org, unidade_saude=unit,
                    tipo=tipo, numero=num, nome=nome, **hier,
                )
    return (
        list(m.ServicoFilaSaude.objects.filter(organization=org, unidade_saude=unit, ativo=True).order_by('ordem', 'nome')),
        list(m.LocalAtendimentoSaude.objects.filter(organization=org, unidade_saude=unit, ativo=True).order_by('tipo', 'numero')),
    )


def _normalize_digits(value):
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


@transaction.atomic
def issue_queue_ticket(org, unit, *, service_code, priority_type='CONVENCIONAL', priority_detail='', cpf_cns='', citizen_id=None, actor=None):
    """Issue an atomic sequential queue ticket in DB synced between Totem, SmartTV and Console."""
    if not org or not unit:
        raise ValidationError('Unidade de saúde não identificada para emissão de senha.')
    if not public_module_available(org, m.HEALTH_MODULE):
        raise PermissionDenied('Módulo Saúde SUS não está ativo nesta Prefeitura.')
    _authorize(actor, org, 'saude.appointments.manage', unit)
    with tenant_context(org):
        # Lock the parent even for the first ticket of the day.
        m.UnidadeSaudeMunicipal.objects.select_for_update().get(pk=unit.pk, organization=org)
        services, _ = ensure_default_queue_catalog(org, unit)
        code = (service_code or 'CLN').upper()[:8]
        # Map legacy frontend prefixes seamlessly
        code_aliases = {'GER': 'CLN'}
        code = code_aliases.get(code, code)
        service = m.ServicoFilaSaude.objects.filter(
            organization=org, unidade_saude=unit, codigo=code, ativo=True,
        ).first() or m.ServicoFilaSaude.objects.filter(
            organization=org, unidade_saude=unit, ativo=True,
        ).order_by('ordem').first()
        if not service:
            raise ValidationError('Nenhum serviço de fila ativo na unidade.')

        today = timezone.localdate()
        last_seq = (
            m.SenhaFilaAtendimento.objects.select_for_update()
            .filter(organization=org, unidade_saude=unit, servico=service, emitida_em__date=today)
            .order_by('-numero_sequencial')
            .values_list('numero_sequencial', flat=True)
            .first() or 0
        )
        seq = last_seq + 1
        is_priority = priority_type == m.SenhaFilaAtendimento.TipoPrioridade.PRIORITARIA or service.codigo == 'PRE'
        prio = m.SenhaFilaAtendimento.TipoPrioridade.PRIORITARIA if is_priority else m.SenhaFilaAtendimento.TipoPrioridade.CONVENCIONAL
        formatted = f'{service.codigo}-P{seq:03d}' if (prio == 'PRIORITARIA' and service.codigo != 'PRE') else f'{service.codigo}-{seq:03d}'

        citizen = None
        if citizen_id:
            citizen = m.CidadaoSaudeMunicipal.objects.filter(organization=org, pk=citizen_id).first()
        elif cpf_cns:
            digits = _normalize_digits(cpf_cns)
            if len(digits) == 11:
                citizen = m.CidadaoSaudeMunicipal.objects.filter(organization=org, cpf=digits).first()
            elif len(digits) == 15:
                citizen = m.CidadaoSaudeMunicipal.objects.filter(organization=org, cns=digits).first()

        hier = _health_hierarchy(unit)
        ticket = m.SenhaFilaAtendimento.objects.create(
            organization=org,
            unidade_saude=unit,
            servico=service,
            cidadao=citizen,
            numero_sequencial=seq,
            tipo_prioridade=prio,
            detalhe_prioridade=(priority_detail or ('Prioritário Legal' if is_priority else ''))[:60],
            codigo_formatado=formatted,
            status=m.SenhaFilaAtendimento.Status.EMITIDA,
            emitida_em=timezone.now(),
            **hier,
        )
        _audit(
            actor if (actor and actor.is_authenticated) else None,
            org, 'SENHA_FILA_EMITIDA', ticket._meta.label_lower, ticket.pk,
            patient_id=citizen.pk if citizen else None,
            details={'codigo': ticket.codigo_formatado, 'servico': service.codigo, 'prioridade': prio},
        )
        return ticket


def _serialize_ticket(ticket, *, include_citizen_details=True):
    tri = getattr(ticket, 'triagem_manchester', None)
    cor = tri.cor if tri else ('AMARELO' if ticket.tipo_prioridade == 'PRIORITARIA' else 'VERDE')
    meta = MANCHESTER_META.get(cor, MANCHESTER_META['VERDE'])
    citizen = ticket.cidadao
    if not include_citizen_details:
        tri = None
    wait_min = int(max(0, ((ticket.chamada_em or timezone.now()) - ticket.emitida_em).total_seconds() // 60))
    return {
        'public_id': str(ticket.public_id),
        'codigo': ticket.codigo_formatado,
        'servico_codigo': ticket.servico.codigo,
        'servico_nome': ticket.servico.nome,
        'status': ticket.status,
        'status_label': ticket.get_status_display(),
        'prioridade': ticket.tipo_prioridade,
        'detalhe_prioridade': ticket.detalhe_prioridade if include_citizen_details else '',
        'paciente_nome': (citizen.nome if citizen and include_citizen_details else 'Atendimento ' + ticket.codigo_formatado),
        'paciente_id': citizen.pk if (citizen and include_citizen_details) else None,
        'paciente_cns': citizen.cns if (citizen and include_citizen_details) else '',
        'local': ticket.local_chamado.nome if ticket.local_chamado else '',
        'local_id': ticket.local_chamado_id,
        'profissional': ticket.profissional.servidor.nome if (ticket.profissional and ticket.profissional.servidor) else '',
        'profissional_id': ticket.profissional_id,
        'manchester_cor': cor if include_citizen_details else '',
        'manchester_css': meta['css'],
        'manchester_label': f'Manchester {cor.title()} · {meta["label"]}' if tri else ('Prioridade Legal (Lei 10.048/00)' if ticket.tipo_prioridade == 'PRIORITARIA' else 'Classificação Padrão'),
        'manchester_fluxograma': tri.fluxograma if tri and include_citizen_details else '',
        'manchester_vitals': {
            'pa': f'{tri.pa_sistolica}/{tri.pa_diastolica}' if (tri and tri.pa_sistolica and tri.pa_diastolica) else '',
            'fc': tri.frequencia_cardiaca if tri else None,
            'spo2': tri.saturacao_o2 if tri else None,
            'temp': str(tri.temperatura) if (tri and tri.temperatura is not None) else '',
            'glicemia': tri.glicemia if tri else None,
        },
        'espera_minutos': wait_min,
        'emitida_em': timezone.localtime(ticket.emitida_em).strftime('%H:%M:%S'),
        'chamada_em': timezone.localtime(ticket.chamada_em).strftime('%H:%M:%S') if ticket.chamada_em else '',
    }


def list_queue_snapshot(org, unit=None, *, include_citizen_details=True):
    """Live DB snapshot for Totem, SmartTV, Medical Console, and Manchester Triage."""
    if not org:
        return {'waiting': [], 'calling_now': None, 'in_service': [], 'history': [], 'services': [], 'locals': [], 'professionals': [], 'summary': {}}
    with tenant_context(org):
        if unit is None:
            unit = resolve_health_unit(org)
        services = list(m.ServicoFilaSaude.objects.filter(organization=org, unidade_saude=unit, ativo=True))
        locals_list = list(m.LocalAtendimentoSaude.objects.filter(organization=org, unidade_saude=unit, ativo=True))
        today = timezone.localdate()
        base_qs = m.SenhaFilaAtendimento.objects.filter(organization=org, emitida_em__date=today)
        if unit:
            base_qs = base_qs.filter(unidade_saude=unit)
        base_qs = base_qs.select_related(
            'servico', 'cidadao', 'local_chamado', 'profissional__servidor', 'triagem_manchester',
        )
        rows = list(base_qs)
        # fallback to recent active tickets if none created today yet
        if not rows:
            fb_qs = m.SenhaFilaAtendimento.objects.filter(organization=org)
            if unit:
                fb_qs = fb_qs.filter(unidade_saude=unit)
            rows = list(
                fb_qs.select_related('servico', 'cidadao', 'local_chamado', 'profissional__servidor', 'triagem_manchester')
                .order_by('-emitida_em')[:25]
            )

        def _sort_key(t):
            tri = getattr(t, 'triagem_manchester', None)
            w = MANCHESTER_WEIGHTS.get(tri.cor, 6) if tri else 6
            prio_w = 0 if t.tipo_prioridade == 'PRIORITARIA' else 1
            return (w, prio_w, t.emitida_em)

        waiting = [_serialize_ticket(t, include_citizen_details=include_citizen_details) for t in sorted([r for r in rows if r.status == 'EMITIDA'], key=_sort_key)]
        called = sorted([r for r in rows if r.status == 'CHAMADA'], key=lambda r: r.chamada_em or r.emitida_em, reverse=True)
        in_service = [_serialize_ticket(t, include_citizen_details=include_citizen_details) for t in sorted([r for r in rows if r.status == 'EM_ATENDIMENTO'], key=lambda r: r.iniciada_em or r.emitida_em, reverse=True)]
        finished = sorted([r for r in rows if r.status in {'ENCERRADA', 'NAO_COMPARECEU'}], key=lambda r: r.encerrada_em or r.chamada_em or r.emitida_em, reverse=True)

        calling_now = _serialize_ticket(called[0], include_citizen_details=include_citizen_details) if called else (
            in_service[0] if in_service else None
        )
        history = [_serialize_ticket(t, include_citizen_details=include_citizen_details) for t in (called[1:] + finished)[:6]]

        wait_samples = [
            max(0, (r.chamada_em - r.emitida_em).total_seconds() / 60.0)
            for r in rows if r.chamada_em and r.emitida_em
        ]
        avg_wait = round(sum(wait_samples) / len(wait_samples), 1) if wait_samples else 0.0

        profs_qs = m.ProfissionalSaudeMunicipal.objects.filter(organization=org, ativo=True)
        if unit:
            profs_qs = profs_qs.filter(unidade_saude=unit)
        profs = list(
            profs_qs.select_related('servidor', 'unidade_saude__unidade')
            .order_by('servidor__nome')[:50]
        )

        return {
            'unit_id': unit.pk if unit else None,
            'unit_name': unit.unidade.nome if (unit and unit.unidade) else '',
            'waiting': waiting,
            'calling_now': calling_now,
            'in_service': in_service,
            'history': history,
            'services': [{'id': s.pk, 'codigo': s.codigo, 'nome': s.nome, 'descricao': s.descricao, 'icone': s.icone} for s in services],
            'locals': [{'id': l.pk, 'tipo': l.tipo, 'nome': l.nome} for l in locals_list],
            'professionals': [{
                'id': p.pk,
                'nome': p.servidor.nome,
                'ocupacao': p.ocupacao,
                'conselho': f'{p.conselho} {p.registro_conselho}'.strip(),
                'especialidade': p.especialidade or p.ocupacao,
                'unidade': p.unidade_saude.unidade.nome if p.unidade_saude and p.unidade_saude.unidade else '',
                'ativo': p.ativo,
            } for p in profs],
            'summary': {
                'aguardando': len(waiting),
                'chamadas': len(called),
                'em_atendimento': len(in_service),
                'encerradas': len(finished),
                'total_dia': len(rows),
                'tempo_medio_espera_min': avg_wait,
            },
        }


@transaction.atomic
def transition_queue_ticket(actor, org, *, action, ticket_public_id=None, local_id=None, professional_id=None, target_service_code=None):
    """Execute Call Next / Call Specific / Recall / Start / Finish / No Show / Transfer directly on PostgreSQL/SQLite."""
    _authorize(actor, org, 'saude.appointments.manage')
    if not public_module_available(org, m.HEALTH_MODULE):
        raise PermissionDenied('Módulo Saúde SUS inativo.')
    with tenant_context(org):
        qs = scoped_queryset(actor, m.SenhaFilaAtendimento.objects.filter(organization=org)).select_for_update(of=('self',))
        if ticket_public_id:
            ticket = qs.filter(public_id=ticket_public_id).first()
            if ticket is None:
                raise ValidationError('Senha não encontrada nesta Prefeitura.')
        else:
            # CALL_NEXT or CALL_NEXT_PRIORITY
            waiting_qs = qs.filter(status=m.SenhaFilaAtendimento.Status.EMITIDA).select_related('triagem_manchester')
            if action == 'CALL_NEXT_PRIORITY':
                waiting_qs = waiting_qs.filter(
                    Q(tipo_prioridade='PRIORITARIA') | Q(triagem_manchester__cor__in=['VERMELHO', 'LARANJA', 'AMARELO'])
                )
            candidates = list(waiting_qs)
            if not candidates:
                raise ValidationError('Não há senhas aguardando nesta fila.')
            candidates.sort(key=lambda t: (
                MANCHESTER_WEIGHTS.get(t.triagem_manchester.cor, 6) if getattr(t, 'triagem_manchester', None) else 6,
                0 if t.tipo_prioridade == 'PRIORITARIA' else 1,
                t.emitida_em,
            ))
            ticket = candidates[0]

        now = timezone.now()
        if local_id:
            local = m.LocalAtendimentoSaude.objects.filter(organization=org, pk=local_id, ativo=True).first()
            if local:
                ticket.local_chamado = local
        elif not ticket.local_chamado_id:
            ticket.local_chamado = m.LocalAtendimentoSaude.objects.filter(
                organization=org, unidade_saude=ticket.unidade_saude, ativo=True,
            ).order_by('id').first()

        if professional_id:
            prof = scoped_queryset(actor, m.ProfissionalSaudeMunicipal.objects.filter(organization=org, pk=professional_id, ativo=True)).first()
            if prof:
                ticket.profissional = prof

        act = (action or '').upper()
        if act in {'CALL', 'CALL_NEXT', 'CALL_NEXT_PRIORITY'}:
            ticket.status = m.SenhaFilaAtendimento.Status.CHAMADA
            ticket.chamada_em = now
        elif act == 'RECALL':
            ticket.status = m.SenhaFilaAtendimento.Status.CHAMADA
            ticket.chamada_em = now
        elif act == 'START':
            ticket.status = m.SenhaFilaAtendimento.Status.EM_ATENDIMENTO
            ticket.iniciada_em = now
            if not ticket.chamada_em:
                ticket.chamada_em = now
        elif act == 'FINISH':
            ticket.status = m.SenhaFilaAtendimento.Status.ENCERRADA
            ticket.encerrada_em = now
        elif act == 'NO_SHOW':
            ticket.status = m.SenhaFilaAtendimento.Status.NAO_COMPARECEU
            ticket.encerrada_em = now
        elif act == 'TRANSFER':
            if not target_service_code:
                raise ValidationError('Informe o serviço de destino para encaminhamento.')
            dest = m.ServicoFilaSaude.objects.filter(
                organization=org, unidade_saude=ticket.unidade_saude, codigo=target_service_code.upper(), ativo=True,
            ).first()
            if dest is None:
                raise ValidationError('Setor/serviço de destino não encontrado.')
            ticket.servico = dest
            ticket.status = m.SenhaFilaAtendimento.Status.EMITIDA
            ticket.local_chamado = None
        else:
            raise ValidationError('Ação de fila inválida.')

        ticket.save()
        _audit(
            actor if (actor and actor.is_authenticated) else None,
            org, f'FILA_{act}', ticket._meta.label_lower, ticket.pk,
            patient_id=ticket.cidadao_id,
            details={'codigo': ticket.codigo_formatado, 'status': ticket.status},
        )
        return ticket


@transaction.atomic
def save_manchester_triage(actor, org, *, ticket_public_id=None, citizen_id=None, nurse_id=None, flowchart, discriminator, color, vitals=None, complaint=''):
    if not public_module_available(org, m.HEALTH_MODULE):
        raise PermissionDenied('Módulo Saúde SUS inativo.')
    _authorize(actor, org, 'saude.records.create')
    _authorize(actor, org, 'saude.records.sensitive')
    vitals = vitals or {}
    cor_norm = (color or 'VERDE').upper()
    if cor_norm not in MANCHESTER_META:
        cor_norm = 'VERDE'
    sla = MANCHESTER_META[cor_norm]['sla']
    with tenant_context(org):
        ticket = None
        if ticket_public_id:
            ticket = m.SenhaFilaAtendimento.objects.filter(organization=org, public_id=ticket_public_id).first()
        citizen = None
        if citizen_id:
            citizen = m.CidadaoSaudeMunicipal.objects.filter(organization=org, pk=citizen_id).first()
        if citizen_id and citizen is None:
            raise ValidationError('Cidadão inválido nesta Prefeitura.')
        if ticket_public_id and ticket is None:
            raise ValidationError('Senha inválida nesta Prefeitura.')
        if citizen is None and ticket and ticket.cidadao:
            citizen = ticket.cidadao
        if citizen is None:
            raise ValidationError('Nenhum cidadão válido encontrado para registrar triagem Manchester.')

        nurses = m.ProfissionalSaudeMunicipal.objects.filter(organization=org, ativo=True, servidor__usuario=actor)
        nurse = nurses.filter(pk=nurse_id).first() if nurse_id else nurses.first()
        if nurse is None:
            raise ValidationError('Cadastre ao menos um profissional de enfermagem/saúde na unidade.')

        _authorize(actor, org, 'saude.records.create', citizen)
        if ticket:
            _authorize(actor, org, 'saude.records.create', ticket)
        unit = ticket.unidade_saude if ticket else nurse.unidade_saude
        if nurse.unidade_saude_id != unit.pk:
            raise ValidationError('Profissional não vinculado à unidade da triagem.')
        hier = _health_hierarchy(unit)

        def _int_or_none(key):
            val = vitals.get(key)
            if val in (None, ''):
                return None
            return int(val)

        def _dec_or_none(key):
            val = vitals.get(key)
            if val in (None, ''):
                return None
            return Decimal(str(val).replace(',', '.'))

        defaults = {
            'organization': org,
            'cidadao': citizen,
            'enfermeiro': nurse,
            'fluxograma': (flowchart or 'Acolhimento com Classificação de Risco')[:120],
            'discriminador': (discriminator or 'Avaliação inicial de enfermagem')[:140],
            'cor': cor_norm,
            'tempo_alvo_minutos': sla,
            'pa_sistolica': _int_or_none('pa_sistolica'),
            'pa_diastolica': _int_or_none('pa_diastolica'),
            'frequencia_cardiaca': _int_or_none('frequencia_cardiaca'),
            'frequencia_respiratoria': _int_or_none('frequencia_respiratoria'),
            'temperatura': _dec_or_none('temperatura'),
            'glicemia': _int_or_none('glicemia'),
            'saturacao_o2': _int_or_none('saturacao_o2'),
            'peso_kg': _dec_or_none('peso_kg'),
            'altura_cm': _int_or_none('altura_cm'),
            'queixa_principal': '',
            'queixa_principal_cifrada': _fernet().encrypt(str(complaint).encode('utf-8')) if complaint else b'',
            'avaliada_em': timezone.now(),
            **hier,
        }
        if ticket:
            row, _ = m.ClassificacaoManchester.objects.update_or_create(
                organization=org, senha=ticket, defaults=defaults,
            )
            # Re-queue prioritized to Médico after triage
            consult_srv = m.ServicoFilaSaude.objects.filter(organization=org, unidade_saude=unit, codigo='CLN', ativo=True).first()
            if consult_srv and ticket.servico.codigo == 'TRI':
                ticket.servico = consult_srv
            if cor_norm in {'VERMELHO', 'LARANJA', 'AMARELO'}:
                ticket.tipo_prioridade = m.SenhaFilaAtendimento.TipoPrioridade.PRIORITARIA
            ticket.status = m.SenhaFilaAtendimento.Status.EMITIDA
            ticket.save()
        else:
            row = m.ClassificacaoManchester.objects.create(**defaults)

        _audit(
            actor if (actor and actor.is_authenticated) else None,
            org, 'TRIAGEM_MANCHESTER_SALVA', row._meta.label_lower, row.pk,
            patient_id=citizen.pk, details={'cor': cor_norm, 'sla_min': sla},
        )
        return row


# ---------------------------------------------------------------------------
# MAPA ACS REAL (GEORREFERENCIAMENTO TERRITORIAL + VISITAS)
# ---------------------------------------------------------------------------

def _default_city_center(org):
    # Deterministic coordinates by known UF/municipality or neutral Brazilian center fallback
    cities = {
        'brejo grande': (-10.4275, -36.4720),
        'poco redondo': (-9.8053, -37.6847),
        'poço redondo': (-9.8053, -37.6847),
        'nova canaa': (-14.7942, -40.1536),
        'nova canaã': (-14.7942, -40.1536),
        'aracaju': (-10.9472, -37.0731),
    }
    city_key = (getattr(org, 'cidade', '') or '').strip().lower()
    return cities.get(city_key, (-10.9167, -37.0500))


def _coord_offset(seed_str, base_lat, base_lng, scale=0.0045):
    digest = hashlib.md5(seed_str.encode('utf-8')).hexdigest()
    dlat = (int(digest[:4], 16) / 65535.0 - 0.5) * scale
    dlng = (int(digest[4:8], 16) / 65535.0 - 0.5) * scale
    return round(base_lat + dlat, 6), round(base_lng + dlng, 6)


def get_acs_map_snapshot(actor, org, *, microarea='', condition=''):
    """Return real database families, visits, ACS agents and epidemiological risk heat circles."""
    _authorize(actor, org, 'saude.field.view')
    _authorize(actor, org, 'saude.records.sensitive')
    if not org:
        return {'center': [-10.9167, -37.05], 'houses': [], 'agents': [], 'metrics': {}, 'microareas': []}
    with tenant_context(org):
        base_lat, base_lng = _default_city_center(org)
        fam_qs = scoped_queryset(actor, m.FamiliaTerritorialSaude.objects.filter(organization=org, ativo=True)).select_related('equipe', 'unidade_saude__unidade')
        if microarea:
            fam_qs = fam_qs.filter(microarea=microarea)

        families = list(fam_qs.order_by('codigo')[:120])
        links = {
            v.familia_id: v.cidadao
            for v in m.VinculoFamiliaSaude.objects.filter(organization=org, familia__in=families, referencia=True).select_related('cidadao')
        }
        all_links = m.VinculoFamiliaSaude.objects.filter(organization=org, familia__in=families).select_related('cidadao')
        family_members = {}
        for lnk in all_links:
            family_members.setdefault(lnk.familia_id, []).append(lnk.cidadao)

        recent_visits = list(
            scoped_queryset(actor, m.VisitaTerritorialSaude.objects.filter(organization=org))
            .select_related('agente__servidor', 'familia', 'cidadao')
            .order_by('-realizada_em')[:250]
        )
        visits_by_fam = {}
        for v in recent_visits:
            if v.familia_id and v.familia_id not in visits_by_fam:
                visits_by_fam[v.familia_id] = v

        houses = []
        risk_counts = {'BAIXO': 0, 'MEDIO': 0, 'ALTO': 0, 'CRITICO': 0}
        condition_counts = {}
        microareas = set()

        for fam in families:
            if fam.microarea:
                microareas.add(fam.microarea)
            lat = float(fam.latitude) if fam.latitude is not None else None
            lng = float(fam.longitude) if fam.longitude is not None else None
            if lat is None or lng is None:
                lat, lng = None, None
            last_visit = visits_by_fam.get(fam.pk)
            ref_citizen = links.get(fam.pk) or (family_members.get(fam.pk, [None])[0])
            resp_name = fam.responsavel_nome or (ref_citizen.nome if ref_citizen else f'Família {fam.codigo}')
            cns = ref_citizen.cns if ref_citizen else ''
            risk = fam.risco_territorial or 'BAIXO'
            if last_visit and last_visit.alerta_epidemiologico:
                risk = 'CRITICO' if 'dengue' in last_visit.alerta_epidemiologico.lower() else ('ALTO' if risk == 'BAIXO' else risk)
            conds = list(fam.condicoes_sentinelas or [])
            if last_visit and last_visit.condicao_acompanhada and last_visit.condicao_acompanhada not in conds:
                conds.append(last_visit.condicao_acompanhada)
            if condition and condition.upper() not in [c.upper() for c in conds]:
                continue
            risk_counts[risk] = risk_counts.get(risk, 0) + 1
            for c in conds:
                condition_counts[c] = condition_counts.get(c, 0) + 1
            houses.append({
                'id': fam.pk,
                'codigo': fam.codigo,
                'coords': [lat, lng],
                'name': f'{fam.codigo} · {fam.referencia or fam.endereco[:36]}',
                'endereco': fam.endereco,
                'microarea': fam.microarea or 'MA-01',
                'resp': resp_name,
                'cns': cns or 'Não informado',
                'risk': 'red' if risk == 'CRITICO' else ('yellow' if risk in {'ALTO', 'MEDIO'} else 'green'),
                'risk_level': risk,
                'conditions': conds,
                'alert': (last_visit.alerta_epidemiologico if last_visit and last_visit.alerta_epidemiologico else f'Acompanhamento territorial {fam.codigo} ({", ".join(conds) or "Rotina APS"})'),
                'pa': f'{last_visit.pa_sistolica}/{last_visit.pa_diastolica}' if (last_visit and last_visit.pa_sistolica and last_visit.pa_diastolica) else '--/--',
                'glicemia': str(last_visit.glicemia) if (last_visit and last_visit.glicemia) else '--',
                'peso': str(last_visit.peso_kg) if (last_visit and last_visit.peso_kg is not None) else '--',
                'ultima_visita': timezone.localtime(last_visit.realizada_em).strftime('%d/%m/%Y %H:%M') if last_visit else 'Sem visita registrada',
                'agente': last_visit.agente.servidor.nome if (last_visit and last_visit.agente and last_visit.agente.servidor) else (fam.equipe.nome if fam.equipe else 'Equipe ESF'),
            })

        agents_qs = scoped_queryset(
            actor,
            m.ProfissionalSaudeMunicipal.objects.filter(organization=org, ativo=True).filter(
                Q(ocupacao__icontains='ACS') | Q(ocupacao__icontains='Agente') | Q(equipe__tipo__in=['ACS', 'ACE', 'ESF'])
            ),
        ).select_related('servidor', 'equipe', 'unidade_saude__unidade').distinct()[:40]

        agents = []
        for prof in agents_qs:
            p_visits = [v for v in recent_visits if v.agente_id == prof.pk]
            latest_v = p_visits[0] if p_visits else None
            if latest_v and latest_v.latitude is not None and latest_v.longitude is not None:
                alat, alng = float(latest_v.latitude), float(latest_v.longitude)
            else:
                alat, alng = None, None
            agents.append({
                'id': prof.pk,
                'nome': prof.servidor.nome,
                'ocupacao': prof.ocupacao,
                'microarea': prof.equipe.microarea if prof.equipe else 'Geral',
                'visitas_realizadas': len(p_visits),
                'coords': [alat, alng],
                'ultima_atividade': timezone.localtime(latest_v.realizada_em).strftime('%d/%m %H:%M') if latest_v else 'Pronto p/ campo',
            })

        _audit(actor, org, 'MAPA_ACS_CONSULTA', 'rh_municipal.visitaterritorialsaude', None, details={'familias': len(houses), 'visitas': len(recent_visits)})
        return {
            'center': [base_lat, base_lng],
            'houses': houses,
            'agents': agents,
            'microareas': sorted(microareas),
            'metrics': {
                'total_familias': len(families),
                'familias_exibidas': len(houses),
                'visitas_registradas': len(recent_visits),
                'agentes_ativos': len(agents),
                'alertas_criticos': risk_counts.get('CRITICO', 0) + risk_counts.get('ALTO', 0),
                'condicoes': condition_counts,
            },
        }


@transaction.atomic
def register_acs_map_visit(actor, org, *, family_id, agent_id, condition='', alert_text='', pa_sys=None, pa_dia=None, glicemia=None, peso=None, lat=None, lng=None, notes=''):
    if not public_module_available(org, m.HEALTH_MODULE):
        raise PermissionDenied('Módulo Saúde SUS inativo.')
    _authorize(actor, org, 'saude.field.manage')
    with tenant_context(org):
        fam = m.FamiliaTerritorialSaude.objects.filter(organization=org, pk=family_id).first()
        if fam is None:
            raise ValidationError('Família territorial não encontrada nesta Prefeitura.')
        agent = m.ProfissionalSaudeMunicipal.objects.filter(organization=org, pk=agent_id, ativo=True).first()
        if agent is None:
            raise ValidationError('Agente de saúde não encontrado nesta Prefeitura.')
        ref_link = m.VinculoFamiliaSaude.objects.filter(organization=org, familia=fam).select_related('cidadao').first()
        _authorize(actor, org, 'saude.field.manage', fam)
        _authorize(actor, org, 'saude.field.manage', agent)
        if agent.servidor.usuario_id != actor.pk:
            raise PermissionDenied('Agente deve corresponder ao usuário autenticado.')
        hier = _health_hierarchy(fam.unidade_saude)
        cipher = _fernet().encrypt(notes.encode('utf-8')) if notes else b''
        visit = m.VisitaTerritorialSaude.objects.create(
            organization=org,
            tipo=m.VisitaTerritorialSaude.Tipo.ACS,
            familia=fam,
            cidadao=ref_link.cidadao if ref_link else None,
            agente=agent,
            realizada_em=timezone.now(),
            observacoes_cifradas=cipher,
            latitude=Decimal(str(lat)) if lat not in (None, '') else fam.latitude,
            longitude=Decimal(str(lng)) if lng not in (None, '') else fam.longitude,
            pa_sistolica=int(pa_sys) if pa_sys not in (None, '') else None,
            pa_diastolica=int(pa_dia) if pa_dia not in (None, '') else None,
            glicemia=int(glicemia) if glicemia not in (None, '') else None,
            peso_kg=Decimal(str(peso).replace(',', '.')) if peso not in (None, '') else None,
            condicao_acompanhada=(condition or '').upper()[:32],
            alerta_epidemiologico=(alert_text or '')[:180],
            status='REALIZADA',
            **hier,
        )
        if condition and condition.upper() not in [c.upper() for c in (fam.condicoes_sentinelas or [])]:
            fam.condicoes_sentinelas = list(fam.condicoes_sentinelas or []) + [condition.upper()]
        if alert_text:
            fam.risco_territorial = 'CRITICO' if 'dengue' in alert_text.lower() else 'ALTO'
        if lat not in (None, '') and lng not in (None, ''):
            fam.latitude = Decimal(str(lat))
            fam.longitude = Decimal(str(lng))
        fam.save()
        _audit(actor, org, 'VISITA_ACS_MAPA_REGISTRADA', visit._meta.label_lower, visit.pk, patient_id=visit.cidadao_id, details={'familia': fam.codigo, 'condicao': visit.condicao_acompanhada})
        return visit


# ---------------------------------------------------------------------------
# PAINEL BI SAÚDE — PORTARIA GM/MS 3.493/2024 & PORTARIA 161
# ---------------------------------------------------------------------------

def compute_health_bi_panel(actor, org, *, unit_id=None, days=30):
    """Aggregate Portaria 3.493 / APS indicators & operational BI directly from DB."""
    if not public_module_available(org, m.HEALTH_MODULE):
        raise PermissionDenied('Módulo Saúde SUS inativo.')
    _authorize(actor, org, 'saude.dashboard.view')
    with tenant_context(org):
        since = timezone.now() - timedelta(days=int(days))
        unit_filter = {'unidade_saude_id': unit_id} if unit_id else {}

        units_qs = scoped_queryset(actor, m.UnidadeSaudeMunicipal.objects.filter(organization=org, ativo=True))
        teams_qs = scoped_queryset(actor, m.EquipeSaudeMunicipal.objects.filter(organization=org, ativo=True, **unit_filter))
        citizens_qs = scoped_queryset(actor, m.CidadaoSaudeMunicipal.objects.filter(organization=org, situacao='ATIVO'))
        families_qs = scoped_queryset(actor, m.FamiliaTerritorialSaude.objects.filter(organization=org, ativo=True, **unit_filter))
        visits_qs = scoped_queryset(actor, m.VisitaTerritorialSaude.objects.filter(organization=org, realizada_em__gte=since))
        appts_qs = scoped_queryset(actor, m.AtendimentoSaudeMunicipal.objects.filter(organization=org, **unit_filter))
        tickets_qs = scoped_queryset(actor, m.SenhaFilaAtendimento.objects.filter(organization=org, emitida_em__gte=since, **unit_filter))
        triage_qs = scoped_queryset(actor, m.ClassificacaoManchester.objects.filter(organization=org, avaliada_em__gte=since))
        disp_qs = scoped_queryset(actor, m.DispensacaoMedicamentoMunicipal.objects.filter(organization=org, dispensada_em__gte=since, **unit_filter))
        lots_qs = scoped_queryset(actor, m.LoteMedicamentoMunicipal.objects.filter(organization=org, **unit_filter)).select_related('medicamento')
        reg_qs = scoped_queryset(actor, m.SolicitacaoRegulacaoSaude.objects.filter(organization=org))
        prod_qs = scoped_queryset(actor, m.RegistroProducaoSUS.objects.filter(organization=org, **unit_filter))
        beds_qs = scoped_queryset(actor, m.LeitoSaudeMunicipal.objects.filter(organization=org, ativo=True, **unit_filter))
        alerts_qs = scoped_queryset(actor, m.EventoAlertaMonitorSaude.objects.filter(organization=org, reconhecido=False))

        total_families = families_qs.count()
        visited_families = visits_qs.exclude(familia_id__isnull=True).values('familia_id').distinct().count()
        cobertura_visita_pct = round((visited_families / total_families) * 100, 1) if total_families else 0.0

        gestantes_fams = (
            sum(
                1 for conds in families_qs.values_list('condicoes_sentinelas', flat=True)
                if conds and any(str(c).upper() == 'GESTANTE' for c in conds)
            )
            + visits_qs.filter(condicao_acompanhada__iexact='GESTANTE').values('familia_id').distinct().count()
        )
        has_dm_count = (
            visits_qs.filter(Q(condicao_acompanhada__in=['HIPERTENSAO', 'DIABETES']) | Q(pa_sistolica__isnull=False) | Q(glicemia__isnull=False)).count()
            + triage_qs.filter(Q(pa_sistolica__gte=140) | Q(glicemia__gte=126)).count()
        )
        odont_count = tickets_qs.filter(servico__codigo='ODO').count() + appts_qs.filter(codigo_procedimento__startswith='0307').count()

        manchester_dist = {row['cor']: row['total'] for row in triage_qs.values('cor').annotate(total=Count('id'))}
        ticket_times = list(tickets_qs.filter(chamada_em__isnull=False).values_list('emitida_em', 'chamada_em'))
        wait_mins = [max(0.0, (c - e).total_seconds() / 60.0) for e, c in ticket_times]
        avg_wait = round(sum(wait_mins) / len(wait_mins), 1) if wait_mins else 0.0

        expiring_limit = timezone.localdate() + timedelta(days=60)
        low_stock = sum(1 for lot in lots_qs if lot.saldo <= lot.medicamento.estoque_minimo)
        expiring_lots = sum(1 for lot in lots_qs if lot.validade <= expiring_limit and lot.saldo > 0)

        beds_total = beds_qs.count()
        beds_occupied = beds_qs.filter(status='OCUPADO').count()
        beds_available = beds_qs.filter(status='DISPONIVEL').count()
        occupancy_pct = round((beds_occupied / beds_total) * 100, 1) if beds_total else 0.0

        _audit(actor, org, 'BI_SAUDE_CONSULTA', 'rh_municipal.registroproducaosus', None, details={'days': int(days)})

        return {
            'periodo_dias': int(days),
            'cards': {
                'unidades_ativas': units_qs.count(),
                'equipes_ativas': teams_qs.count(),
                'cidadaos_ativos': citizens_qs.count(),
                'familias_cadastradas': total_families,
                'visitas_acs_periodo': visits_qs.count(),
                'cobertura_territorial_pct': min(100.0, cobertura_visita_pct),
                'atendimentos_concluidos': appts_qs.filter(status='CONCLUIDO').count(),
                'senhas_emitidas_periodo': tickets_qs.count(),
                'tempo_medio_espera_min': avg_wait,
                'dispensacoes_periodo': disp_qs.count(),
                'lotes_estoque_critico': low_stock,
                'lotes_vencendo_60d': expiring_lots,
                'regulacao_pendente': reg_qs.filter(status='PENDENTE').count(),
                'producao_sus_registros': prod_qs.count(),
                'leitos_total': beds_total,
                'leitos_ocupados': beds_occupied,
                'leitos_disponiveis': beds_available,
                'ocupacao_hospitalar_pct': occupancy_pct,
                'alertas_monitor_abertos': alerts_qs.count(),
            },
            'portaria_3493': [
                {
                    'codigo': 'C1',
                    'indicador': 'Mais Acesso à APS & Acolhimento em Fila',
                    'meta': '>= 75%',
                    'valor_atual': f'{min(100.0, cobertura_visita_pct)}%',
                    'detalhe': f'{visited_families}/{total_families} famílias visitadas no ciclo de {days} dias',
                    'status': 'META' if cobertura_visita_pct >= 75 else ('ATENCAO' if cobertura_visita_pct >= 40 else 'CRITICO'),
                },
                {
                    'codigo': 'C2',
                    'indicador': 'Cuidado no Desenvolvimento Infantil / Territorial',
                    'meta': '100% microáreas cobertas',
                    'valor_atual': f'{families_qs.exclude(microarea="").values("microarea").distinct().count()} microáreas',
                    'detalhe': f'{visits_qs.count()} visitas ACS/ACE registradas no banco',
                    'status': 'META' if visits_qs.exists() else 'ATENCAO',
                },
                {
                    'codigo': 'C3',
                    'indicador': 'Gestação e Puerpério (Pré-natal APS)',
                    'meta': 'Busca ativa contínua',
                    'valor_atual': f'{gestantes_fams} acomp.',
                    'detalhe': 'Famílias e visitas com condição sentinela GESTANTE',
                    'status': 'META' if gestantes_fams > 0 else 'ATENCAO',
                },
                {
                    'codigo': 'C4',
                    'indicador': 'Cuidado da Pessoa com Hipertensão e Diabetes (HAS/DM2)',
                    'meta': 'Aferição PA & Glicemia >= 60%',
                    'valor_atual': f'{has_dm_count} aferições',
                    'detalhe': 'Visitas ACS e triagens Manchester com registro PA/Glicemia',
                    'status': 'META' if has_dm_count > 0 else 'ATENCAO',
                },
                {
                    'codigo': 'C5',
                    'indicador': 'Saúde Bucal na Atenção Primária',
                    'meta': 'Fluxo ativo UBS',
                    'valor_atual': f'{odont_count} atend.',
                    'detalhe': 'Senhas ODO e atendimentos odontológicos registrados',
                    'status': 'META' if odont_count > 0 else 'ATENCAO',
                },
                {
                    'codigo': 'C6',
                    'indicador': 'Tempo Médio de Espera e Classificação Manchester',
                    'meta': '<= 45 min médio',
                    'valor_atual': f'{avg_wait} min',
                    'detalhe': f'{triage_qs.count()} classificações Manchester no período',
                    'status': 'META' if avg_wait <= 45 else 'ATENCAO',
                },
            ],
            'manchester_dist': {
                'VERMELHO': manchester_dist.get('VERMELHO', 0),
                'LARANJA': manchester_dist.get('LARANJA', 0),
                'AMARELO': manchester_dist.get('AMARELO', 0),
                'VERDE': manchester_dist.get('VERDE', 0),
                'AZUL': manchester_dist.get('AZUL', 0),
            },
            'unidades': list(units_qs.values('id', 'unidade__nome', 'tipo', 'cnes')),
        }


# ---------------------------------------------------------------------------
# LEITOS & MONITOR MULTIPARAMÉTRICO EM TEMPO REAL
# ---------------------------------------------------------------------------

def evaluate_vital_alarms(*, fc_bpm=None, spo2_percent=None, pa_sistolica=None, pa_diastolica=None, fr_irpm=None, temperatura_c=None):
    """Clinical alarm engine (SpO2, HR, SBP, Temp, RR) returning (level, alerts_list)."""
    alerts = []
    level = 'NORMAL'

    def _escalate(sev, param, val, msg):
        nonlocal level
        alerts.append({'severidade': sev, 'parametro': param, 'valor': str(val), 'mensagem': msg})
        if sev == 'CRITICO':
            level = 'CRITICO'
        elif level != 'CRITICO':
            level = 'ATENCAO'

    if spo2_percent is not None:
        if spo2_percent < 90:
            _escalate('CRITICO', 'SpO2', f'{spo2_percent}%', f'Dessaturação grave (SpO2 {spo2_percent}% < 90%)')
        elif spo2_percent < 94:
            _escalate('ATENCAO', 'SpO2', f'{spo2_percent}%', f'SpO2 limítrofe ({spo2_percent}% < 94%)')

    if fc_bpm is not None:
        if fc_bpm < 45 or fc_bpm > 135:
            _escalate('CRITICO', 'FC', f'{fc_bpm} bpm', f'Arritmia / Frequência cardíaca crítica ({fc_bpm} bpm)')
        elif fc_bpm < 55 or fc_bpm > 110:
            _escalate('ATENCAO', 'FC', f'{fc_bpm} bpm', f'Frequência cardíaca fora da faixa ({fc_bpm} bpm)')

    if pa_sistolica is not None:
        if pa_sistolica < 85 or pa_sistolica > 180:
            _escalate('CRITICO', 'PA', f'{pa_sistolica}/{pa_diastolica or "--"} mmHg', f'Crise pressórica ou hipotensão crítica (PAS {pa_sistolica} mmHg)')
        elif pa_sistolica < 95 or pa_sistolica > 150:
            _escalate('ATENCAO', 'PA', f'{pa_sistolica}/{pa_diastolica or "--"} mmHg', f'Pressão arterial em alerta ({pa_sistolica}/{pa_diastolica or "--"} mmHg)')

    if temperatura_c is not None:
        t_val = float(temperatura_c)
        if t_val >= 38.5 or t_val <= 35.0:
            _escalate('CRITICO', 'Temp', f'{t_val:.1f} °C', f'Alteração térmica importante ({t_val:.1f} °C)')
        elif t_val >= 37.8:
            _escalate('ATENCAO', 'Temp', f'{t_val:.1f} °C', f'Estado febril ({t_val:.1f} °C)')

    if fr_irpm is not None:
        if fr_irpm < 10 or fr_irpm > 28:
            _escalate('CRITICO', 'FR', f'{fr_irpm} irpm', f'Desconforto respiratório crítico ({fr_irpm} irpm)')

    return level, alerts


@transaction.atomic
def create_ward_bed_and_monitor(actor, org, *, unit_id, ala_codigo, ala_nome, ala_tipo='OBSERVACAO', bed_codigo, bed_tipo='OBSERVACAO', serial_monitor='', modelo_monitor='Monitor Multiparamétrico Wi-Fi 5P', protocolo='HTTP_JSON', endpoint_ip=''):
    if not public_module_available(org, m.HEALTH_MODULE):
        raise PermissionDenied('Módulo Saúde SUS inativo.')
    _authorize(actor, org, 'saude.units.manage')
    with tenant_context(org):
        unit = m.UnidadeSaudeMunicipal.objects.filter(organization=org, pk=unit_id).first()
        if unit is None:
            raise ValidationError('Unidade de saúde não encontrada.')
        _authorize(actor, org, 'saude.units.manage', unit)
        hier = _health_hierarchy(unit)
        ala, _ = m.AlaEnfermariaSaude.objects.get_or_create(
            organization=org, unidade_saude=unit, codigo=ala_codigo.upper()[:24],
            defaults={'nome': ala_nome[:120], 'tipo': ala_tipo, **hier},
        )
        bed, created = m.LeitoSaudeMunicipal.objects.get_or_create(
            organization=org, unidade_saude=unit, codigo=bed_codigo.upper()[:24],
            defaults={'ala': ala, 'tipo_leito': bed_tipo, 'descricao': f'{ala.nome} · {bed_codigo.upper()}', **hier},
        )
        if not created:
            raise ValidationError(f'O leito {bed_codigo.upper()} já existe nesta unidade.')
        monitor = None
        if serial_monitor:
            monitor = m.EquipamentoMonitorSaude.objects.create(
                organization=org, unidade_saude=unit, leito=bed,
                serial_dispositivo=serial_monitor.upper()[:64],
                modelo=modelo_monitor[:100], protocolo=protocolo,
                endpoint_ou_ip=endpoint_ip[:120],
                token_ingestao='',
                **hier,
            )
        _audit(actor, org, 'LEITO_MONITOR_CADASTRADO', bed._meta.label_lower, bed.pk, details={'ala': ala.codigo, 'leito': bed.codigo, 'monitor': serial_monitor})
        return bed, monitor


@transaction.atomic
def admit_patient_to_bed(actor, org, *, bed_public_id, citizen_id, professional_id=None, precaution=''):
    if not public_module_available(org, m.HEALTH_MODULE):
        raise PermissionDenied('Módulo Saúde SUS inativo.')
    _authorize(actor, org, 'saude.appointments.manage')
    with tenant_context(org):
        bed = m.LeitoSaudeMunicipal.objects.select_for_update().filter(organization=org, public_id=bed_public_id).first()
        if bed is None:
            raise ValidationError('Leito não encontrado.')
        _authorize(actor, org, 'saude.appointments.manage', bed)
        if bed.status not in {'DISPONIVEL', 'RESERVADO'}:
            raise ValidationError(f'Leito {bed.codigo} não está disponível (status atual: {bed.get_status_display()}).')
        citizen = m.CidadaoSaudeMunicipal.objects.filter(organization=org, pk=citizen_id, situacao='ATIVO').first()
        if citizen is None:
            raise ValidationError('Paciente não encontrado ou inativo nesta Prefeitura.')
        _authorize(actor, org, 'saude.appointments.manage', citizen)
        prof = m.ProfissionalSaudeMunicipal.objects.filter(organization=org, pk=professional_id).first() if professional_id else None
        bed.status = m.LeitoSaudeMunicipal.Status.OCUPADO
        bed.paciente_atual = citizen
        bed.profissional_responsavel = prof
        bed.admitido_em = timezone.now()
        bed.precaucao_isolamento = (precaution or '')[:60]
        bed.save()
        _audit(actor, org, 'ADMISSAO_LEITO', bed._meta.label_lower, bed.pk, patient_id=citizen.pk, details={'leito': bed.codigo})
        return bed


@transaction.atomic
def transition_bed_status(actor, org, *, bed_public_id, new_status):
    if not public_module_available(org, m.HEALTH_MODULE):
        raise PermissionDenied('Módulo Saúde SUS inativo.')
    _authorize(actor, org, 'saude.appointments.manage')
    allowed = {c[0] for c in m.LeitoSaudeMunicipal.Status.choices}
    if new_status not in allowed:
        raise ValidationError('Status de leito inválido.')
    with tenant_context(org):
        bed = m.LeitoSaudeMunicipal.objects.select_for_update().filter(organization=org, public_id=bed_public_id).first()
        if bed is None:
            raise ValidationError('Leito não encontrado.')
        prev_patient_id = bed.paciente_atual_id
        if new_status in {'DISPONIVEL', 'HIGIENIZACAO', 'MANUTENCAO'}:
            bed.paciente_atual = None
            bed.atendimento_atual = None
            bed.admitido_em = None
        _authorize(actor, org, 'saude.appointments.manage', bed)
        bed.status = new_status
        bed.save()
        _audit(actor, org, 'LEITO_STATUS_ALTERADO', bed._meta.label_lower, bed.pk, patient_id=prev_patient_id, details={'leito': bed.codigo, 'status': new_status})
        return bed


@transaction.atomic
def ingest_monitor_telemetry(org, *, monitor_id_or_serial, fc_bpm=None, spo2_percent=None, pa_sistolica=None, pa_diastolica=None, fr_irpm=None, temperatura_c=None, etco2_mmhg=None, ritmo_ecg='SINUSAL', battery=None, origin='TELEMETRIA_REAL', actor=None):
    """Ingest live vital signs from bedside monitor, compute MAP + clinical alarms, and log alerts."""
    if not public_module_available(org, m.HEALTH_MODULE):
        raise PermissionDenied('Módulo Saúde SUS inativo.')
    with tenant_context(org):
        monitor = (
            m.EquipamentoMonitorSaude.objects.select_for_update(of=('self',))
            .filter(organization=org)
            .filter(Q(serial_dispositivo=str(monitor_id_or_serial).upper()) | Q(public_id=monitor_id_or_serial) if len(str(monitor_id_or_serial)) == 36 else Q(serial_dispositivo=str(monitor_id_or_serial).upper()))
            .select_related('leito__unidade_saude__unidade', 'leito__paciente_atual')
            .first()
        )
        if monitor is None or monitor.leito is None:
            raise ValidationError('Monitor multiparamétrico ou leito vinculado não encontrado.')

        if actor is not None:
            _authorize(actor, org, 'saude.records.create', monitor)
            _require_active_clinician(actor, org, monitor.unidade_saude)
        bed = monitor.leito
        if bed.organization_id != org.pk:
            raise PermissionDenied('Leito fora da Prefeitura do equipamento.')
        hier = _health_hierarchy(bed.unidade_saude)
        temp_dec = Decimal(str(temperatura_c).replace(',', '.')) if temperatura_c not in (None, '') else None
        pam = None
        if pa_sistolica is not None and pa_diastolica is not None:
            pam = int(round((int(pa_sistolica) + 2 * int(pa_diastolica)) / 3.0))

        level, alerts = evaluate_vital_alarms(
            fc_bpm=fc_bpm, spo2_percent=spo2_percent,
            pa_sistolica=pa_sistolica, pa_diastolica=pa_diastolica,
            fr_irpm=fr_irpm, temperatura_c=temp_dec,
        )

        telem = m.TelemetriaMonitorLeito.objects.create(
            organization=org,
            equipamento=monitor,
            leito=bed,
            cidadao=bed.paciente_atual,
            coletado_em=timezone.now(),
            fc_bpm=fc_bpm,
            spo2_percent=spo2_percent,
            pa_sistolica=pa_sistolica,
            pa_diastolica=pa_diastolica,
            pam_mmhg=pam,
            fr_irpm=fr_irpm,
            temperatura_c=temp_dec,
            etco2_mmhg=etco2_mmhg,
            ritmo_ecg=(ritmo_ecg or 'SINUSAL')[:40],
            nivel_alerta=level,
            alertas_detectados=alerts,
            origem_leitura=origin[:24],
            **hier,
        )
        monitor.online = True
        monitor.ultimo_heartbeat = timezone.now()
        if battery is not None:
            monitor.bateria_percentual = max(0, min(100, int(battery)))
        monitor.save(update_fields=['online', 'ultimo_heartbeat', 'bateria_percentual'])

        for al in alerts:
            m.EventoAlertaMonitorSaude.objects.create(
                organization=org,
                leito=bed,
                telemetria=telem,
                severidade=al['severidade'],
                parametro=al['parametro'],
                valor_aferido=al['valor'],
                mensagem=al['mensagem'],
                ocorrido_em=timezone.now(),
                **hier,
            )

        _audit(actor, org, 'MONITOR_TELEMETRIA_REGISTRADA', telem._meta.label_lower, telem.pk, patient_id=bed.paciente_atual_id, details={'leito': bed.codigo, 'nivel': level, 'gateway': actor is None})
        return telem


@transaction.atomic
def acknowledge_monitor_alert(actor, org, *, alert_public_id, conduta=''):
    _authorize(actor, org, 'saude.records.create')
    with tenant_context(org):
        alert = m.EventoAlertaMonitorSaude.objects.select_for_update().filter(organization=org, public_id=alert_public_id).first()
        if alert is None:
            raise ValidationError('Alarme clínico não encontrado.')
        _authorize(actor, org, 'saude.records.create', alert)
        _require_active_clinician(actor, org, alert.leito.unidade_saude)
        alert.reconhecido = True
        alert.reconhecido_por = actor
        alert.reconhecido_em = timezone.now()
        alert.conduta = (conduta or 'Avaliação realizada à beira-leito pela equipe assistencial.')[:300]
        alert.save(update_fields=['reconhecido', 'reconhecido_por', 'reconhecido_em', 'conduta'])
        _audit(actor, org, 'ALERTA_MONITOR_RECONHECIDO', alert._meta.label_lower, alert.pk, details={'parametro': alert.parametro, 'leito': alert.leito.codigo})
        return alert


def get_beds_monitor_snapshot(actor, org, unit_id=None):
    """Return live hospital/UPA beds, attached monitors, latest vital signs and active alarms."""
    _authorize(actor, org, 'saude.records.sensitive')
    _authorize(actor, org, 'saude.dashboard.view')
    with tenant_context(org):
        unit = resolve_health_unit(org, unit_id=unit_id, user=actor)
        alas_qs = scoped_queryset(actor, m.AlaEnfermariaSaude.objects.filter(organization=org, ativo=True))
        beds_qs = scoped_queryset(actor, m.LeitoSaudeMunicipal.objects.filter(organization=org, ativo=True)).select_related(
            'ala', 'unidade_saude__unidade', 'paciente_atual', 'profissional_responsavel__servidor', 'monitor',
        )
        if unit:
            alas_qs = alas_qs.filter(unidade_saude=unit)
            beds_qs = beds_qs.filter(unidade_saude=unit)

        _audit(actor, org, 'LEITOS_MONITOR_CONSULTADOS', 'saude.monitor_leitos')
        beds = list(beds_qs.order_by('ala__codigo', 'codigo'))
        latest_by_bed = {}
        for t in m.TelemetriaMonitorLeito.objects.filter(organization=org, leito__in=beds).order_by('leito_id', '-coletado_em', '-pk'):
            if t.leito_id not in latest_by_bed:
                latest_by_bed[t.leito_id] = t

        open_alerts = list(
            scoped_queryset(actor, m.EventoAlertaMonitorSaude.objects.filter(organization=org, reconhecido=False))
            .select_related('leito__ala')
            .order_by('-ocorrido_em')[:25]
        )

        bed_cards = []
        counts = {'DISPONIVEL': 0, 'OCUPADO': 0, 'HIGIENIZACAO': 0, 'MANUTENCAO': 0, 'RESERVADO': 0}
        for b in beds:
            counts[b.status] = counts.get(b.status, 0) + 1
            mon = getattr(b, 'monitor', None)
            telem = latest_by_bed.get(b.pk)
            is_online = bool(mon and mon.online and mon.ultimo_heartbeat and (timezone.now() - mon.ultimo_heartbeat).total_seconds() <= 300)
            bed_cards.append({
                'public_id': str(b.public_id),
                'id': b.pk,
                'codigo': b.codigo,
                'ala': b.ala.nome,
                'ala_codigo': b.ala.codigo,
                'tipo_leito': b.get_tipo_leito_display(),
                'status': b.status,
                'status_label': b.get_status_display(),
                'precaucao': b.precaucao_isolamento,
                'paciente': {
                    'id': b.paciente_atual_id,
                    'nome': b.paciente_atual.nome,
                    'cns': b.paciente_atual.cns,
                } if b.paciente_atual else None,
                'responsavel': b.profissional_responsavel.servidor.nome if (b.profissional_responsavel and b.profissional_responsavel.servidor) else '',
                'admitido_em': timezone.localtime(b.admitido_em).strftime('%d/%m %H:%M') if b.admitido_em else '',
                'monitor': {
                    'serial': mon.serial_dispositivo,
                    'modelo': mon.modelo,
                    'protocolo': mon.get_protocolo_display(),
                    'online': is_online or bool(mon and mon.online),
                    'bateria': mon.bateria_percentual,
                    'heartbeat': timezone.localtime(mon.ultimo_heartbeat).strftime('%H:%M:%S') if mon.ultimo_heartbeat else 'Sem sinal',
                } if mon else None,
                'telemetria': {
                    'fc_bpm': telem.fc_bpm,
                    'spo2_percent': telem.spo2_percent,
                    'pa_sistolica': telem.pa_sistolica,
                    'pa_diastolica': telem.pa_diastolica,
                    'pam_mmhg': telem.pam_mmhg,
                    'fr_irpm': telem.fr_irpm,
                    'temperatura_c': str(telem.temperatura_c) if telem.temperatura_c is not None else '',
                    'etco2_mmhg': telem.etco2_mmhg,
                    'ritmo_ecg': telem.ritmo_ecg,
                    'nivel_alerta': telem.nivel_alerta,
                    'alertas': telem.alertas_detectados,
                    'coletado_em': timezone.localtime(telem.coletado_em).strftime('%H:%M:%S'),
                    'origem': telem.origem_leitura,
                } if telem else None,
            })

        return {
            'unit_id': unit.pk if unit else None,
            'unit_name': unit.unidade.nome if (unit and unit.unidade) else '',
            'alas': [{'id': a.pk, 'codigo': a.codigo, 'nome': a.nome, 'tipo': a.get_tipo_display()} for a in alas_qs],
            'beds': bed_cards,
            'alerts': [{
                'public_id': str(al.public_id),
                'leito_codigo': al.leito.codigo,
                'ala': al.leito.ala.nome,
                'severidade': al.severidade,
                'parametro': al.parametro,
                'valor_aferido': al.valor_aferido,
                'mensagem': al.mensagem,
                'ocorrido_em': timezone.localtime(al.ocorrido_em).strftime('%d/%m %H:%M:%S'),
            } for al in open_alerts],
            'summary': {
                'total_leitos': len(beds),
                'ocupados': counts['OCUPADO'],
                'disponiveis': counts['DISPONIVEL'],
                'higienizacao': counts['HIGIENIZACAO'],
                'alertas_ativos': len(open_alerts),
            },
        }
