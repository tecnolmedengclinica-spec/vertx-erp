"""Real database services for Track B:
- Login Único gov.br (OIDC PKCE + configurable Homologation/Local Demo flow)
- Citizen Portal Digital Inbox (Protocols, Ouvidoria, e-SIC, SUS scheduling)
- Municipal Tax Workflows (DAM w/ FEBRABAN Seg 8 barcode & EMV Pix, CND/CPEN certificates & public verification, ITBI parameterized assessment, NFS-e RPS XML preview).
"""
import base64
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import html
import json
import secrets
from urllib.parse import urlencode
import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from platform_core.context import current_organization, tenant_context
from platform_core.models import Organization

from . import phase_e_models as pe
from . import phase_f_models as pf
from . import phase_g_models as pg
from .phase_e_services import _audit as _audit_e
from .phase_e_services import _next_number, _save_immutable, public_module_available
from .phase_f_services import _audit as _audit_f
from .phase_f_services import _money, _require as _require_f, _require_record_scope
from .phase_g_services import _audit as _audit_g
from .phase_g_services import _fernet, _health_hierarchy


def _digits(value):
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


def _mask_cpf(cpf):
    d = _digits(cpf)
    if len(d) == 11:
        return f'***.{d[3:6]}.{d[6:9]}-**'
    if len(d) == 14:
        return f'**.{d[2:5]}.{d[5:8]}/{d[8:12]}-**'
    return d


# ---------------------------------------------------------------------------
# GOV.BR LOGIN ÚNICO (OIDC + PKCE + HOMOLOGAÇÃO LOCAL AUDITADA)
# ---------------------------------------------------------------------------

def require_local_govbr_demo(request):
    """Synthetic identities are never a substitute for a verified OIDC login."""
    host = request.get_host().split(':')[0]
    if not (getattr(settings, 'MUNICIPAL_GOVBR_DEMO_ENABLED', False)
            and settings.DEBUG and host in {'localhost', '127.0.0.1', 'testserver'}):
        raise PermissionDenied('Simulação de identidade disponível somente em testes locais isolados.')


def get_or_init_govbr_config(org):
    if not org:
        return None
    with tenant_context(org):
        cfg, _ = pe.ConfiguracaoGovBrMunicipal.objects.get_or_create(
            organization=org,
            defaults={
                'ambiente': pe.ConfiguracaoGovBrMunicipal.Ambiente.HOMOLOGACAO,
                'issuer_url': 'https://sso.staging.acesso.gov.br',
                'escopos': 'openid email profile govbr_confiabilidades',
                'nivel_minimo': pe.ConfiguracaoGovBrMunicipal.NivelMinimo.PRATA,
                'modo_homologacao_local': True,
                'ativo': True,
            },
        )
        return cfg


def build_govbr_authorize_flow(org, request):
    """Build OIDC Authorization Code + PKCE parameters and return target redirect URL."""
    require_local_govbr_demo(request)
    cfg = get_or_init_govbr_config(org)
    if not cfg.ativo or not cfg.modo_homologacao_local or cfg.ambiente != 'HOMOLOGACAO':
        raise PermissionDenied('Integração oficial gov.br ainda não homologada.')
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')

    request.session[f'govbr_oidc_{org.pk}'] = {
        'state': state,
        'nonce': nonce,
        'code_verifier': code_verifier,
    }
    request.session.modified = True

    if cfg.client_id and not cfg.modo_homologacao_local and cfg.issuer_url:
        redirect_uri = cfg.redirect_uri or request.build_absolute_uri(
            reverse('rh_municipal_public:public_govbr_callback', args=[org.slug])
        )
        params = urlencode({
            'response_type': 'code',
            'client_id': cfg.client_id,
            'scope': cfg.escopos,
            'redirect_uri': redirect_uri,
            'nonce': nonce,
            'state': state,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })
        return f"{cfg.issuer_url.rstrip('/')}/authorize?{params}", False

    local_url = reverse('rh_municipal_public:public_govbr_homolog', args=[org.slug])
    return f'{local_url}?state={state}', True


@transaction.atomic
def authenticate_govbr_citizen(org, request, *, cpf, nome, email='', telefone='', nivel='PRATA', origem='GOVBR_HOMOLOGACAO_LOCAL', sub=''):
    """Validate normalized citizen identity, persist SessaoCidadaoGovBr and bind session."""
    require_local_govbr_demo(request)
    cfg = get_or_init_govbr_config(org)
    if not cfg.ativo or not cfg.modo_homologacao_local or cfg.ambiente != 'HOMOLOGACAO' or origem != 'GOVBR_HOMOLOGACAO_LOCAL':
        raise PermissionDenied('Identidade oficial não verificada.')
    cpf_clean = _digits(cpf)
    if len(cpf_clean) != 11:
        raise ValidationError('Informe um CPF válido com 11 dígitos.')
    nome_clean = (nome or '').strip()
    if len(nome_clean) < 3:
        raise ValidationError('Informe o nome completo do cidadão.')
    nivel_clean = (nivel or 'PRATA').upper()
    if nivel_clean not in {'BRONZE', 'PRATA', 'OURO'}:
        nivel_clean = 'PRATA'

    selos = ['SIMULAÇÃO LOCAL — identidade não verificada']

    with tenant_context(org):
        sess = pe.SessaoCidadaoGovBr.objects.create(
            organization=org,
            cpf=cpf_clean,
            nome=nome_clean[:180],
            email=(email or '')[:254],
            telefone=(telefone or '')[:30],
            nivel_confiabilidade=nivel_clean,
            selos=selos,
            origem_autenticacao=origem[:30],
            sub_govbr=(sub or cpf_clean)[:120],
            expira_em=timezone.now() + timedelta(hours=8),
            ativo=True,
        )
        if hasattr(request, 'session'):
            request.session[f'citizen_govbr_{org.pk}'] = str(sess.public_id)
            request.session['citizen_last_org_slug'] = org.slug
            request.session.modified = True
        return sess


def get_active_govbr_session(org, request):
    if not org or not request or not hasattr(request, 'session'):
        return None
    try:
        require_local_govbr_demo(request)
    except PermissionDenied:
        return None
    pub_id = request.session.get(f'citizen_govbr_{org.pk}')
    if not pub_id:
        return None
    try:
        uid = uuid.UUID(str(pub_id))
    except (ValueError, TypeError):
        return None
    with tenant_context(org):
        sess = pe.SessaoCidadaoGovBr.objects.filter(
            organization=org, public_id=uid, ativo=True,
        ).first()
    if sess and sess.expira_em and sess.expira_em < timezone.now():
        return None
    return sess


def clear_govbr_session(org, request):
    if not org or not request or not hasattr(request, 'session'):
        return
    pub_id = request.session.pop(f'citizen_govbr_{org.pk}', None)
    request.session.modified = True
    if pub_id:
        try:
            uid = uuid.UUID(str(pub_id))
            with tenant_context(org):
                pe.SessaoCidadaoGovBr.objects.filter(organization=org, public_id=uid).update(ativo=False)
        except (ValueError, TypeError):
            pass


# ---------------------------------------------------------------------------
# PORTAL DO CIDADÃO CONECTADO AO BANCO REAL
# ---------------------------------------------------------------------------

def _resolve_portal_actor_fallback(org):
    """Find an active internal user belonging to org only for FK attribution of citizen self-service protocols."""
    from platform_core.models import OrganizationMembership
    membership = (
        OrganizationMembership.objects.filter(organization=org, status='ATIVO', user__is_active=True, role='ADMIN')
        .select_related('user')
        .order_by('-role', 'id')
        .first()
    )
    return membership.user if membership else None


@transaction.atomic
def create_citizen_digital_protocol(org, *, cpf, nome, email='', telefone='', assunto, descricao='', secretaria_id=None, user=None):
    """Persist real ProtocoloMunicipal + ProcessoDigitalMunicipal + ParteProcessoMunicipal for a citizen request."""
    cpf_clean = _digits(cpf)
    if len(cpf_clean) != 11:
        raise ValidationError('CPF válido com 11 dígitos é obrigatório para protocolar requerimento.')
    if not assunto or not assunto.strip():
        raise ValidationError('Informe o assunto do requerimento.')

    actor = user if (user and getattr(user, 'is_authenticated', False)) else _resolve_portal_actor_fallback(org)
    if actor is None:
        raise ValidationError('Nenhum operador municipal configurado na Prefeitura para receber protocolos externos.')

    with tenant_context(org):
        Organization.objects.select_for_update().get(pk=org.pk)
        year_p, proto_num = _next_number(org, pe.ContadorExpedienteMunicipal.Familia.PROTOCOLO)
        year_d, proc_num = _next_number(org, pe.ContadorExpedienteMunicipal.Familia.PROCESSO)

        sec = None
        if secretaria_id:
            from .models import Secretaria
            sec = Secretaria.objects.filter(organization=org, pk=secretaria_id, ativo=True).first()

        proto = pe.ProtocoloMunicipal.objects.create(
            organization=org,
            numero=proto_num,
            exercicio=year_p,
            origem=pe.ProtocoloMunicipal.Origem.PORTAL,
            assunto=assunto.strip()[:180],
            resumo=(descricao or assunto).strip(),
            interessado_nome=nome.strip()[:180],
            interessado_documento=cpf_clean,
            interessado_email=(email or '')[:254],
            secretaria=sec,
            criado_por=actor,
        )
        proc = pe.ProcessoDigitalMunicipal.objects.create(
            organization=org,
            numero=proc_num,
            exercicio=year_d,
            titulo=assunto.strip()[:200],
            assunto=assunto.strip()[:180],
            descricao=(descricao or '').strip(),
            status=pe.ProcessoDigitalMunicipal.Status.AUTUADO,
            prioridade=pe.ProcessoDigitalMunicipal.Prioridade.NORMAL,
            sigilo=pe.ProcessoDigitalMunicipal.Sigilo.INTERNO,
            protocolo=proto,
            secretaria=sec,
            criado_por=actor,
        )
        pe.ParteProcessoMunicipal.objects.create(
            organization=org,
            processo=proc,
            tipo=pe.ParteProcessoMunicipal.Tipo.INTERESSADO,
            nome=nome.strip()[:180],
            documento=cpf_clean,
            email=(email or '')[:254],
            telefone=(telefone or '')[:30],
        )
        _save_immutable(
            pe.TramiteProcessoMunicipal,
            organization=org,
            processo=proc,
            acao=pe.TramiteProcessoMunicipal.Acao.AUTUACAO,
            status_anterior='',
            status_novo=proc.status,
            secretaria_destino=sec,
            descricao=f'Requerimento protocolado pelo cidadão via Portal Digital ({_mask_cpf(cpf_clean)}).',
            registrado_por=actor,
        )
        _audit_e(actor, 'PROTOCOLO_CIDADAO_PORTAL', proc, changes={'numero': proc.numero, 'protocolo': proto.numero})
        return proc, proto


@transaction.atomic
def book_citizen_sus_appointment(org, *, cpf, nome, unit_id, procedure_code='0301010064', complaint='', user=None):
    """Create real citizen SUS appointment + queue ticket synced with UBS."""
    cpf_clean = _digits(cpf)
    if len(cpf_clean) != 11:
        raise ValidationError('CPF com 11 dígitos é obrigatório para agendamento SUS.')
    if not public_module_available(org, pg.HEALTH_MODULE):
        raise ValidationError('Módulo Saúde SUS não contratado nesta Prefeitura.')
    actor = user if (user and getattr(user, 'is_authenticated', False)) else _resolve_portal_actor_fallback(org)
    if actor is None:
        raise ValidationError('Organização sem profissional/operador base cadastrado.')

    with tenant_context(org):
        unit = pg.UnidadeSaudeMunicipal.objects.filter(organization=org, pk=unit_id, ativo=True).select_related('unidade').first()
        if unit is None:
            raise ValidationError('Nenhuma Unidade Básica de Saúde ativa disponível.')

        hier = _health_hierarchy(unit)
        citizen = pg.CidadaoSaudeMunicipal.objects.filter(organization=org, cpf=cpf_clean).first()
        if citizen is None:
            citizen = pg.CidadaoSaudeMunicipal.objects.create(
                organization=org,
                cpf=cpf_clean,
                nome=(nome or 'Cidadão Gov.br')[:180],
                municipio=getattr(org, 'cidade', '') or '',
                uf=getattr(org, 'uf', '') or '',
                situacao='ATIVO',
                **hier,
            )

        agenda = pg.AgendaSaudeMunicipal.objects.filter(
            organization=org, unidade_saude=unit, situacao='ABERTA', fim__gte=timezone.now(),
        ).order_by('inicio').first()

        appt = pg.AtendimentoSaudeMunicipal.objects.create(
            organization=org,
            cidadao=citizen,
            unidade_saude=unit,
            profissional=agenda.profissional if agenda else None,
            agenda=agenda,
            iniciado_em=agenda.inicio if agenda else timezone.now(),
            status=pg.AtendimentoSaudeMunicipal.Status.AGENDADO,
            codigo_procedimento=(procedure_code or '0301010064')[:32],
            queixa_portal_cifrada=_fernet().encrypt(str(complaint).encode('utf-8')) if complaint else b'',
            criado_por=actor,
            **hier,
        )
        from .saude_realtime_services import issue_queue_ticket
        ticket = issue_queue_ticket(
            org, unit, service_code='CLN', priority_type='CONVENCIONAL',
            priority_detail='Agendamento Portal Cidadão',
            citizen_id=citizen.pk, actor=actor,
        )
        ticket.atendimento_clinico = appt
        ticket.save(update_fields=['atendimento_clinico'])
        _audit_g(actor, org, 'AGENDAMENTO_PORTAL_CIDADAO', appt._meta.label_lower, appt.pk, patient_id=citizen.pk, details={'senha': ticket.codigo_formatado})
        return appt, ticket


def load_citizen_portal_data(org, citizen_cpf=None):
    """Query real processes, ombudsman, SUS schedules, tax liabilities, DAMs and CNDs from DB."""
    if not org:
        return {
            'open_processes': [], 'archived_processes': [],
            'ombudsman_rows': [], 'health_appointments': [],
            'taxpayer': None, 'properties': [], 'tax_launches': [],
            'dams': [], 'certificates': [], 'clearance': None,
            'health_units': [], 'secretarias': [],
        }

    with tenant_context(org):
        from .models import Secretaria
        cpf_clean = _digits(citizen_cpf) if citizen_cpf else ''
        open_statuses = {'AUTUADO', 'DISTRIBUIDO', 'EM_ANALISE', 'AGUARDANDO_PARECER', 'AGUARDANDO_ASSINATURA', 'PENDENTE_INTERESSADO'}

        if cpf_clean:
            procs = list(
                pe.ProcessoDigitalMunicipal.objects.filter(organization=org, partes__documento=cpf_clean)
                .select_related('secretaria', 'protocolo')
                .prefetch_related('tramitacoes', 'despachos')
                .distinct()
                .order_by('-criado_em')[:50]
            )
            omb = list(
                pe.ManifestacaoOuvidoriaMunicipal.objects.filter(organization=org, documento=cpf_clean)
                .order_by('-recebido_em')[:20]
            )
            appts = list(
                pg.AtendimentoSaudeMunicipal.objects.filter(organization=org, cidadao__cpf=cpf_clean)
                .select_related('unidade_saude__unidade', 'profissional__servidor', 'cidadao')
                .prefetch_related('senhas_vinculadas')
                .order_by('-pk')[:20]
            )
            taxpayer = pf.ContribuinteMunicipal.objects.filter(organization=org, documento=cpf_clean).first()
            if taxpayer:
                properties = list(pf.ImovelMunicipal.objects.filter(organization=org).filter(Q(proprietario=taxpayer) | Q(possuidor=taxpayer)).order_by('inscricao')[:30])
                launches = list(pf.LancamentoTributarioMunicipal.objects.filter(organization=org, contribuinte=taxpayer).select_related('imovel', 'regra').prefetch_related('parcelas', 'guias_dam').order_by('-exercicio', '-pk')[:40])
                dams = list(pf.GuiaArrecadacaoDAM.objects.filter(organization=org, contribuinte=taxpayer).select_related('lancamento').order_by('-emitida_em')[:30])
                certs = list(pf.CertidaoTributariaMunicipal.objects.filter(organization=org, contribuinte=taxpayer).order_by('-emitida_em')[:20])
                clearance = verify_taxpayer_clearance(org, taxpayer)
            else:
                properties, launches, dams, certs, clearance = [], [], [], [], None
        else:
            # Preview mode without active citizen login: never expose external citizens' private records
            procs, omb, appts = [], [], []
            taxpayer, properties, launches, dams, certs, clearance = None, [], [], [], [], None

        health_units = list(
            pg.UnidadeSaudeMunicipal.objects.filter(organization=org, ativo=True)
            .select_related('unidade')
            .order_by('unidade__nome')
        )
        secretarias = list(Secretaria.objects.filter(organization=org, ativo=True).order_by('nome'))

        return {
            'open_processes': [p for p in procs if p.status in open_statuses],
            'archived_processes': [p for p in procs if p.status not in open_statuses],
            'ombudsman_rows': omb,
            'health_appointments': appts,
            'taxpayer': taxpayer,
            'properties': properties,
            'tax_launches': launches,
            'dams': dams,
            'certificates': certs,
            'clearance': clearance,
            'health_units': health_units,
            'secretarias': secretarias,
        }


# ---------------------------------------------------------------------------
# TRIBUTÁRIO MUNICIPAL REAL (DAM FEBRABAN/PIX, CND/CPEN, ITBI, NFS-e PREVIEW)
# ---------------------------------------------------------------------------

def _mod10(num_str):
    total = 0
    weight = 2
    for ch in reversed(num_str):
        val = int(ch) * weight
        total += (val // 10) + (val % 10)
        weight = 1 if weight == 2 else 2
    rem = total % 10
    return 0 if rem == 0 else (10 - rem)


def _crc16_ccitt(payload):
    crc = 0xFFFF
    for ch in payload.encode('utf-8'):
        crc ^= (ch << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f'{crc:04X}'


def build_febraban_segmento8_and_pix(org, *, valor_decimal, vencimento, nosso_numero):
    """Build valid 44-digit FEBRABAN Seg 8 (Arrecadação Prefeituras) barcode, 48-digit linha digitável, and EMV Pix."""
    cents = int((_money(valor_decimal) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    val_11 = f'{cents % 100000000000:011d}'
    cnpj_base = (_digits(getattr(org, 'cnpj', '')) or '00000000000000')[:8].zfill(8)
    due_str = vencimento.strftime('%Y%m%d')
    nosso_digits = _digits(nosso_numero).zfill(13)[-13:]
    # 816 -> Product 8 (Arrecadação), Segment 1 (Prefeituras), Valor Efetivo 6 (Mod 10)
    raw_without_dv = '816' + val_11 + cnpj_base + due_str + nosso_digits
    raw_without_dv = raw_without_dv[:43].ljust(43, '0')
    dv_geral = str(_mod10(raw_without_dv))
    barcode44 = raw_without_dv[:3] + dv_geral + raw_without_dv[3:]

    blocks = []
    for i in range(0, 44, 11):
        blk = barcode44[i:i + 11]
        blocks.append(f'{blk}-{_mod10(blk)}')
    linha = ' '.join(blocks)

    amt_str = f'{_money(valor_decimal):.2f}'
    city = (getattr(org, 'cidade', '') or 'MUNICIPIO')[:15].upper()
    merchant = (getattr(org, 'nome_fantasia', '') or 'PREFEITURA MUNICIPAL')[:25].upper()
    pix_key = _digits(getattr(org, 'cnpj', '')) or '00000000000100'
    mai = f'0014br.gov.bcb.pix01{len(pix_key):02d}{pix_key}'
    txid = (_digits(nosso_numero) or 'DAM001')[:20]
    add_data = f'05{len(txid):02d}{txid}'
    emv_body = (
        '000201'
        f'26{len(mai):02d}{mai}'
        '52040000'
        '5303986'
        f'54{len(amt_str):02d}{amt_str}'
        '5802BR'
        f'59{len(merchant):02d}{merchant}'
        f'60{len(city):02d}{city}'
        f'62{len(add_data):02d}{add_data}'
        '6304'
    )
    pix_payload = emv_body + _crc16_ccitt(emv_body)
    return barcode44, linha, pix_payload


@transaction.atomic
def emit_dam(org, *, launch_id, installment_id=None, actor=None):
    """Issue real DAM (Guia de Arrecadação Municipal) persisted in DB with barcode & Pix."""
    if not public_module_available(org, pf.PHASE_F_MODULES['tributos']):
        raise PermissionDenied('Módulo de Tributos não ativo nesta Prefeitura.')
    with tenant_context(org):
        launch = pf.LancamentoTributarioMunicipal.objects.select_for_update().filter(
            organization=org, pk=launch_id,
        ).select_related('contribuinte').first()
        if launch is None:
            raise ValidationError('Lançamento tributário não encontrado nesta Prefeitura.')
        if actor and actor.is_authenticated:
            _require_record_scope(actor, 'tributo.view', pf.PHASE_F_MODULES['tributos'], launch)

        parcela = None
        if installment_id:
            parcela = pf.ParcelaTributariaMunicipal.objects.filter(organization=org, lancamento=launch, pk=installment_id).first()
            if parcela is None:
                raise ValidationError('Parcela não encontrada para o lançamento.')
            paid = sum((p.valor for p in parcela.pagamentos.filter(status='REGISTRADO')), Decimal('0'))
            open_amount = max(Decimal('0.00'), parcela.valor - paid)
            due = parcela.vencimento
        else:
            paid = sum((p.valor for part in launch.parcelas.all() for p in part.pagamentos.filter(status='REGISTRADO')), Decimal('0'))
            open_amount = max(Decimal('0.00'), launch.valor_total - paid)
            due = launch.vencimento

        if open_amount <= 0:
            raise ValidationError('Este lançamento ou parcela já está integralmente quitado.')

        num_dam = f'DAM-{launch.exercicio}-{uuid.uuid4().hex[:10].upper()}'
        barcode44, linha, pix_code = build_febraban_segmento8_and_pix(
            org, valor_decimal=open_amount, vencimento=due, nosso_numero=num_dam,
        )
        dam = pf.GuiaArrecadacaoDAM.objects.create(
            organization=org,
            numero_dam=num_dam,
            contribuinte=launch.contribuinte,
            lancamento=launch,
            parcela=parcela,
            vencimento=due,
            valor_principal=open_amount,
            valor_acrescimos=Decimal('0.00'),
            valor_desconto=Decimal('0.00'),
            valor_total=open_amount,
            codigo_barras=barcode44,
            linha_digitavel=linha,
            pix_copia_cola=pix_code,
            status=pf.GuiaArrecadacaoDAM.Status.EMITIDA,
            emitida_por=actor if (actor and getattr(actor, 'is_authenticated', False)) else None,
            secretaria_id=launch.secretaria_id,
            departamento_id=launch.departamento_id,
            setor_id=launch.setor_id,
            unidade_id=launch.unidade_id,
        )
        _audit_f(actor if (actor and getattr(actor, 'is_authenticated', False)) else None, org,
                 'DAM_EMITIDO', dam._meta.label_lower, dam.pk,
                 {'numero_dam': dam.numero_dam, 'lancamento': launch.numero, 'valor': str(open_amount)})
        return dam


def verify_taxpayer_clearance(org, contribuinte):
    """Deterministically inspect DB tax launches & active debt to decide CND vs CPEN vs POSITIVA."""
    today = timezone.localdate()
    with tenant_context(org):
        active_debts = list(
            pf.InscricaoDividaAtivaMunicipal.objects.filter(
                organization=org, contribuinte=contribuinte, status__in=['INSCRITA', 'PARCELADA'],
            )
        )
        open_launches = list(
            pf.LancamentoTributarioMunicipal.objects.filter(
                organization=org, contribuinte=contribuinte, status__in=['ABERTO', 'PARCIAL'],
            ).prefetch_related('parcelas__pagamentos')
        )
        overdue_launches = []
        not_due_launches = []
        total_overdue = Decimal('0.00')
        for ln in open_launches:
            paid = sum((p.valor for part in ln.parcelas.all() for p in part.pagamentos.filter(status='REGISTRADO')), Decimal('0'))
            saldo = max(Decimal('0.00'), ln.valor_total - paid)
            if saldo <= 0:
                continue
            if ln.vencimento < today:
                overdue_launches.append({'numero': ln.numero, 'tributo': ln.tributo, 'saldo': str(saldo)})
                total_overdue += saldo
            else:
                not_due_launches.append({'numero': ln.numero, 'tributo': ln.tributo, 'saldo': str(saldo)})

        unpaid_da = [d for d in active_debts if d.status == 'INSCRITA']
        parceled_da = [d for d in active_debts if d.status == 'PARCELADA']

        if unpaid_da or overdue_launches:
            tipo = pf.CertidaoTributariaMunicipal.Tipo.POSITIVA
        elif parceled_da:
            tipo = pf.CertidaoTributariaMunicipal.Tipo.CPEN
        else:
            tipo = pf.CertidaoTributariaMunicipal.Tipo.CND

        return {
            'tipo_elegivel': tipo,
            'possui_debito_vencido': bool(unpaid_da or overdue_launches),
            'debitos_vencidos': overdue_launches,
            'debitos_a_vencer': not_due_launches,
            'divida_ativa_ativa': [{'numero': d.numero, 'status': d.status, 'valor': str(d.valor_inscrito)} for d in active_debts],
            'total_vencido': str(_money(total_overdue + sum((d.valor_inscrito for d in unpaid_da), Decimal('0')))),
        }


@transaction.atomic
def issue_tax_certificate(org, *, contribuinte_id, imovel_id=None, finalidade='Fins Gerais e Licitações', actor=None, validity_days=90):
    """Issue CND, CPEN or Positiva certificate directly from real taxpayer ledger in DB."""
    if not public_module_available(org, pf.PHASE_F_MODULES['tributos']):
        raise PermissionDenied('Módulo Tributário não ativo nesta Prefeitura.')
    with tenant_context(org):
        contrib = pf.ContribuinteMunicipal.objects.filter(organization=org, pk=contribuinte_id).first()
        if contrib is None:
            raise ValidationError('Contribuinte não encontrado nesta Prefeitura.')
        imovel = None
        if imovel_id:
            imovel = pf.ImovelMunicipal.objects.filter(organization=org, pk=imovel_id).first()
        status_info = verify_taxpayer_clearance(org, contrib)
        tipo = status_info['tipo_elegivel']
        year = timezone.localdate().year
        numero = f'{tipo}-{year}-{uuid.uuid4().hex[:8].upper()}'
        raw_token = uuid.uuid4().hex[:16].upper()
        auth_code = f'{raw_token[:4]}-{raw_token[4:8]}-{raw_token[8:12]}-{raw_token[12:16]}'
        cert = pf.CertidaoTributariaMunicipal.objects.create(
            organization=org,
            numero=numero,
            tipo=tipo,
            contribuinte=contrib,
            imovel=imovel,
            codigo_autenticacao=auth_code,
            finalidade=(finalidade or 'Fins Gerais')[:140],
            situacao_snapshot=status_info,
            valida_ate=timezone.localdate() + timedelta(days=int(validity_days)),
            status=pf.CertidaoTributariaMunicipal.Status.VALIDA,
            emitida_por=actor if (actor and getattr(actor, 'is_authenticated', False)) else None,
            secretaria_id=contrib.secretaria_id,
            departamento_id=contrib.departamento_id,
            setor_id=contrib.setor_id,
            unidade_id=contrib.unidade_id,
        )
        _audit_f(actor if (actor and getattr(actor, 'is_authenticated', False)) else None, org,
                 'CERTIDAO_TRIBUTARIA_EMITIDA', cert._meta.label_lower, cert.pk,
                 {'numero': cert.numero, 'tipo': cert.tipo, 'autenticacao': cert.codigo_autenticacao})
        return cert


def validate_tax_certificate_public(org, *, numero='', codigo_autenticacao=''):
    """Public verification of municipal CND/CPEN authenticity code without exposing sensitive items."""
    num_clean = (numero or '').strip().upper()
    code_clean = (codigo_autenticacao or '').strip().upper()
    if not org or not code_clean:
        return None
    with tenant_context(org):
        qs = pf.CertidaoTributariaMunicipal.objects.filter(organization=org).select_related('contribuinte')
        if code_clean:
            qs = qs.filter(codigo_autenticacao=code_clean)
        if num_clean:
            qs = qs.filter(numero=num_clean)
        cert = qs.first()
    if cert is None:
        return {'encontrada': False}
    is_valid = cert.status == pf.CertidaoTributariaMunicipal.Status.VALIDA and cert.valida_ate >= timezone.localdate()
    return {
        'encontrada': True,
        'valida': is_valid,
        'numero': cert.numero,
        'tipo': cert.get_tipo_display(),
        'contribuinte_nome': cert.contribuinte.nome,
        'contribuinte_documento_mascarado': _mask_cpf(cert.contribuinte.documento),
        'codigo_autenticacao': cert.codigo_autenticacao,
        'emitida_em': timezone.localtime(cert.emitida_em).strftime('%d/%m/%Y %H:%M'),
        'valida_ate': cert.valida_ate.strftime('%d/%m/%Y'),
        'status': 'AUTÊNTICA E VÁLIDA' if is_valid else 'EXPIRADA OU REVOGADA',
    }


@transaction.atomic
def evaluate_itbi_transfer(actor, *, property_id, seller_id, buyer_id, valor_venal, valor_transacao, rule_id, registry_office=''):
    """Assess real property transfer ITBI strictly using parameterized active RegraTributariaMunicipal ('ITBI')."""
    org = _require_f(actor, 'tributo.manage', pf.PHASE_F_MODULES['tributos'])
    prop = pf.ImovelMunicipal.objects.filter(organization=org, pk=property_id).first()
    seller = pf.ContribuinteMunicipal.objects.filter(organization=org, pk=seller_id).first()
    buyer = pf.ContribuinteMunicipal.objects.filter(organization=org, pk=buyer_id).first()
    if not prop or not seller or not buyer:
        raise ValidationError('Imóvel, transmitente ou adquirente não encontrado nesta Prefeitura.')
    rule = pf.RegraTributariaMunicipal.objects.filter(
        organization=org, pk=rule_id, tributo=pf.RegraTributariaMunicipal.Tributo.ITBI, ativa=True,
    ).first()
    if rule is None:
        raise ValidationError('Selecione uma regra ativa parametrizada de ITBI.')
    rate = rule.parametros.get('aliquota_percentual')
    if rate is None:
        raise ValidationError('A regra de ITBI não possui alíquota parametrizada.')

    venal = _money(valor_venal)
    trans = _money(valor_transacao)
    base_itbi = max(venal, trans)
    percent = Decimal(str(rate))
    tax_amount = _money(base_itbi * percent / Decimal('100'))
    year = timezone.localdate().year
    due = timezone.localdate() + timedelta(days=30)

    # Note: LancamentoTributarioMunicipal.clean() checks taxpayer is owner/possessor if property is set;
    # for buyer assessment before deed transfer, associate property when buyer is possessor/owner, else link via ITBI record.
    launch_prop = prop if prop.proprietario_id == buyer.pk or prop.possuidor_id == buyer.pk else None
    launch = pf.LancamentoTributarioMunicipal.objects.create(
        organization=org,
        numero=f'ITBI-{year}-{uuid.uuid4().hex[:10].upper()}',
        tributo=pf.RegraTributariaMunicipal.Tributo.ITBI,
        exercicio=year,
        competencia=f'{timezone.localdate():%Y-%m}',
        contribuinte=buyer,
        imovel=launch_prop,
        regra=rule,
        regra_snapshot={**rule.parametros, 'codigo': rule.codigo, 'versao': rule.versao, 'imovel_inscricao': prop.inscricao},
        base_calculo=base_itbi,
        aliquota_percentual=percent,
        retencao=Decimal('0.00'),
        valor_total=tax_amount,
        vencimento=due,
        criado_por=actor,
        secretaria_id=prop.secretaria_id,
        departamento_id=prop.departamento_id,
        setor_id=prop.setor_id,
        unidade_id=prop.unidade_id,
    )
    itbi = pf.SolicitacaoITBIMunicipal.objects.create(
        organization=org,
        numero=f'PROC-ITBI-{year}-{uuid.uuid4().hex[:8].upper()}',
        imovel=prop,
        transmitente=seller,
        adquirente=buyer,
        valor_venal=venal,
        valor_transacao=trans,
        regra=rule,
        valor_itbi=tax_amount,
        cartorio_matricula=(registry_office or prop.matricula or '')[:100],
        lancamento=launch,
        status=pf.SolicitacaoITBIMunicipal.Status.AVALIADO,
        criado_por=actor,
        secretaria_id=prop.secretaria_id,
        departamento_id=prop.departamento_id,
        setor_id=prop.setor_id,
        unidade_id=prop.unidade_id,
    )
    dam = emit_dam(org, launch_id=launch.pk, actor=actor)
    _audit_f(actor, org, 'ITBI_AVALIADO', itbi._meta.label_lower, itbi.pk,
             {'numero': itbi.numero, 'imovel': prop.inscricao, 'valor_itbi': str(tax_amount), 'dam': dam.numero_dam})
    return itbi, dam


def build_nfse_rps_preview(org, note):
    """Render ABRASF / Padrão Nacional RPS XML preview using tenant parameterized ISS rule without transmitting."""
    rule = pf.RegraTributariaMunicipal.objects.filter(
        organization=org, tributo=pf.RegraTributariaMunicipal.Tributo.ISS, ativa=True,
    ).order_by('-versao').first()
    aliquota = Decimal(str(rule.parametros.get('aliquota_percentual'))) if (rule and rule.parametros.get('aliquota_percentual') is not None) else None
    valor_iss = _money(note.valor_servico * aliquota / Decimal('100')) if aliquota is not None else None
    xml_preview = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<CompNfseRps xmlns="http://www.abrasf.org.br/nfse.xsd">\n'
        f'  <InfDeclaracaoPrestacaoServico>\n'
        f'    <Rps><IdentificacaoRps><Numero>{html.escape(note.numero_interno)}</Numero><Tipo>1</Tipo></IdentificacaoRps>'
        f'<Competencia>{html.escape(note.competencia)}</Competencia></Rps>\n'
        f'    <Prestador><CpfCnpj>{html.escape(note.prestador.documento)}</CpfCnpj>'
        f'<RazaoSocial>{html.escape(note.prestador.nome)}</RazaoSocial></Prestador>\n'
        f'    <Tomador><CpfCnpj>{html.escape(note.tomador.documento)}</CpfCnpj>'
        f'<RazaoSocial>{html.escape(note.tomador.nome)}</RazaoSocial></Tomador>\n'
        f'    <Servico><ItemListaServico>{html.escape(note.codigo_servico)}</ItemListaServico>'
        f'<Discriminacao>{html.escape(note.descricao[:200])}</Discriminacao>'
        f'<Valores><ValorServicos>{note.valor_servico:.2f}</ValorServicos>'
        + (f'<Aliquota>{aliquota:.2f}</Aliquota><ValorIss>{valor_iss:.2f}</ValorIss>' if aliquota is not None else '<Observacao>Regra ISS pendente de parametrização</Observacao>')
        + f'</Valores></Servico>\n'
        f'  </InfDeclaracaoPrestacaoServico>\n'
        f'</CompNfseRps>'
    )
    return {
        'aliquota_percentual': str(aliquota) if aliquota is not None else 'Não parametrizada',
        'valor_iss': f'{valor_iss:.2f}' if valor_iss is not None else 'Pendente de regra ISS',
        'regra_codigo': rule.codigo if rule else 'Nenhuma regra ISS ativa',
        'xml': xml_preview,
    }
