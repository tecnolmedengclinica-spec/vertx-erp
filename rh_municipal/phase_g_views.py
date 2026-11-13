"""Authenticated municipal health screens. Sensitive content is never in dashboard queries."""
import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import BadRequest, PermissionDenied, ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from platform_core.context import current_organization, tenant_context
from . import phase_g_services as svc
from . import saude_realtime_services as rt
from .phase_g_forms import (
    ClinicalEntryActionForm, HealthAgendaForm, HealthAppointmentForm, HealthFieldVisitForm,
    HealthEpidemiologyNotificationForm, HealthEpidemiologyTransitionForm,
    HealthDispensationForm, HealthLotReceiveForm, HealthMedicineForm,
    HealthSanitaryEstablishmentForm, HealthSanitaryInspectionForm,
    HealthProductionForm, HealthProfessionalForm, HealthRegulationPriorityForm,
    HealthRegulationRequestForm, HealthRegulationScheduleForm,
    HealthRegulationScheduleTransitionForm, HealthRegulationSlotForm,
    HealthTeamForm, HealthTFDForm, HealthTFDTransitionForm, HealthUnitForm, PatientForm,
)
from . import phase_g_models as health
from .models import AcessoMunicipal
from .public_services import accesses_for, has_municipal_permission
from .phase_e_services import public_module_available
from .public_views import _context
from .public_services import scoped_queryset


def _can(user, capability):
    organization = current_organization()
    return bool(organization and public_module_available(organization, 'saude_sus')
                and has_municipal_permission(user, capability, module='saude_sus'))


@login_required
@require_GET
def health_home(request):
    summary = svc.health_dashboard(request.user)
    flags = {
        'can_health_dashboard': True,
        'can_health_patients': _can(request.user, 'saude.patients.view') or _can(request.user, 'saude.patients.manage'),
        'can_health_patient_manage': _can(request.user, 'saude.patients.manage'),
        'can_health_records': _can(request.user, 'saude.records.view') or _can(request.user, 'saude.records.sensitive'),
        'can_health_pharmacy': _can(request.user, 'saude.pharmacy.view') or _can(request.user, 'saude.pharmacy.manage'),
        'can_health_field': _can(request.user, 'saude.field.view') or _can(request.user, 'saude.field.manage'),
        'can_health_sanitary': _can(request.user, 'saude.sanitary.view') or _can(request.user, 'saude.sanitary.manage'),
        'can_health_epidemiology': _can(request.user, 'saude.epidemiology.view') or _can(request.user, 'saude.epidemiology.manage'),
        'health_encryption_ready': bool(getattr(settings, 'MUNICIPAL_HEALTH_ENCRYPTION_KEY', '')),
        'can_health_units': _can(request.user, 'saude.units.view') or _can(request.user, 'saude.units.manage'),
        'can_health_teams': _can(request.user, 'saude.teams.view') or _can(request.user, 'saude.teams.manage'),
        'can_health_professionals': _can(request.user, 'saude.professionals.view') or _can(request.user, 'saude.professionals.manage'),
        'can_health_appointments': _can(request.user, 'saude.appointments.view') or _can(request.user, 'saude.appointments.manage'),
        'can_health_tfd': _can(request.user, 'saude.tfd.view') or _can(request.user, 'saude.tfd.manage'),
        'can_health_production': _can(request.user, 'saude.production.view') or _can(request.user, 'saude.production.manage'),
        'can_health_regulation': any(_can(request.user, capability) for capability in (
            'saude.regulation.view', 'saude.regulation.manage', 'saude.regulation.prioritize',
        )),
    }
    context = _context(request, **flags)
    context.update({'summary': summary, 'title': 'Saúde SUS', 'subtitle': 'Visão municipal agregada. Integrações oficiais e protocolos assistenciais exigem homologação própria.'})
    return render(request, 'rh_municipal/phase_g_home.html', context)


@login_required
@require_http_methods(['GET', 'POST'])
def patient_create(request):
    if not (_can(request.user, 'saude.patients.manage')):
        raise PermissionDenied('Cadastro de cidadão/paciente não autorizado.')
    form = PatientForm(request.POST or None, actor=request.user)
    if request.method == 'POST' and form.is_valid():
        try:
            svc.create_patient(request.user, data=form.cleaned_data)
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Cadastro municipal de saúde criado.')
            return redirect('rh_municipal_public:saude_home')
    context = _context(request, can_health_dashboard=True, can_health_patients=True,
                        can_health_patient_manage=True, can_health_records=False,
                        can_health_pharmacy=False, health_encryption_ready=False)
    context.update({'form': form, 'title': 'Novo cidadão/paciente', 'subtitle': 'Dados municipais de saúde, restritos à Prefeitura e aos escopos autorizados.'})
    return render(request, 'rh_municipal/phase_g_patient_form.html', context)


HEALTH_REGISTRIES = {
    'unidades': {
        'title': 'Unidades de saúde', 'view': 'saude.units.view', 'manage': 'saude.units.manage',
        'form': HealthUnitForm, 'model': health.UnidadeSaudeMunicipal,
        'service': 'configure_health_unit', 'select': ('unidade',),
        'rows': lambda row: (row.unidade.nome, row.get_tipo_display(), row.cnes or 'CNES não informado', 'Ativa' if row.ativo else 'Inativa'),
    },
    'equipes': {
        'title': 'Equipes de saúde', 'view': 'saude.teams.view', 'manage': 'saude.teams.manage',
        'form': HealthTeamForm, 'model': health.EquipeSaudeMunicipal,
        'service': 'configure_health_team', 'select': ('unidade_saude__unidade',),
        'rows': lambda row: (row.codigo, row.nome, f'{row.get_tipo_display()} · {row.unidade_saude.unidade.nome}', 'Ativa' if row.ativo else 'Inativa'),
    },
    'profissionais': {
        'title': 'Profissionais de saúde', 'view': 'saude.professionals.view', 'manage': 'saude.professionals.manage',
        'form': HealthProfessionalForm, 'model': health.ProfissionalSaudeMunicipal,
        'service': 'configure_health_professional', 'select': ('servidor', 'unidade_saude__unidade', 'equipe'),
        'rows': lambda row: (row.servidor.nome, row.ocupacao, f'{row.unidade_saude.unidade.nome} · {row.equipe.nome if row.equipe_id else "Sem equipe"}', 'Ativo' if row.ativo else 'Inativo'),
    },
    'agendas': {
        'title': 'Agendas e atendimentos', 'view': 'saude.appointments.view', 'manage': 'saude.appointments.manage',
        'form': HealthAgendaForm, 'model': health.AgendaSaudeMunicipal,
        'service': 'configure_health_agenda', 'select': ('profissional__servidor', 'unidade_saude__unidade'),
        'rows': lambda row: (row.profissional.servidor.nome, row.unidade_saude.unidade.nome,
                             f'{row.inicio:%d/%m/%Y %H:%M}–{row.fim:%H:%M} · {row.capacidade} vagas', row.get_situacao_display()),
    },
    'atendimentos': {
        'title': 'Agendamentos', 'view': 'saude.appointments.view', 'manage': 'saude.appointments.manage',
        'form': HealthAppointmentForm, 'model': health.AtendimentoSaudeMunicipal,
        'service': 'create_appointment', 'select': ('cidadao', 'agenda__profissional__servidor', 'unidade_saude__unidade'),
        'rows': lambda row: (row.cidadao.nome, row.unidade_saude.unidade.nome,
                             f'{row.agenda.inicio:%d/%m/%Y %H:%M}' if row.agenda_id else 'Sem agenda',
                             row.get_status_display(), row.pk,
                             row.status if row.status in {health.AtendimentoSaudeMunicipal.Status.AGENDADO,
                                                          health.AtendimentoSaudeMunicipal.Status.EM_ATENDIMENTO} else '',
                             row.cidadao.public_id),
    },
}


@login_required
@require_http_methods(['GET', 'POST'])
def health_registry(request, registry):
    config = HEALTH_REGISTRIES.get(registry)
    if config is None:
        raise PermissionDenied('Área da Saúde SUS desconhecida.')
    can_view = _can(request.user, config['view']) or _can(request.user, config['manage'])
    can_manage = _can(request.user, config['manage'])
    if not can_view:
        raise PermissionDenied('Você não possui acesso a este cadastro de Saúde SUS.')

    form = config['form'](request.POST or None, actor=request.user) if can_manage else None
    if request.method == 'POST':
        if not can_manage:
            raise PermissionDenied('Você não pode alterar este cadastro de Saúde SUS.')
        if form.is_valid():
            try:
                if registry == 'atendimentos':
                    svc.create_appointment(
                        request.user, agenda_id=form.cleaned_data['agenda'].pk,
                        citizen_id=form.cleaned_data['cidadao'].pk,
                        request_id=form.cleaned_data['request_id'],
                    )
                else:
                    getattr(svc, config['service'])(request.user, data=form.cleaned_data)
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, 'Registro salvo na Prefeitura atual.')
                return redirect('rh_municipal_public:saude_registry', registry=registry)

    organization = current_organization()
    records = config['model'].objects.filter(organization=organization)
    records = scoped_queryset(request.user, records)
    records = records.select_related(*config['select']).order_by('-pk')[:100]
    rows = [config['rows'](row) for row in records]
    context = _context(
        request, can_health_dashboard=_can(request.user, 'saude.dashboard.view'),
        can_health_patients=_can(request.user, 'saude.patients.view') or _can(request.user, 'saude.patients.manage'),
        can_health_patient_manage=_can(request.user, 'saude.patients.manage'),
        can_health_records=_can(request.user, 'saude.records.view') or _can(request.user, 'saude.records.sensitive'),
        can_health_pharmacy=_can(request.user, 'saude.pharmacy.view') or _can(request.user, 'saude.pharmacy.manage'),
        health_encryption_ready=bool(getattr(settings, 'MUNICIPAL_HEALTH_ENCRYPTION_KEY', '')),
        can_health_units=_can(request.user, 'saude.units.view') or _can(request.user, 'saude.units.manage'),
        can_health_teams=_can(request.user, 'saude.teams.view') or _can(request.user, 'saude.teams.manage'),
        can_health_professionals=_can(request.user, 'saude.professionals.view') or _can(request.user, 'saude.professionals.manage'),
        can_health_appointments=_can(request.user, 'saude.appointments.view') or _can(request.user, 'saude.appointments.manage'),
    )
    context.update({
        'title': config['title'], 'subtitle': 'Cadastros limitados à Prefeitura, ao módulo contratado e ao escopo municipal autorizado.',
        'form': form, 'can_manage_registry': can_manage, 'rows': rows, 'registry': registry,
        'health_registry_title': config['title'],
        'can_health_records_sensitive': _can(request.user, 'saude.records.sensitive'),
    })
    return render(request, 'rh_municipal/phase_g_registry.html', context)


@login_required
@require_http_methods(['POST'])
def health_appointment_transition(request, pk):
    if not _can(request.user, 'saude.appointments.manage'):
        raise PermissionDenied('Você não pode alterar a situação deste atendimento.')
    try:
        svc.transition_appointment(
            request.user, appointment_id=pk,
            status=request.POST.get('status', ''),
            justification=request.POST.get('justification', ''),
        )
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Situação do atendimento atualizada e auditada.')
    return redirect('rh_municipal_public:saude_registry', registry='atendimentos')


@login_required
@require_http_methods(['GET', 'POST'])
def health_clinical_record(request, pk):
    if not all(_can(request.user, code) for code in ('saude.records.sensitive', 'saude.records.view', 'saude.patients.view')):
        raise PermissionDenied('Acesso ao prontuário clínico não autorizado.')
    if request.method == 'POST' and not _can(request.user, 'saude.records.create'):
        raise PermissionDenied('Este acesso permite somente consulta do prontuário.')
    organization = current_organization()
    appointment = scoped_queryset(
        request.user,
        health.AtendimentoSaudeMunicipal.objects.filter(organization=organization),
    ).select_related('cidadao', 'unidade_saude__unidade').filter(pk=pk).first()
    if appointment is None:
        raise PermissionDenied('O atendimento não está disponível no escopo autorizado.')

    for capability in ('saude.records.view', 'saude.records.sensitive'):
        svc._record_allowed(request.user, capability, appointment)
    svc._record_allowed(request.user, 'saude.patients.view', appointment.cidadao)
    if request.method == 'POST':
        svc._record_allowed(request.user, 'saude.records.create', appointment)
    svc._audit(request.user, organization, 'PRONTUARIO_ABERTO', appointment._meta.label_lower,
               appointment.pk, appointment.cidadao_id, reason='Consulta ao atendimento')

    form = ClinicalEntryActionForm(request.POST or None, initial={'action': 'create'})
    viewed_content = None
    if request.method == 'POST' and form.is_valid():
        values = form.cleaned_data
        try:
            if values['action'] == 'create':
                svc.create_clinical_entry(
                    request.user, appointment_id=appointment.pk,
                    content=values['content'],
                )
                messages.success(request, 'Evolução cifrada e registrada na Prefeitura atual.')
                return redirect('rh_municipal_public:saude_clinical_record', pk=appointment.pk)
            entry = health.EvolucaoClinicaMunicipal.objects.filter(
                organization=organization, atendimento=appointment, pk=values['entry_id'],
            ).first()
            if entry is None:
                raise ValidationError('Evolução não encontrada neste atendimento.')
            viewed_content = svc.read_clinical_entry(
                request.user, entry_id=entry.pk, justification=values['justification'],
            )
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, str(exc))

    entries = scoped_queryset(
        request.user,
        health.EvolucaoClinicaMunicipal.objects.filter(
            organization=organization, atendimento=appointment,
        ),
    ).select_related('autor').defer('conteudo_cifrado').order_by('-criada_em')
    context = _context(
        request, can_health_dashboard=_can(request.user, 'saude.dashboard.view'),
        can_health_patients=_can(request.user, 'saude.patients.view'),
        can_health_records=True, can_health_records_sensitive=True,
        can_health_patient_manage=_can(request.user, 'saude.patients.manage'),
        can_health_pharmacy=False,
        health_encryption_ready=bool(getattr(settings, 'MUNICIPAL_HEALTH_ENCRYPTION_KEY', '')),
        can_health_units=_can(request.user, 'saude.units.view'),
        can_health_teams=_can(request.user, 'saude.teams.view'),
        can_health_professionals=_can(request.user, 'saude.professionals.view'),
        can_health_appointments=_can(request.user, 'saude.appointments.view'),
    )
    context.update({
        'title': 'Prontuário clínico',
        'subtitle': 'A leitura do conteúdo exige justificativa e gera auditoria.',
        'appointment': appointment, 'entries': entries, 'form': form,
        'viewed_content': viewed_content,
        'can_create_clinical': has_municipal_permission(request.user, 'saude.records.create', module='saude_sus', record=appointment),
    })
    response = render(request, 'rh_municipal/phase_g_clinical_record.html', context)
    response['Cache-Control'] = 'no-store, private'
    response['Pragma'] = 'no-cache'
    return response


@login_required
@require_http_methods(['GET', 'POST'])
def health_pharmacy(request):
    can_view = any(_can(request.user, capability) for capability in (
        'saude.pharmacy.view', 'saude.pharmacy.manage', 'saude.dispense',
    ))
    if not can_view:
        raise PermissionDenied('Você não possui acesso à farmácia municipal.')
    organization = current_organization()
    can_manage_stock = _can(request.user, 'saude.pharmacy.manage')
    can_dispense = _can(request.user, 'saude.dispense')
    can_manage_catalog = can_manage_stock and any(
        access.escopo == AcessoMunicipal.Escopo.PREFEITURA for access in accesses_for(request.user)
    )

    medication_form = HealthMedicineForm(request.POST if request.POST.get('action') == 'medication' else None, actor=request.user)
    lot_form = HealthLotReceiveForm(request.POST if request.POST.get('action') == 'lot' else None, actor=request.user)
    dispensation_form = HealthDispensationForm(request.POST if request.POST.get('action') == 'dispense' else None, actor=request.user)
    if request.method == 'POST':
        action = request.POST.get('action')
        selected_form = {
            'medication': medication_form,
            'lot': lot_form,
            'dispense': dispensation_form,
        }.get(action)
        if selected_form is None:
            raise PermissionDenied('Operação da farmácia inválida.')
        if action == 'medication' and not can_manage_catalog:
            raise PermissionDenied('Somente o escopo da Prefeitura pode administrar o catálogo municipal de medicamentos.')
        if action == 'lot' and not can_manage_stock:
            raise PermissionDenied('Você não pode receber lotes de medicamentos.')
        if action == 'dispense' and not can_dispense:
            raise PermissionDenied('Você não pode registrar dispensações.')
        if selected_form.is_valid():
            try:
                data = selected_form.cleaned_data
                if action == 'medication':
                    svc.configure_medicine(request.user, data=data)
                elif action == 'lot':
                    svc.receive_medicine_lot(
                        request.user, medicine_id=data['medicamento'].pk,
                        health_unit_id=data['unidade_saude'].pk, lot=data['lot'],
                        expires_on=data['expires_on'], quantity=data['quantity'],
                    )
                else:
                    svc.dispense_fefo(
                        request.user, citizen_id=data['cidadao'].pk,
                        health_unit_id=data['unidade_saude'].pk,
                        items=[{'medicine_id': data['medicamento'].pk, 'quantity': data['quantidade']}],
                        request_id=data['request_id'],
                    )
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, {
                    'medication': 'Medicamento cadastrado no catálogo da Prefeitura.',
                    'lot': 'Lote registrado; o saldo foi adicionado uma única vez.',
                    'dispense': 'Dispensação registrada com baixa FEFO e auditoria.',
                }[action])
                return redirect('rh_municipal_public:saude_pharmacy')

    units = scoped_queryset(request.user, health.UnidadeSaudeMunicipal.objects.filter(
        organization=organization, ativo=True,
    ))
    lots = health.LoteMedicamentoMunicipal.objects.filter(
        organization=organization, unidade_saude__in=units,
    ).select_related('medicamento', 'unidade_saude__unidade').order_by('validade', 'pk')
    medicines = health.MedicamentoMunicipal.objects.filter(organization=organization, ativo=True)
    if not any(access.escopo == AcessoMunicipal.Escopo.PREFEITURA for access in accesses_for(request.user)):
        medicines = medicines.filter(lotes__unidade_saude__in=units).distinct()
    medicine_rows = []
    for medicine in medicines.order_by('nome')[:100]:
        scoped_lots = lots.filter(medicamento=medicine)
        medicine_rows.append((
            medicine.codigo, medicine.nome, medicine.apresentacao,
            sum((lot.saldo for lot in scoped_lots), Decimal('0')),
            scoped_lots.count(),
        ))
    lot_rows = [(lot.medicamento.nome, lot.unidade_saude.unidade.nome, lot.lote,
                 lot.validade, lot.saldo) for lot in lots[:200]]
    summary = {
        'medicines': medicines.count(),
        'lots': lots.count(),
        'expired': lots.filter(validade__lt=timezone.localdate(), saldo__gt=0).count(),
        'low_stock': sum(1 for medicine in medicines if medicine.estoque_minimo > 0 and
                         sum((lot.saldo for lot in lots.filter(medicamento=medicine)), Decimal('0')) <= medicine.estoque_minimo),
    }
    context = _context(
        request, can_health_dashboard=_can(request.user, 'saude.dashboard.view'),
        can_health_patients=_can(request.user, 'saude.patients.view'),
        can_health_patient_manage=_can(request.user, 'saude.patients.manage'),
        can_health_records=_can(request.user, 'saude.records.view') or _can(request.user, 'saude.records.sensitive'),
        can_health_records_sensitive=_can(request.user, 'saude.records.sensitive'),
        can_health_pharmacy=True,
        health_encryption_ready=bool(getattr(settings, 'MUNICIPAL_HEALTH_ENCRYPTION_KEY', '')),
        can_health_units=_can(request.user, 'saude.units.view'),
        can_health_teams=_can(request.user, 'saude.teams.view'),
        can_health_professionals=_can(request.user, 'saude.professionals.view'),
        can_health_appointments=_can(request.user, 'saude.appointments.view'),
    )
    context.update({
        'title': 'Farmácia municipal',
        'subtitle': 'Catálogo, lotes por unidade e dispensação FEFO sob escopo e auditoria.',
        'can_manage_stock': can_manage_stock, 'can_manage_catalog': can_manage_catalog,
        'can_dispense': can_dispense, 'medication_form': medication_form,
        'lot_form': lot_form, 'dispensation_form': dispensation_form,
        'medicine_rows': medicine_rows, 'lot_rows': lot_rows, 'summary': summary,
    })
    return render(request, 'rh_municipal/phase_g_pharmacy.html', context)


@login_required
@require_http_methods(['GET', 'POST'])
def health_field_visits(request):
    can_view = _can(request.user, 'saude.field.view') or _can(request.user, 'saude.field.manage')
    if not can_view:
        raise PermissionDenied('Você não possui acesso às visitas territoriais.')
    organization = current_organization()
    can_manage = _can(request.user, 'saude.field.manage')
    form = HealthFieldVisitForm(
        request.POST if request.POST.get('action') == 'record_visit' else None,
        actor=request.user,
    )
    if request.method == 'POST':
        if request.POST.get('action') != 'record_visit' or not can_manage:
            raise PermissionDenied('Você não pode registrar visitas ACS/ACE.')
        if form.is_valid():
            data = form.cleaned_data
            try:
                svc.record_field_visit(
                    request.user, request_id=data['request_id'], visit_type=data['tipo'],
                    agent_id=data['agente'].pk,
                    family_id=data['familia'].pk if data['familia'] else None,
                    citizen_id=data['cidadao'].pk if data['cidadao'] else None,
                    property_id=data['imovel'].pk if data['imovel'] else None,
                    performed_at=data['realizada_em'], observations=data['observacoes'],
                )
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            else:
                messages.success(request, 'Visita registrada com observações protegidas e auditoria.')
                return redirect('rh_municipal_public:saude_field_visits')

    visits = scoped_queryset(
        request.user,
        health.VisitaTerritorialSaude.objects.filter(organization=organization),
    ).select_related(
        'agente__servidor', 'agente__unidade_saude__unidade', 'familia', 'cidadao', 'imovel',
    ).order_by('-realizada_em', '-pk')[:100]
    context = _context(
        request, can_health_dashboard=_can(request.user, 'saude.dashboard.view'),
        can_health_patients=_can(request.user, 'saude.patients.view'),
        can_health_patient_manage=_can(request.user, 'saude.patients.manage'),
        can_health_records=_can(request.user, 'saude.records.view') or _can(request.user, 'saude.records.sensitive'),
        can_health_records_sensitive=_can(request.user, 'saude.records.sensitive'),
        can_health_pharmacy=_can(request.user, 'saude.pharmacy.view') or _can(request.user, 'saude.pharmacy.manage'),
        can_health_field=True,
        can_health_units=_can(request.user, 'saude.units.view'),
        can_health_teams=_can(request.user, 'saude.teams.view'),
        can_health_professionals=_can(request.user, 'saude.professionals.view'),
        can_health_appointments=_can(request.user, 'saude.appointments.view'),
    )
    context.update({
        'title': 'Visitas territoriais ACS/ACE',
        'subtitle': 'Registros online com dados limitados ao escopo municipal e observações cifradas.',
        'can_manage_field': can_manage, 'form': form, 'visits': visits,
        'visit_count': len(visits),
    })
    response = render(request, 'rh_municipal/phase_g_field_visits.html', context)
    response['Cache-Control'] = 'no-store, private'
    response['Pragma'] = 'no-cache'
    return response


@login_required
@require_http_methods(['GET', 'POST'])
def health_surveillance(request):
    can_sanitary_view = _can(request.user, 'saude.sanitary.view') or _can(request.user, 'saude.sanitary.manage')
    can_sanitary_manage = _can(request.user, 'saude.sanitary.manage')
    can_epidemiology_view = _can(request.user, 'saude.epidemiology.view') or _can(request.user, 'saude.epidemiology.manage')
    can_epidemiology_manage = _can(request.user, 'saude.epidemiology.manage')
    if not (can_sanitary_view or can_epidemiology_view):
        raise PermissionDenied('Você não possui acesso à Vigilância municipal.')
    organization = current_organization()
    action = request.POST.get('action') if request.method == 'POST' else ''
    establishment_form = HealthSanitaryEstablishmentForm(
        request.POST if action == 'establishment' else None, actor=request.user,
    ) if can_sanitary_manage else None
    inspection_form = HealthSanitaryInspectionForm(
        request.POST if action == 'inspection' else None, actor=request.user,
    ) if can_sanitary_manage else None
    notification_form = HealthEpidemiologyNotificationForm(
        request.POST if action == 'notification' else None, actor=request.user,
    ) if can_epidemiology_manage else None
    transition_form = HealthEpidemiologyTransitionForm(
        request.POST if action == 'notification_transition' else None,
    ) if can_epidemiology_manage else None

    if request.method == 'POST':
        if action == 'establishment' and can_sanitary_manage and establishment_form.is_valid():
            try:
                svc.configure_sanitary_establishment(request.user, data=establishment_form.cleaned_data)
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            else:
                messages.success(request, 'Estabelecimento sanitário cadastrado no escopo municipal.')
                return redirect('rh_municipal_public:saude_surveillance')
        elif action == 'inspection' and can_sanitary_manage and inspection_form.is_valid():
            data = inspection_form.cleaned_data
            try:
                svc.register_sanitary_inspection(
                    request.user, request_id=data['request_id'], establishment_id=data['estabelecimento'].pk,
                    professional_id=data['realizada_por'].pk, performed_at=data['realizada_em'],
                    result=data['resultado'], return_deadline=data['prazo_retorno'], observations=data['observacoes'],
                )
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            else:
                messages.success(request, 'Inspeção registrada; observações cifradas e evento auditado.')
                return redirect('rh_municipal_public:saude_surveillance')
        elif action == 'notification' and can_epidemiology_manage and notification_form.is_valid():
            data = notification_form.cleaned_data
            try:
                svc.create_epidemiology_notification(
                    request.user, request_id=data['request_id'],
                    citizen_id=data['cidadao'].pk if data['cidadao'] else None,
                    code=data['codigo_agravo'], description=data['descricao_agravo'],
                    notified_on=data['notificada_em'], details=data['detalhes'],
                )
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            else:
                messages.success(request, 'Notificação restrita registrada e auditada; ela não será transmitida automaticamente.')
                return redirect('rh_municipal_public:saude_surveillance')
        elif action == 'notification_transition' and can_epidemiology_manage and transition_form.is_valid():
            try:
                svc.transition_epidemiology_notification(
                    request.user, notification_id=request.POST.get('notification_id'),
                    situation=transition_form.cleaned_data['situacao'],
                    justification=transition_form.cleaned_data['justificativa'],
                )
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            else:
                messages.success(request, 'Situação da notificação atualizada com justificativa e auditoria.')
                return redirect('rh_municipal_public:saude_surveillance')
        elif action == 'establishment' and can_sanitary_manage:
            pass
        elif action == 'inspection' and can_sanitary_manage:
            pass
        elif action == 'notification' and can_epidemiology_manage:
            pass
        elif action == 'notification_transition' and can_epidemiology_manage:
            pass
        else:
            raise PermissionDenied('Operação de Vigilância não autorizada ou inválida.')

    establishments = inspections = notifications = []
    if can_sanitary_view or can_sanitary_manage:
        establishments = scoped_queryset(
            request.user, health.EstabelecimentoSanitarioMunicipal.objects.filter(organization=organization),
        ).order_by('razao_social')[:200]
        inspections = scoped_queryset(
            request.user, health.InspecaoSanitariaMunicipal.objects.filter(organization=organization),
        ).select_related('estabelecimento', 'realizada_por__servidor').order_by('-realizada_em', '-pk')[:100]
    if can_epidemiology_view or can_epidemiology_manage:
        # Restricted metadata only; never show citizen identity or decrypted details in the list.
        notifications = scoped_queryset(
            request.user, health.NotificacaoEpidemiologicaMunicipal.objects.filter(organization=organization),
        ).order_by('-notificada_em', '-pk')[:100]
    context = _context(
        request, can_health_dashboard=_can(request.user, 'saude.dashboard.view'),
        can_health_patients=_can(request.user, 'saude.patients.view'),
        can_health_patient_manage=_can(request.user, 'saude.patients.manage'),
        can_health_records=_can(request.user, 'saude.records.view') or _can(request.user, 'saude.records.sensitive'),
        can_health_records_sensitive=_can(request.user, 'saude.records.sensitive'),
        can_health_pharmacy=_can(request.user, 'saude.pharmacy.view') or _can(request.user, 'saude.pharmacy.manage'),
        can_health_field=_can(request.user, 'saude.field.view') or _can(request.user, 'saude.field.manage'),
        can_health_sanitary=can_sanitary_view or can_sanitary_manage,
        can_health_epidemiology=can_epidemiology_view or can_epidemiology_manage,
        can_health_units=_can(request.user, 'saude.units.view'),
        can_health_teams=_can(request.user, 'saude.teams.view'),
        can_health_professionals=_can(request.user, 'saude.professionals.view'),
        can_health_appointments=_can(request.user, 'saude.appointments.view'),
    )
    context.update({
        'title': 'Vigilância Sanitária e Epidemiológica',
        'subtitle': 'Cadastros e registros restritos, limitados ao tenant, ao escopo e à permissão.',
        'can_sanitary_view': can_sanitary_view, 'can_sanitary_manage': can_sanitary_manage,
        'can_epidemiology_view': can_epidemiology_view, 'can_epidemiology_manage': can_epidemiology_manage,
        'establishment_form': establishment_form, 'inspection_form': inspection_form,
        'notification_form': notification_form, 'transition_form': transition_form,
        'establishments': establishments, 'inspections': inspections, 'notifications': notifications,
    })
    response = render(request, 'rh_municipal/phase_g_surveillance.html', context)
    response['Cache-Control'] = 'no-store, private'
    response['Pragma'] = 'no-cache'
    return response


@login_required
@require_http_methods(['GET', 'POST'])
def health_tfd(request):
    can_view = _can(request.user, 'saude.tfd.view') or _can(request.user, 'saude.tfd.manage')
    can_manage = _can(request.user, 'saude.tfd.manage')
    if not can_view:
        raise PermissionDenied('Você não possui acesso às solicitações de TFD.')
    organization = current_organization()
    action = request.POST.get('action', '') if request.method == 'POST' else ''
    form = HealthTFDForm(request.POST if action == 'create' else None, actor=request.user) if can_manage else None
    transition_form = HealthTFDTransitionForm(
        request.POST if action == 'transition' else None,
    ) if can_manage else None
    if request.method == 'POST':
        if not can_manage:
            raise PermissionDenied('Você não pode alterar solicitações de TFD.')
        if action == 'create' and form.is_valid():
            data = form.cleaned_data
            try:
                svc.create_tfd_request(
                    request.user, request_id=data['request_id'], citizen_id=data['cidadao'].pk,
                    destination=data['destino'], treatment=data['tratamento'],
                    accompaniment=data['acompanhante'],
                )
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            else:
                messages.success(request, 'Solicitação de TFD criada. Dados clínicos foram protegidos.')
                return redirect('rh_municipal_public:saude_tfd')
        elif action == 'transition' and transition_form.is_valid():
            try:
                svc.transition_tfd_request(
                    request.user, request_id=request.POST.get('tfd_id'),
                    status=transition_form.cleaned_data['status'],
                    justification=transition_form.cleaned_data['justificativa'],
                )
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            else:
                messages.success(request, 'Situação do TFD atualizada e auditada.')
                return redirect('rh_municipal_public:saude_tfd')
        else:
            raise PermissionDenied('Operação de TFD inválida ou não autorizada.')
    rows = scoped_queryset(
        request.user, health.TFDMunicipal.objects.filter(organization=organization),
    ).select_related('cidadao').order_by('-solicitada_em', '-pk')[:100]
    context = _context(
        request, can_health_dashboard=_can(request.user, 'saude.dashboard.view'),
        can_health_patients=_can(request.user, 'saude.patients.view'),
        can_health_patient_manage=_can(request.user, 'saude.patients.manage'),
        can_health_records=_can(request.user, 'saude.records.view'),
        can_health_pharmacy=_can(request.user, 'saude.pharmacy.view'),
        can_health_field=_can(request.user, 'saude.field.view'),
        can_health_tfd=True,
    )
    context.update({
        'title': 'Tratamento Fora do Domicílio',
        'subtitle': 'Solicitações restritas por Prefeitura e escopo; detalhes de saúde ficam cifrados.',
        'can_manage_tfd': can_manage, 'form': form, 'transition_form': transition_form, 'rows': rows,
    })
    response = render(request, 'rh_municipal/phase_g_tfd.html', context)
    response['Cache-Control'] = 'no-store, private'
    response['Pragma'] = 'no-cache'
    return response


@login_required
@require_http_methods(['GET', 'POST'])
def health_regulation(request):
    can_view = any(_can(request.user, capability) for capability in (
        'saude.regulation.view', 'saude.regulation.manage', 'saude.regulation.prioritize',
    ))
    can_manage = _can(request.user, 'saude.regulation.manage')
    can_prioritize = _can(request.user, 'saude.regulation.prioritize')
    if not can_view:
        raise PermissionDenied('Você não possui acesso à regulação municipal.')
    organization = current_organization()
    action = request.POST.get('action', '') if request.method == 'POST' else ''
    if request.method == 'POST':
        if action in {'create_request', 'create_slot', 'schedule', 'transition'} and not can_manage:
            raise PermissionDenied('Você não pode gerenciar a fila de regulação.')
        if action == 'priority' and not can_prioritize:
            raise PermissionDenied('Você não pode alterar prioridade de regulação.')
        if action not in {'create_request', 'create_slot', 'schedule', 'transition', 'priority'}:
            raise PermissionDenied('Operação de regulação inválida.')
    request_form = HealthRegulationRequestForm(
        request.POST if action == 'create_request' else None, actor=request.user,
    ) if can_manage else None
    slot_form = HealthRegulationSlotForm(
        request.POST if action == 'create_slot' else None, actor=request.user,
    ) if can_manage else None
    schedule_form = HealthRegulationScheduleForm(
        request.POST if action == 'schedule' else None, actor=request.user,
    ) if can_manage else None
    transition_form = HealthRegulationScheduleTransitionForm(
        request.POST if action == 'transition' else None,
    ) if can_manage else None
    priority_form = HealthRegulationPriorityForm(
        request.POST if action == 'priority' else None,
    ) if can_prioritize else None
    if request.method == 'POST':
        try:
            if action == 'create_request' and can_manage and request_form.is_valid():
                data = request_form.cleaned_data
                svc.create_regulation_request(
                    request.user, request_id=data['request_id'], citizen_id=data['cidadao'].pk,
                    origin_unit_id=data['unidade_origem'].pk,
                    destination_unit_id=data['unidade_destino'].pk if data['unidade_destino'] else None,
                    procedure=data['procedimento'], priority=data['prioridade'],
                    justification=data['justificativa'],
                )
                messages.success(request, 'Solicitação entrou na fila; justificativa clínica protegida.')
                return redirect('rh_municipal_public:saude_regulation')
            if action == 'create_slot' and can_manage and slot_form.is_valid():
                data = slot_form.cleaned_data
                svc.create_regulation_slot(
                    request.user, request_id=data['request_id'], health_unit_id=data['unidade_saude'].pk,
                    procedure=data['procedimento'], starts_at=data['inicio'], capacity=data['capacidade'],
                )
                messages.success(request, 'Vaga de regulação criada dentro do escopo da unidade.')
                return redirect('rh_municipal_public:saude_regulation')
            if action == 'schedule' and can_manage and schedule_form.is_valid():
                data = schedule_form.cleaned_data
                svc.schedule_regulation_request(
                    request.user, operation_id=data['request_id'], request_id=data['solicitacao'].pk,
                    slot_id=data['vaga'].pk,
                )
                messages.success(request, 'Solicitação agendada e vinculada ao atendimento.')
                return redirect('rh_municipal_public:saude_regulation')
            if action == 'transition' and can_manage and transition_form.is_valid():
                svc.transition_regulation_schedule(
                    request.user, schedule_id=request.POST.get('schedule_id'),
                    situation=transition_form.cleaned_data['situacao'],
                    justification=transition_form.cleaned_data['justificativa'],
                )
                messages.success(request, 'Situação do atendimento atualizada e auditada.')
                return redirect('rh_municipal_public:saude_regulation')
            if action == 'priority' and can_prioritize and priority_form.is_valid():
                svc.change_regulation_priority(
                    request.user, request_id=request.POST.get('request_pk'),
                    priority=priority_form.cleaned_data['prioridade'],
                    justification=priority_form.cleaned_data['justificativa'],
                )
                messages.success(request, 'Prioridade alterada com justificativa e auditoria.')
                return redirect('rh_municipal_public:saude_regulation')
            if action not in {'create_request', 'create_slot', 'schedule', 'transition', 'priority'}:
                raise PermissionDenied('Operação de regulação inválida.')
            if action in {'create_request', 'create_slot', 'schedule', 'transition'} and not can_manage:
                raise PermissionDenied('Você não pode gerenciar a fila de regulação.')
            if action == 'priority' and not can_prioritize:
                raise PermissionDenied('Você não pode alterar prioridade de regulação.')
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))

    requests = scoped_queryset(
        request.user, health.SolicitacaoRegulacaoSaude.objects.filter(organization=organization),
    ).select_related('unidade_origem__unidade', 'unidade_destino__unidade').order_by('status', '-solicitada_em')[:100]
    slots = list(scoped_queryset(
        request.user, health.VagaRegulacaoSaude.objects.filter(organization=organization),
    ).select_related('unidade_saude__unidade').order_by('inicio')[:100])
    for slot in slots:
        slot.vagas_ocupadas = slot.agendamentos_regulacao.filter(situacao='AGENDADO').count()
    schedules = scoped_queryset(
        request.user, health.AgendamentoRegulacaoSaude.objects.filter(organization=organization),
    ).select_related(
        'solicitacao', 'vaga__unidade_saude__unidade', 'atendimento',
    ).order_by('-criado_em')[:100]
    context = _context(
        request, can_health_dashboard=_can(request.user, 'saude.dashboard.view'),
        can_health_patients=_can(request.user, 'saude.patients.view'),
        can_health_patient_manage=_can(request.user, 'saude.patients.manage'),
        can_health_records=_can(request.user, 'saude.records.view'),
        can_health_pharmacy=_can(request.user, 'saude.pharmacy.view'),
        can_health_field=_can(request.user, 'saude.field.view'),
        can_health_regulation=True,
    )
    context.update({
        'title': 'Regulação e fila municipal',
        'subtitle': 'Fila, capacidade e atendimento vinculados; prioridade e transições auditadas.',
        'can_manage_regulation': can_manage, 'can_prioritize_regulation': can_prioritize,
        'request_form': request_form, 'slot_form': slot_form, 'schedule_form': schedule_form,
        'transition_form': transition_form, 'priority_form': priority_form,
        'requests': requests, 'slots': slots, 'schedules': schedules,
    })
    response = render(request, 'rh_municipal/phase_g_regulation.html', context)
    response['Cache-Control'] = 'no-store, private'
    response['Pragma'] = 'no-cache'
    return response


@login_required
@require_http_methods(['GET', 'POST'])
def health_production(request):
    can_view = _can(request.user, 'saude.production.view') or _can(request.user, 'saude.production.manage')
    can_manage = _can(request.user, 'saude.production.manage')
    if not can_view:
        raise PermissionDenied('Você não possui acesso aos registros de produção SUS.')
    organization = current_organization()
    form = HealthProductionForm(
        request.POST if request.POST.get('action') == 'record' else None,
        actor=request.user,
    ) if can_manage else None
    if request.method == 'POST':
        if request.POST.get('action') != 'record' or not can_manage:
            raise PermissionDenied('Você não pode registrar produção SUS.')
        if form.is_valid():
            data = form.cleaned_data
            try:
                svc.register_sus_production(
                    request.user, request_id=data['request_id'], competence=data['competencia'],
                    record_type=data['tipo'], health_unit_id=data['unidade_saude'].pk,
                    citizen_id=data['cidadao'].pk if data['cidadao'] else None,
                    professional_id=data['profissional'].pk if data['profissional'] else None,
                    procedure_code=data['codigo_procedimento'], quantity=data['quantidade'],
                )
            except (PermissionDenied, ValidationError) as exc:
                messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            else:
                messages.success(request, 'Registro local criado; nenhuma transmissão oficial foi realizada.')
                return redirect('rh_municipal_public:saude_production')
        else:
            messages.error(request, 'Revise os dados do registro de produção.')
    rows = scoped_queryset(
        request.user, health.RegistroProducaoSUS.objects.filter(organization=organization),
    ).select_related('unidade_saude__unidade').order_by('-competencia', '-criado_em', '-pk')[:100]
    context = _context(
        request, can_health_dashboard=_can(request.user, 'saude.dashboard.view'),
        can_health_patients=_can(request.user, 'saude.patients.view'),
        can_health_patient_manage=_can(request.user, 'saude.patients.manage'),
        can_health_records=_can(request.user, 'saude.records.view'),
        can_health_pharmacy=_can(request.user, 'saude.pharmacy.view'),
        can_health_field=_can(request.user, 'saude.field.view'),
        can_health_production=True,
    )
    context.update({
        'title': 'Produção SUS',
        'subtitle': 'Registros locais e rascunhos AIH/APAC. Envio a sistemas oficiais permanece desabilitado.',
        'can_manage_production': can_manage, 'form': form, 'rows': rows,
    })
    response = render(request, 'rh_municipal/phase_g_production.html', context)
    response['Cache-Control'] = 'no-store, private'
    response['Pragma'] = 'no-cache'
    return response


@login_required
@require_GET
def health_manifest(request):
    organization = current_organization()
    if not organization or not public_module_available(organization, 'saude_sus') or not has_municipal_permission(
        request.user, 'saude.dashboard.view', module='saude_sus',
    ):
        raise PermissionDenied('PWA Saúde SUS não autorizado.')
    body = {
        'id': '/gestao-publica/saude/',
        'name': 'VERTYX Saúde SUS',
        'short_name': 'VERTYX Saúde',
        'start_url': '/gestao-publica/saude/',
        'scope': '/gestao-publica/saude/',
        'display': 'standalone',
        'background_color': '#f4f7fb',
        'theme_color': '#123b67',
    }
    response = HttpResponse(json.dumps(body), content_type='application/manifest+json')
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@require_GET
def health_worker(request):
    if not _can(request.user, 'saude.dashboard.view'):
        raise PermissionDenied('PWA Saúde SUS não autorizado.')
    body = """self.addEventListener('install',e=>{self.skipWaiting()});self.addEventListener('activate',e=>{e.waitUntil(self.clients.claim())});self.addEventListener('fetch',e=>{const p=new URL(e.request.url).pathname;if(e.request.method!=='GET'||p.startsWith('/gestao-publica/saude/prontuarios/')||p.endsWith('/prontuario/'))return;e.respondWith(fetch(e.request,{cache:'no-store'}))});"""
    response = HttpResponse(body, content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/gestao-publica/saude/'
    response['Cache-Control'] = 'no-store'
    return response


def get_tenant_branding(request=None):
    """Retorna dados dinâmicos de personalização (nome do município, UBS, brasão e cores), eliminando nomes estáticos."""
    org = current_organization()
    session_branding = request.session.get('vertyx_branding') if request and hasattr(request, 'session') else None
    session_org = request.session.get('vertyx_branding_org') if request and hasattr(request, 'session') else None
    if org and session_org == org.pk and session_branding and isinstance(session_branding, dict):
        return session_branding

    from .models import PerfilPrefeitura, UnidadeSaudeMunicipal

    profile = PerfilPrefeitura.objects.filter(organization=org).first() if org else None
    units = UnidadeSaudeMunicipal.objects.filter(organization=org) if org else UnidadeSaudeMunicipal.objects.none()
    ubs = scoped_queryset(request.user, units).select_related('unidade').first() if request and org else None

    nome_inst = profile.nome_institucional if profile and profile.nome_institucional else (org.display_name if org else 'Prefeitura Municipal')
    unidade_nome = ubs.unidade.nome if ubs and ubs.unidade else 'Unidade Básica de Saúde Central'
    cidade = org.cidade if org and org.cidade else 'Município'
    uf = org.uf if org and org.uf else 'BR'

    return {
        'nome_institucional': nome_inst,
        'orgao_gestor': 'Secretaria Municipal de Saúde',
        'unidade_nome': unidade_nome,
        'municipio': cidade,
        'uf': uf,
        'municipio_uf': f"{cidade} - {uf}" if cidade and uf else nome_inst,
        'cnpj': org.cnpj if org else '',
        'codigo_ibge': profile.codigo_ibge if profile else '',
        'cor_primaria': '#0284c7',
        'cor_secundaria': '#10b981',
        'slogan': 'Sistema Integrado de Gestão Pública e Saúde da Família',
    }


@login_required
@require_http_methods(['GET', 'POST'])
def health_system_customization(request):
    """Tela e API de personalização dinâmica de identidade visual, nome da prefeitura e unidades."""
    import json
    from django.http import JsonResponse
    from .models import PerfilPrefeitura, UnidadeSaudeMunicipal
    from .public_services import require_municipal_permission

    svc._require(request.user, 'saude.units.manage')
    require_municipal_permission(request.user, 'profile.manage')
    if not accesses_for(request.user).filter(papel='ADMIN_PREFEITURA', escopo='PREFEITURA').exists():
        raise PermissionDenied('Personalização exige administração com escopo de Prefeitura.')

    if request.method == 'POST':
        from django.db import transaction
        from .phase_g_forms import HealthBrandingForm
        from .public_services import audit_public
        try:
            data = json.loads(request.body.decode('utf-8')) if request.content_type == 'application/json' else request.POST
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'status': 'error', 'message': 'Dados de personalização inválidos.'}, status=400)
        if not isinstance(data, dict) and not hasattr(data, 'getlist'):
            return JsonResponse({'status': 'error', 'message': 'Dados de personalização inválidos.'}, status=400)
        form = HealthBrandingForm(data)
        if not form.is_valid():
            return JsonResponse({'status': 'error', 'message': 'Dados de personalização inválidos.', 'fields': list(form.errors)}, status=400)
        branding_dict = form.cleaned_data
        branding_dict['municipio_uf'] = f"{branding_dict['municipio']} - {branding_dict['uf']}"
        org = current_organization()
        with transaction.atomic():
            org.nome_fantasia = branding_dict['nome_institucional']
            org.cidade = branding_dict['municipio']
            org.uf = branding_dict['uf']
            org.save(update_fields=['nome_fantasia', 'cidade', 'uf'])
            perfil = PerfilPrefeitura.objects.filter(organization=org).first()
            if perfil:
                perfil.nome_institucional = branding_dict['nome_institucional']
                perfil.codigo_ibge = branding_dict['codigo_ibge']
                perfil.save(update_fields=['nome_institucional', 'codigo_ibge'])
            ubs = UnidadeSaudeMunicipal.objects.filter(organization=org).select_related('unidade').first()
            if ubs:
                ubs.unidade.nome = branding_dict['unidade_nome']
                ubs.unidade.save(update_fields=['nome'])
            audit_public(request.user, 'SAUDE_IDENTIDADE_ATUALIZADA', org, {'campos': sorted(form.cleaned_data)})
        request.session['vertyx_branding'] = branding_dict
        request.session['vertyx_branding_org'] = org.pk
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '') or request.content_type == 'application/json':
            return JsonResponse({'status': 'ok', 'branding': branding_dict})
        return render(request, 'rh_municipal/health_system_customization.html', {'branding': branding_dict, 'saved': True})

    branding = get_tenant_branding(request)
    return render(request, 'rh_municipal/health_system_customization.html', {'branding': branding})


def _parse_json_body(request):
    try:
        data = json.loads(request.body.decode('utf-8')) if request.content_type == 'application/json' else request.POST.dict()
    except (ValueError, UnicodeDecodeError):
        raise BadRequest('Corpo da requisição inválido.')
    if not isinstance(data, dict):
        raise BadRequest('Informe um objeto JSON.')
    return data


def _kiosk_org_and_unit(request):
    svc._require(request.user, 'saude.appointments.view')
    org = rt.resolve_health_organization(request)
    if not org or not public_module_available(org, 'saude_sus'):
        raise PermissionDenied('Saúde SUS não está contratada e ativa para esta Prefeitura.')
    unit_id = request.GET.get('unidade_id') or request.POST.get('unidade_id')
    unit = rt.resolve_health_unit(org, unit_id=unit_id, user=request.user)
    return org, unit


@never_cache
@login_required
@ensure_csrf_cookie
@require_GET
def health_call_panel_tv(request):
    """Tela Fullscreen para SmartTV de sala de espera conectada à fila real no banco."""
    org, unit = _kiosk_org_and_unit(request)
    branding = get_tenant_branding(request)
    snapshot = rt.list_queue_snapshot(org, unit, include_citizen_details=False)
    return render(request, 'rh_municipal/health_call_panel_tv.html', {
        'branding': branding,
        'organization': org,
        'snapshot': snapshot,
        'snapshot_json': json.dumps(snapshot, ensure_ascii=False),
    })


@never_cache
@login_required
@ensure_csrf_cookie
@require_GET
def health_totem(request):
    """Totem Touchscreen de Autoatendimento para emissão de senhas reais no banco."""
    org, unit = _kiosk_org_and_unit(request)
    branding = get_tenant_branding(request)
    snapshot = rt.list_queue_snapshot(org, unit, include_citizen_details=False)
    return render(request, 'rh_municipal/health_totem.html', {
        'branding': branding,
        'organization': org,
        'services': snapshot['services'],
        'snapshot_json': json.dumps(snapshot, ensure_ascii=False),
    })


@login_required
@require_http_methods(['GET', 'POST'])
def health_professionals_screen(request):
    """Tela de Gestão de Médicos e Profissionais de Saúde conectada ao cadastro real."""
    if not (_can(request.user, 'saude.professionals.view') or _can(request.user, 'saude.professionals.manage') or _can(request.user, 'saude.dashboard.view')):
        raise PermissionDenied('Acesso a profissionais de saúde não autorizado.')
    org = current_organization()
    branding = get_tenant_branding(request)
    profs = scoped_queryset(
        request.user,
        health.ProfissionalSaudeMunicipal.objects.filter(organization=org),
    ).select_related('servidor', 'unidade_saude__unidade', 'equipe').order_by('servidor__nome')[:100]
    context = _context(
        request, can_health_dashboard=True,
        can_health_professionals=True,
    )
    context.update({
        'branding': branding,
        'professionals': profs,
        'title': 'Profissionais de Saúde',
        'subtitle': 'Corpo clínico e equipes vinculados às Unidades de Saúde',
    })
    return render(request, 'rh_municipal/health_professionals.html', context)


@never_cache
@login_required
@ensure_csrf_cookie
@require_GET
def health_call_panel(request):
    """Console de Operação do Médico e Recepção da UBS sincronizado com banco real."""
    if not (_can(request.user, 'saude.dashboard.view') or _can(request.user, 'saude.appointments.view')):
        raise PermissionDenied('Acesso ao console de chamadas não autorizado.')
    org, unit = _kiosk_org_and_unit(request)
    branding = get_tenant_branding(request)
    snapshot = rt.list_queue_snapshot(org, unit, include_citizen_details=_can(request.user, 'saude.records.sensitive'))
    context = _context(request, can_health_dashboard=True, can_health_appointments=True)
    context.update({
        'title': 'Console de Chamadas UBS',
        'subtitle': 'Gerenciamento de fila em tempo real sincronizado com Banco de Dados e SmartTV',
        'branding': branding,
        'snapshot': snapshot,
        'snapshot_json': json.dumps(snapshot, ensure_ascii=False),
    })
    return render(request, 'rh_municipal/health_call_panel.html', context)


@never_cache
@login_required
@ensure_csrf_cookie
@require_GET
def health_acs_map(request):
    """Georreferenciamento territorial e visitas dos ACS com dados reais do banco."""
    if not (_can(request.user, 'saude.field.view') or _can(request.user, 'saude.dashboard.view')):
        raise PermissionDenied('Acesso ao georreferenciamento ACS não autorizado.')
    org = current_organization()
    branding = get_tenant_branding(request)
    map_data = rt.get_acs_map_snapshot(
        request.user, org,
        microarea=request.GET.get('microarea', ''),
        condition=request.GET.get('condicao', ''),
    )
    context = _context(request, can_health_dashboard=True, can_health_field=True)
    context.update({
        'title': 'Georreferenciamento ACS',
        'subtitle': 'Monitoramento territorial real de visitas domiciliares da Atenção Primária',
        'branding': branding,
        'map_data': map_data,
        'map_data_json': json.dumps(map_data, ensure_ascii=False),
    })
    return render(request, 'rh_municipal/health_acs_map.html', context)


@never_cache
@login_required
@ensure_csrf_cookie
@require_GET
def health_triage_manchester(request):
    """Estação de Triagem e Classificação de Risco Manchester conectada à fila real."""
    if not (_can(request.user, 'saude.records.view') or _can(request.user, 'saude.appointments.view') or _can(request.user, 'saude.dashboard.view')):
        raise PermissionDenied('Acesso à estação de triagem Manchester não autorizado.')
    svc._require(request.user, 'saude.records.sensitive')
    org, unit = _kiosk_org_and_unit(request)
    branding = get_tenant_branding(request)
    snapshot = rt.list_queue_snapshot(org, unit, include_citizen_details=_can(request.user, 'saude.records.sensitive'))
    citizens = scoped_queryset(
        request.user,
        health.CidadaoSaudeMunicipal.objects.filter(organization=org, situacao='ATIVO'),
    ).order_by('nome')[:100]
    context = _context(request, can_health_dashboard=True, can_health_records=True)
    context.update({
        'branding': branding,
        'snapshot': snapshot,
        'snapshot_json': json.dumps(snapshot, ensure_ascii=False),
        'citizens': citizens,
    })
    return render(request, 'rh_municipal/health_triage_manchester.html', context)


@login_required
@require_GET
def health_pec_soap(request):
    """Prontuário Eletrônico do Cidadão (PEC e-SUS) Atendimento Clínico SOAP com branding dinâmico."""
    svc._require(request.user, 'saude.records.sensitive')
    branding = get_tenant_branding(request)
    return render(request, 'rh_municipal/health_pec_soap.html', {'branding': branding})


@login_required
@require_GET
def health_pharmacy_dispensation(request):
    """Farmácia Básica Municipal - Dispensação de Medicamentos Hórus / e-SUS AF com branding dinâmico."""
    svc._require(request.user, 'saude.pharmacy.view')
    branding = get_tenant_branding(request)
    return render(request, 'rh_municipal/health_pharmacy_dispensation.html', {'branding': branding})


@never_cache
@login_required
@require_GET
def saude_queue_api(request):
    """JSON live polling endpoint for Totem, SmartTV, Medical Console and Manchester Triage."""
    org, unit = _kiosk_org_and_unit(request)
    snapshot = rt.list_queue_snapshot(org, unit, include_citizen_details=_can(request.user, 'saude.records.sensitive'))
    response = JsonResponse({'ok': True, 'organization': org.slug, **snapshot})
    response['Cache-Control'] = 'no-store'
    return response


@never_cache
@login_required
@require_POST
def saude_queue_issue_api(request):
    """Persist new queue ticket in DB from Kiosk Totem or reception."""
    org, unit = _kiosk_org_and_unit(request)
    svc._require(request.user, 'saude.appointments.manage')
    data = _parse_json_body(request)
    try:
        ticket = rt.issue_queue_ticket(
            org, unit,
            service_code=data.get('prefix') or data.get('servico') or 'CLN',
            priority_type='PRIORITARIA' if data.get('prioritaria') or (data.get('prefix') == 'PRE') else 'CONVENCIONAL',
            priority_detail=data.get('detalhe_prioridade', ''),
            cpf_cns=data.get('cpf_cns', ''),
            citizen_id=data.get('cidadao_id'),
            actor=request.user,
        )
    except (PermissionDenied, ValidationError) as exc:
        msg = '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        return JsonResponse({'ok': False, 'error': msg}, status=400)
    snapshot = rt.list_queue_snapshot(org, unit, include_citizen_details=_can(request.user, 'saude.records.sensitive'))
    return JsonResponse({
        'ok': True,
        'ticket': rt._serialize_ticket(ticket, include_citizen_details=_can(request.user, 'saude.records.sensitive')),
        'snapshot': snapshot,
    }, status=201)


@never_cache
@login_required
@require_POST
def saude_queue_action_api(request):
    """Authenticated transition endpoint for Console Médico & Recepção (CALL/RECALL/START/FINISH/NO_SHOW/TRANSFER)."""
    if not _can(request.user, 'saude.appointments.manage'):
        raise PermissionDenied('Permissão insuficiente para operar a fila clínica.')
    org, unit = _kiosk_org_and_unit(request)
    data = _parse_json_body(request)
    try:
        ticket = rt.transition_queue_ticket(
            request.user, org,
            action=data.get('action', ''),
            ticket_public_id=data.get('public_id'),
            local_id=data.get('local_id'),
            professional_id=data.get('profissional_id'),
            target_service_code=data.get('destino_codigo'),
        )
    except (PermissionDenied, ValidationError) as exc:
        msg = '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        return JsonResponse({'ok': False, 'error': msg}, status=400)
    snapshot = rt.list_queue_snapshot(org, unit, include_citizen_details=_can(request.user, 'saude.records.sensitive'))
    return JsonResponse({
        'ok': True,
        'ticket': rt._serialize_ticket(ticket, include_citizen_details=_can(request.user, 'saude.records.sensitive')),
        'snapshot': snapshot,
    })


@never_cache
@login_required
@require_POST
def saude_triage_save_api(request):
    """Save real Manchester Classification to DB and re-prioritize medical queue."""
    if not (_can(request.user, 'saude.records.create') and _can(request.user, 'saude.records.sensitive')):
        raise PermissionDenied('Permissão insuficiente para classificar risco Manchester.')
    org, unit = _kiosk_org_and_unit(request)
    data = _parse_json_body(request)
    try:
        tri = rt.save_manchester_triage(
            request.user, org,
            ticket_public_id=data.get('ticket_public_id'),
            citizen_id=data.get('cidadao_id'),
            nurse_id=data.get('enfermeiro_id'),
            flowchart=data.get('fluxograma', ''),
            discriminator=data.get('discriminador', ''),
            color=data.get('cor', 'VERDE'),
            vitals=data.get('vitals') or data,
            complaint=data.get('queixa_principal', ''),
        )
    except (PermissionDenied, ValidationError, ValueError) as exc:
        msg = '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        return JsonResponse({'ok': False, 'error': msg}, status=400)
    snapshot = rt.list_queue_snapshot(org, unit, include_citizen_details=_can(request.user, 'saude.records.sensitive'))
    return JsonResponse({
        'ok': True,
        'manchester_id': str(tri.public_id),
        'cor': tri.cor,
        'sla_min': tri.tempo_alvo_minutos,
        'snapshot': snapshot,
    }, status=201)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
def saude_acs_map_api(request):
    """Real database endpoint for ACS Georeferencing map & quick field visit logging."""
    if not (_can(request.user, 'saude.field.view') or _can(request.user, 'saude.dashboard.view')):
        raise PermissionDenied('Acesso territorial ACS não autorizado.')
    org = current_organization()
    if request.method == 'POST':
        svc._require(request.user, 'saude.field.manage')
        data = _parse_json_body(request)
        raw_cond = data.get('condicao') or data.get('condicao_acompanhada') or ''
        raw_alert = data.get('alerta') if data.get('alerta') is not None else data.get('alerta_epidemiologico', '')
        if isinstance(raw_alert, bool):
            alert_text = f'Alerta epidemiológico ({raw_cond or "Campo"})' if raw_alert else ''
        else:
            alert_text = str(raw_alert or '')
        raw_notes = data.get('observacoes') or data.get('observacao') or ''
        agent_id = data.get('agente_id')
        if not agent_id:
            with tenant_context(org):
                agent_id = (
                    health.ProfissionalSaudeMunicipal.objects.filter(
                        organization=org, servidor__usuario=request.user, ativo=True,
                    ).values_list('pk', flat=True).first()
                )
        try:
            visit = rt.register_acs_map_visit(
                request.user, org,
                family_id=data.get('familia_id'),
                agent_id=agent_id,
                condition=raw_cond,
                alert_text=alert_text,
                pa_sys=data.get('pa_sistolica'),
                pa_dia=data.get('pa_diastolica'),
                glicemia=data.get('glicemia'),
                peso=data.get('peso_kg'),
                lat=data.get('latitude'),
                lng=data.get('longitude'),
                notes=raw_notes,
            )
        except (PermissionDenied, ValidationError, ValueError) as exc:
            msg = '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        return JsonResponse({
            'ok': True,
            'visit_id': str(visit.public_id),
            'map': rt.get_acs_map_snapshot(request.user, org),
        }, status=201)

    snapshot = rt.get_acs_map_snapshot(
        request.user, org,
        microarea=request.GET.get('microarea', ''),
        condition=request.GET.get('condicao', ''),
    )
    return JsonResponse({'ok': True, **snapshot})


@never_cache
@login_required
@require_GET
def health_bi_panel(request):
    """Painel BI Saúde Municipal — Portaria GM/MS 3.493/2024 & Portaria 161 (dados reais agregados)."""
    org = current_organization()
    unit_id = request.GET.get('unidade_id')
    days = int(request.GET.get('dias') or 30)
    bi = rt.compute_health_bi_panel(request.user, org, unit_id=unit_id, days=days)

    fmt = (request.GET.get('format') or '').lower()
    if fmt == 'json':
        return JsonResponse({'ok': True, **bi})
    if fmt == 'csv':
        import csv
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="painel_bi_saude_3493.csv"'
        writer = csv.writer(response, delimiter=';')
        writer.writerow(['Indicador', 'Meta', 'Valor Atual', 'Status', 'Detalhamento'])
        for row in bi['portaria_3493']:
            writer.writerow([f"{row['codigo']} - {row['indicador']}", row['meta'], row['valor_atual'], row['status'], row['detalhe']])
        return response

    branding = get_tenant_branding(request)
    context = _context(request, can_health_dashboard=True)
    context.update({
        'title': 'Painel BI Saúde SUS (Portaria 3.493 / 161)',
        'subtitle': 'Indicadores agregados da Atenção Primária, Fila/Triagem Manchester, Farmácia e Leitos',
        'branding': branding,
        'bi': bi,
        'selected_unit_id': unit_id or '',
        'selected_days': days,
    })
    return render(request, 'rh_municipal/health_bi_panel.html', context)


@never_cache
@login_required
@ensure_csrf_cookie
@require_http_methods(['GET', 'POST'])
def health_beds_monitor(request):
    """Gestão de Leitos Hospitalares / UPA & Monitor Multiparamétrico em Tempo Real."""
    org = current_organization()
    if not (_can(request.user, 'saude.dashboard.view') or _can(request.user, 'saude.records.view')):
        raise PermissionDenied('Acesso ao mapa de leitos e monitor multiparamétrico não autorizado.')

    unit_id = request.GET.get('unidade_id') or request.POST.get('unidade_id')
    if request.method == 'POST':
        action = request.POST.get('action', '')
        try:
            if action == 'create_bed':
                rt.create_ward_bed_and_monitor(
                    request.user, org,
                    unit_id=request.POST.get('unidade_saude'),
                    ala_codigo=request.POST.get('ala_codigo', 'OBS'),
                    ala_nome=request.POST.get('ala_nome', 'Observação / Urgência'),
                    ala_tipo=request.POST.get('ala_tipo', 'OBSERVACAO'),
                    bed_codigo=request.POST.get('leito_codigo', ''),
                    bed_tipo=request.POST.get('tipo_leito', 'OBSERVACAO'),
                    serial_monitor=request.POST.get('serial_monitor', ''),
                    modelo_monitor=request.POST.get('modelo_monitor', 'Monitor Multiparamétrico 5P'),
                    protocolo=request.POST.get('protocolo', 'HTTP_JSON'),
                    endpoint_ip=request.POST.get('endpoint_ip', ''),
                )
                messages.success(request, 'Leito e monitor multiparamétrico cadastrados.')
            elif action == 'admit':
                rt.admit_patient_to_bed(
                    request.user, org,
                    bed_public_id=request.POST.get('bed_public_id'),
                    citizen_id=request.POST.get('cidadao_id'),
                    professional_id=request.POST.get('profissional_id') or None,
                    precaution=request.POST.get('precaucao', ''),
                )
                messages.success(request, 'Paciente admitido no leito com sucesso.')
            elif action == 'status':
                rt.transition_bed_status(
                    request.user, org,
                    bed_public_id=request.POST.get('bed_public_id'),
                    new_status=request.POST.get('new_status'),
                )
                messages.success(request, 'Situação do leito atualizada.')
            elif action == 'ack_alert':
                rt.acknowledge_monitor_alert(
                    request.user, org,
                    alert_public_id=request.POST.get('alert_public_id'),
                    conduta=request.POST.get('conduta', ''),
                )
                messages.success(request, 'Alarme clínico reconhecido e auditado.')
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        return redirect('rh_municipal_public:saude_beds_monitor')

    snapshot = rt.get_beds_monitor_snapshot(request.user, org, unit_id=unit_id)
    units = scoped_queryset(request.user, health.UnidadeSaudeMunicipal.objects.filter(organization=org, ativo=True)).select_related('unidade')
    citizens = scoped_queryset(request.user, health.CidadaoSaudeMunicipal.objects.filter(organization=org, situacao='ATIVO')).order_by('nome')[:100]
    profs = scoped_queryset(request.user, health.ProfissionalSaudeMunicipal.objects.filter(organization=org, ativo=True)).select_related('servidor')[:50]
    branding = get_tenant_branding(request)
    context = _context(request, can_health_dashboard=True, can_health_records=True, can_health_appointments=True)
    context.update({
        'title': 'Mapa de Leitos & Monitor Multiparamétrico',
        'subtitle': 'Monitoramento em tempo real de internação, observação UPA, sinais vitais e alarmes clínicos',
        'branding': branding,
        'snapshot': snapshot,
        'snapshot_json': json.dumps(snapshot, ensure_ascii=False),
        'units': units,
        'citizens': citizens,
        'professionals': profs,
    })
    return render(request, 'rh_municipal/health_beds_monitor.html', context)


@never_cache
@login_required
@require_GET
def saude_beds_monitor_api(request):
    """Live polling JSON endpoint for bedside monitor wallboard."""
    org = current_organization()
    snapshot = rt.get_beds_monitor_snapshot(request.user, org, unit_id=request.GET.get('unidade_id'))
    response = JsonResponse({'ok': True, **snapshot})
    response['Cache-Control'] = 'no-store'
    return response


@never_cache
@require_POST
def saude_beds_telemetry_api(request):
    """Ingest vital-signs telemetry via authenticated operator OR monitor gateway token."""
    data = _parse_json_body(request)
    auth_header = (request.headers.get('Authorization') or '').strip()
    bearer = auth_header[7:].strip() if auth_header.lower().startswith('bearer ') else ''
    token = (request.headers.get('X-Monitor-Token') or bearer or data.get('token_ingestao') or '').strip()
    org = current_organization()
    actor = request.user if request.user.is_authenticated else None

    if actor is not None:
        if not org or not public_module_available(org, 'saude_sus'):
            raise PermissionDenied('Módulo Saúde SUS inativo.')
        if not (_can(actor, 'saude.records.create') and _can(actor, 'saude.records.sensitive')):
            raise PermissionDenied('Permissão insuficiente para telemetria clínica.')
    elif token:
        if not getattr(settings, 'MUNICIPAL_MONITOR_GATEWAY_ENABLED', False):
            raise PermissionDenied('Gateway externo de monitores desabilitado.')
        mon = health.EquipamentoMonitorSaude.all_objects.filter(token_ingestao=rt.monitor_token_digest(token)).select_related('organization').first()
        if mon is None:
            return JsonResponse({'ok': False, 'error': 'Token de monitor inválido.'}, status=403)
        org = mon.organization
        data['monitor_serial'] = mon.serial_dispositivo
    else:
        return JsonResponse({'ok': False, 'error': 'Autenticação ou X-Monitor-Token obrigatório.'}, status=401)

    try:
        if data.get('action') == 'ack_alert' and actor is not None:
            al = rt.acknowledge_monitor_alert(actor, org, alert_public_id=data.get('alert_public_id'), conduta=data.get('conduta', ''))
            return JsonResponse({'ok': True, 'reconhecido': al.reconhecido})

        telem = rt.ingest_monitor_telemetry(
            org,
            monitor_id_or_serial=data.get('monitor_serial') or data.get('serial_dispositivo') or data.get('monitor_id'),
            fc_bpm=int(data['fc_bpm']) if data.get('fc_bpm') not in (None, '') else None,
            spo2_percent=int(data['spo2_percent']) if data.get('spo2_percent') not in (None, '') else None,
            pa_sistolica=int(data['pa_sistolica']) if data.get('pa_sistolica') not in (None, '') else None,
            pa_diastolica=int(data['pa_diastolica']) if data.get('pa_diastolica') not in (None, '') else None,
            fr_irpm=int(data['fr_irpm']) if data.get('fr_irpm') not in (None, '') else None,
            temperatura_c=data.get('temperatura_c'),
            etco2_mmhg=int(data['etco2_mmhg']) if data.get('etco2_mmhg') not in (None, '') else None,
            ritmo_ecg=data.get('ritmo_ecg', 'SINUSAL'),
            battery=data.get('bateria'),
            origin=data.get('origem_leitura', 'TELEMETRIA_REAL'),
            actor=actor,
        )
    except (PermissionDenied, ValidationError, ValueError) as exc:
        msg = '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        return JsonResponse({'ok': False, 'error': msg}, status=400)

    return JsonResponse({
        'ok': True,
        'telemetria_id': str(telem.public_id),
        'nivel_alerta': telem.nivel_alerta,
        'pam_mmhg': telem.pam_mmhg,
        'alertas': telem.alertas_detectados,
    }, status=201)
