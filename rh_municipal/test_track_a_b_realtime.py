import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

from cryptography.fernet import Fernet
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from platform_core.context import tenant_context
from platform_core.models import ERPProductModule, Organization, OrganizationERP
from platform_core.saas_models import AssinaturaSaaS, PlanoProdutoModulo, PlanoSaaS
from . import cidadao_tributario_services as ct_svc
from . import phase_e_models as pe
from . import phase_f_models as pf
from . import phase_f_services as pf_svc
from . import phase_g_models as pg
from . import saude_realtime_services as rt_svc
from .models import AcessoMunicipal, OrganizationMembership, Secretaria, Servidor, UnidadeMunicipal
from .tests import MunicipalFixtures


class TrackABFixtures(MunicipalFixtures):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

    def setUp(self):
        super().setUp()
        codes = [
            ('gestao_publica', 'Gestão Pública', 1),
            ('saude_sus', 'Saúde SUS', 130),
            ('processo_digital', 'Processo Digital', 80),
            ('tributos_municipais', 'Tributos Municipais', 100),
            ('gestao_territorial', 'Gestão Territorial', 110),
            ('nfse_municipal', 'NFS-e Municipal', 120),
        ]
        self.plan = PlanoSaaS.objects.create(
            slug=f'track-ab-{uuid.uuid4().hex[:8]}',
            nome='Plano Completo Track A+B',
            erp=self.publico,
            modulos=[],
        )
        for code, name, order in codes:
            mod, _ = ERPProductModule.objects.get_or_create(
                erp=self.publico, code=code,
                defaults={'name': name, 'active': True, 'sort_order': order},
            )
            PlanoProdutoModulo.objects.get_or_create(plano=self.plan, modulo=mod)

        AssinaturaSaaS.objects.create(organization=self.c, plano=self.plan, status=AssinaturaSaaS.Status.ACTIVE)
        with tenant_context(self.c):
            self.access = AcessoMunicipal.objects.create(
                membership=self.admin_c,
                papel=AcessoMunicipal.Papel.ADMIN_PREFEITURA,
                escopo=AcessoMunicipal.Escopo.PREFEITURA,
                atribuido_por=self.admin_c.user,
            )
            self.health_unit = pg.UnidadeSaudeMunicipal.objects.create(
                organization=self.c, unidade=self.structure[3], tipo='UBS', cnes='2654321',
            )
            self.team = pg.EquipeSaudeMunicipal.objects.create(
                organization=self.c,
                codigo=f'ESF-{uuid.uuid4().hex[:6]}',
                nome='ESF Central 01',
                tipo='ESF',
                unidade_saude=self.health_unit,
                microarea='01',
                secretaria=self.structure[0],
                departamento=self.structure[1],
                setor=self.structure[2],
                unidade=self.structure[3],
            )
            self.server.usuario = self.admin_c.user
            self.server.save(update_fields=['usuario'])
            self.professional = pg.ProfissionalSaudeMunicipal.objects.create(
                organization=self.c,
                servidor=self.server,
                unidade_saude=self.health_unit,
                equipe=self.team,
                ocupacao='Enfermeiro',
                conselho='COREN',
                registro_conselho='142890',
                especialidade='Atenção Primária',
                ativo=True,
                secretaria=self.structure[0],
                departamento=self.structure[1],
                setor=self.structure[2],
                unidade=self.structure[3],
            )

        # Second active municipality to validate cross-tenant isolation
        self.org2 = Organization.objects.create(
            slug=f'pm-nova-canaa-{uuid.uuid4().hex[:6]}',
            razao_social='Prefeitura Municipal de Nova Canaã',
            nome_fantasia='Prefeitura de Nova Canaã',
            cidade='Nova Canaã',
            uf='SE',
            status=Organization.Status.ACTIVE,
        )
        OrganizationERP.objects.create(organization=self.org2, erp=self.publico)
        AssinaturaSaaS.objects.create(organization=self.org2, plano=self.plan, status=AssinaturaSaaS.Status.ACTIVE)



@override_settings(MUNICIPAL_GOVBR_DEMO_ENABLED=True, MUNICIPAL_MONITOR_GATEWAY_ENABLED=True, DEBUG=True, MUNICIPAL_HEALTH_ENCRYPTION_KEY=Fernet.generate_key().decode())
class TrackABRealtimeTests(TrackABFixtures):
    # ------------------------------------------------------------------
    # TRACK A: SAÚDE (FILA / TOTEM / SMARTTV / MANCHESTER / ACS / BI / LEITOS)
    # ------------------------------------------------------------------
    def test_totem_queue_smarttv_console_manchester_flow(self):
        with tenant_context(self.c):
            citizen = pg.CidadaoSaudeMunicipal.objects.create(
                organization=self.c,
                nome='Paciente Emergência Teste',
                cpf='55544433322',
            )
        # 1. Totem view + Ticket issuance via API
        r_totem = self.client.get(reverse('rh_municipal_public:saude_totem'))
        self.assertEqual(r_totem.status_code, 200)

        r1 = self.client.post(
            reverse('rh_municipal_public:saude_queue_issue_api'),
            data=json.dumps({'prefix': 'GER', 'prioritaria': False, 'cidadao_id': citizen.pk}),
            content_type='application/json',
        )
        self.assertEqual(r1.status_code, 201)
        t_ger = r1.json()['ticket']

        r2 = self.client.post(
            reverse('rh_municipal_public:saude_queue_issue_api'),
            data=json.dumps({'prefix': 'PRE', 'prioritaria': True, 'detalhe_prioridade': 'Idoso 80+'}),
            content_type='application/json',
        )
        self.assertEqual(r2.status_code, 201)
        t_pre = r2.json()['ticket']

        # SmartTV polls queue snapshot: priority ticket PRE comes ahead of GER initially
        r_tv = self.client.get(reverse('rh_municipal_public:saude_call_panel_tv'))
        self.assertEqual(r_tv.status_code, 200)
        snap = self.client.get(reverse('rh_municipal_public:saude_queue_api')).json()
        waiting_codes = [x['codigo'] for x in snap['waiting']]
        self.assertEqual(waiting_codes[0], t_pre['codigo'])
        self.assertEqual(waiting_codes[1], t_ger['codigo'])

        # 2. Manchester triage classifies GER as LARANJA (weight 2) -> jumps ahead of un-triaged PRE (weight 3)
        r_tri = self.client.post(
            reverse('rh_municipal_public:saude_triage_save_api'),
            data=json.dumps({
                'ticket_public_id': t_ger['public_id'],
                'fluxograma': 'Dispneia Aguda',
                'discriminador': 'SpO2 < 90%',
                'cor': 'LARANJA',
                'saturacao_o2': 88,
                'pa_sistolica': 160,
                'pa_diastolica': 100,
                'queixa_principal': 'Falta de ar súbita',
            }),
            content_type='application/json',
        )
        self.assertEqual(r_tri.status_code, 201)
        snap_after = r_tri.json()['snapshot']
        self.assertEqual(snap_after['waiting'][0]['codigo'], t_ger['codigo'])
        self.assertEqual(snap_after['waiting'][0]['manchester_cor'], 'LARANJA')

        # 3. Doctor console transitions CALL_NEXT -> START -> FINISH
        action_url = reverse('rh_municipal_public:saude_queue_action_api')
        call_res = self.client.post(action_url, data=json.dumps({'action': 'CALL_NEXT'}), content_type='application/json')
        self.assertEqual(call_res.status_code, 200)
        self.assertEqual(call_res.json()['ticket']['status'], 'CHAMADA')

        start_res = self.client.post(action_url, data=json.dumps({'action': 'START', 'public_id': t_ger['public_id']}), content_type='application/json')
        self.assertEqual(start_res.json()['ticket']['status'], 'EM_ATENDIMENTO')

        fin_res = self.client.post(action_url, data=json.dumps({'action': 'FINISH', 'public_id': t_ger['public_id']}), content_type='application/json')
        self.assertEqual(fin_res.json()['ticket']['status'], 'ENCERRADA')

        # DB row verified + cross-tenant isolation on org2
        with tenant_context(self.c):
            self.assertTrue(pg.SenhaFilaAtendimento.objects.filter(public_id=t_ger['public_id'], status='ENCERRADA').exists())
        with tenant_context(self.org2):
            self.assertFalse(pg.SenhaFilaAtendimento.objects.filter(organization=self.org2, public_id=t_ger['public_id']).exists())

    @override_settings(MUNICIPAL_HEALTH_ENCRYPTION_KEY=Fernet.generate_key().decode())
    def test_acs_map_real_persistence_and_bi_panel(self):
        with tenant_context(self.c):
            fam = pg.FamiliaTerritorialSaude.objects.create(
                organization=self.c,
                codigo='FAM-BG-001',
                unidade_saude=self.health_unit,
                equipe=self.team,
                microarea='01',
                responsavel_nome='Maria Santos Silva',
                endereco='Rua do Porto, 42',
                latitude=Decimal('-10.427000'),
                longitude=Decimal('-36.472000'),
                risco_territorial='MEDIO',
                condicoes_sentinelas=['HIPERTENSAO'],
                secretaria=self.structure[0],
                departamento=self.structure[1],
                setor=self.structure[2],
                unidade=self.structure[3],
            )
        r_map = self.client.get(reverse('rh_municipal_public:saude_acs_map'))
        self.assertEqual(r_map.status_code, 200)

        r_api_get = self.client.get(reverse('rh_municipal_public:saude_acs_map_api'))
        self.assertEqual(r_api_get.status_code, 200)
        self.assertGreaterEqual(len(r_api_get.json()['houses']), 1)

        r_api_post = self.client.post(
            reverse('rh_municipal_public:saude_acs_map_api'),
            data=json.dumps({
                'familia_id': fam.pk,
                'pa_sistolica': 155,
                'pa_diastolica': 95,
                'glicemia': 168,
                'condicao': 'DENGUE',
                'alerta': 'Foco suspeito de Dengue',
                'observacoes': 'Visita de bloqueio realizada em domicílio.',
            }),
            content_type='application/json',
        )
        self.assertEqual(r_api_post.status_code, 201)
        self.assertIn('map', r_api_post.json())
        fam.refresh_from_db()
        self.assertEqual(fam.risco_territorial, 'CRITICO')
        self.assertIn('DENGUE', fam.condicoes_sentinelas)

        # Portaria 3.493 / 161 BI Panel HTML + CSV export
        r_bi = self.client.get(reverse('rh_municipal_public:saude_bi_panel'))
        self.assertEqual(r_bi.status_code, 200)
        self.assertContains(r_bi, 'Portaria')
        r_csv = self.client.get(reverse('rh_municipal_public:saude_bi_panel') + '?format=csv')
        self.assertEqual(r_csv.status_code, 200)
        self.assertIn('text/csv', r_csv['Content-Type'])
        self.assertIn('Indicador', r_csv.content.decode('utf-8'))

        # Non-authorized user in org blocked from ACS map
        self.client.force_login(self.server_user.user)
        r_forbid = self.client.get(reverse('rh_municipal_public:saude_acs_map'))
        self.assertIn(r_forbid.status_code, (302, 403))

    def test_beds_monitor_telemetry_alarms_and_ack(self):
        beds_url = reverse('rh_municipal_public:saude_beds_monitor')
        with tenant_context(self.c):
            citizen = pg.CidadaoSaudeMunicipal.objects.create(
                organization=self.c,
                nome='José Carlos Monitorado',
                cpf='98765432100',
            )

        # 1. Create ward + bed + monitor
        r_create = self.client.post(beds_url, {
            'action': 'create_bed',
            'unidade_saude': self.health_unit.pk,
            'ala_codigo': 'UPA-VERM',
            'ala_nome': 'Sala Vermelha UPA',
            'ala_tipo': 'SALA_VERMELHA',
            'leito_codigo': 'L-SV-01',
            'tipo_leito': 'SALA_VERMELHA',
            'serial_monitor': 'MINDRAY-SE-001',
            'modelo_monitor': 'uMEC 12 Wi-Fi',
        })
        self.assertEqual(r_create.status_code, 302)
        with tenant_context(self.c):
            bed = pg.LeitoSaudeMunicipal.objects.get(organization=self.c, codigo='L-SV-01')
            monitor = pg.EquipamentoMonitorSaude.objects.get(organization=self.c, serial_dispositivo='MINDRAY-SE-001')

        # 2. Admit patient to bed
        r_admit = self.client.post(beds_url, {
            'action': 'admit',
            'bed_public_id': str(bed.public_id),
            'cidadao_id': citizen.pk,
            'precaucao': 'Monitorização contínua pós-síncope',
        })
        self.assertEqual(r_admit.status_code, 302)
        with tenant_context(self.c):
            bed.refresh_from_db()
            self.assertEqual(bed.status, 'OCUPADO')

        # 3. Unauthenticated medical gateway pushes telemetry via Bearer / X-Monitor-Token
        with tenant_context(self.c):
            monitor.token_ingestao = rt_svc.monitor_token_digest('synthetic-monitor-test-token')
            monitor.save(update_fields=['token_ingestao'])
        self.client.logout()
        r_tel = self.client.post(
            reverse('rh_municipal_public:saude_beds_telemetry_api'),
            data=json.dumps({
                'monitor_serial': 'MINDRAY-SE-001',
                'fc_bpm': 148,
                'spo2_percent': 86,
                'pa_sistolica': 170,
                'pa_diastolica': 110,
                'fr_irpm': 26,
                'temperatura_c': '38.4',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer synthetic-monitor-test-token',
        )
        self.assertEqual(r_tel.status_code, 201)
        tel_json = r_tel.json()
        self.assertEqual(tel_json['nivel_alerta'], 'CRITICO')
        self.assertEqual(tel_json['pam_mmhg'], 130)  # (170 + 2*110)/3 = 130
        self.assertGreaterEqual(len(tel_json['alertas']), 1)

        with tenant_context(self.c):
            alert = pg.EventoAlertaMonitorSaude.objects.filter(organization=self.c, leito=bed, reconhecido=False).first()
            self.assertIsNotNone(alert)

        # 4. Clinician acknowledges alert
        self.client.force_login(self.admin_c.user)
        r_ack = self.client.post(beds_url, {
            'action': 'ack_alert',
            'alert_public_id': str(alert.public_id),
            'conduta': 'O2 suplementar em máscara 5L/min e avaliação médica imediata.',
        })
        self.assertEqual(r_ack.status_code, 302)
        with tenant_context(self.c):
            alert.refresh_from_db()
            self.assertTrue(alert.reconhecido)

        # 5. Cross-tenant isolation check
        with tenant_context(self.org2):
            self.assertFalse(pg.LeitoSaudeMunicipal.objects.filter(organization=self.org2, codigo='L-SV-01').exists())

    # ------------------------------------------------------------------
    # TRACK B: PORTAL DO CIDADÃO + GOV.BR + TRIBUTÁRIO REAL
    # ------------------------------------------------------------------
    def test_govbr_login_and_citizen_portal_end_to_end(self):
        cpf = '12345678901'
        central_url = reverse('rh_municipal_public:public_citizen_central', args=[self.c.slug])
        govbr_start = reverse('rh_municipal_public:public_govbr_start', args=[self.c.slug])
        govbr_homolog = reverse('rh_municipal_public:public_govbr_homolog', args=[self.c.slug])

        self.client.logout()
        self.assertEqual(self.client.get(govbr_start, follow=True).status_code, 200)

        state = self.client.session[f'govbr_oidc_{self.c.pk}']['state']
        # Authenticate a synthetic citizen in the explicitly enabled local fixture
        r_login = self.client.post(govbr_homolog, {
            'state': state,
            'cpf': cpf,
            'nome': 'José da Silva Cidadão',
            'email': 'jose@cidadao.test',
            'nivel': 'OURO',
        })
        self.assertEqual(r_login.status_code, 302)
        with tenant_context(self.c):
            self.assertTrue(pe.SessaoCidadaoGovBr.objects.filter(organization=self.c, cpf=cpf).exists())

        # Create real digital protocol
        r_prot = self.client.post(central_url, {
            'action': 'create_protocol',
            'assunto': 'Solicitação de Poda de Árvore',
            'descricao': 'Árvore obstruindo rede pública na Rua Central.',
        })
        self.assertEqual(r_prot.status_code, 302)
        with tenant_context(self.c):
            self.assertTrue(pe.ProcessoDigitalMunicipal.objects.filter(organization=self.c, titulo='Solicitação de Poda de Árvore').exists())

        # Book SUS appointment
        r_sus = self.client.post(central_url, {
            'action': 'book_appointment',
            'unidade_id': self.health_unit.pk,
            'procedimento': '0301010064',
            'queixa': 'Consulta de rotina hipertensão',
        })
        self.assertEqual(r_sus.status_code, 302)
        with tenant_context(self.c):
            self.assertTrue(pg.AtendimentoSaudeMunicipal.objects.filter(organization=self.c, cidadao__cpf=cpf).exists())
            appointment = pg.AtendimentoSaudeMunicipal.objects.get(organization=self.c, cidadao__cpf=cpf)
            from .phase_g_services import _fernet
            self.assertEqual(_fernet().decrypt(bytes(appointment.queixa_portal_cifrada)), 'Consulta de rotina hipertensão'.encode())
            self.assertFalse(pg.SenhaFilaAtendimento.objects.filter(organization=self.c, detalhe_prioridade__icontains='hipertensão').exists())

        r_view = self.client.get(central_url)
        self.assertEqual(r_view.status_code, 200)
        self.assertContains(r_view, 'Solicitação de Poda de Árvore')

        # Cross-tenant isolation on Org2: same browser/citizen does not leak Org1 items in Org2
        r_org2 = self.client.get(reverse('rh_municipal_public:public_citizen_central', args=[self.org2.slug]))
        self.assertEqual(r_org2.status_code, 200)
        self.assertNotContains(r_org2, 'Solicitação de Poda de Árvore')

    def test_tributary_dam_cnd_cpen_positiva_itbi_and_nfse_preview(self):
        cpf = '12345678901'
        with tenant_context(self.c):
            buyer = pf_svc.create_contributor(self.admin_c.user, data={
                'codigo': 'CTR-BUYER', 'tipo': 'PF', 'nome': 'José Comprador', 'documento': cpf,
                'email': '', 'telefone': '', 'logradouro': 'Rua A', 'numero_endereco': '10', 'situacao': 'ATIVO',
            })
            seller = pf_svc.create_contributor(self.admin_c.user, data={
                'codigo': 'CTR-SELLER', 'tipo': 'PF', 'nome': 'Antônio Transmitente', 'documento': '99988877766',
                'email': '', 'telefone': '', 'logradouro': 'Rua B', 'numero_endereco': '20', 'situacao': 'ATIVO',
            })
            prop = pf_svc.create_property(self.admin_c.user, data={
                'inscricao': '01.02.003.0004', 'proprietario': seller, 'possuidor': buyer,
                'logradouro': 'Av. Beira Rio', 'numero_endereco': '100', 'bairro': 'Centro',
                'cep': '49995000', 'utilizacao': 'RESIDENCIAL', 'area_terreno': Decimal('200'),
                'area_edificada': Decimal('110'), 'situacao': 'ATIVO',
            })
            iptu_rule = pf_svc.save_tax_rule(self.admin_c.user, data={
                'tributo': 'IPTU', 'codigo': 'IPTU-2026', 'versao': 1,
                'vigencia_inicio': date(2026, 1, 1), 'parametros': {'aliquota_percentual': '1.0'}, 'ativa': True,
            })
            itbi_rule = pf_svc.save_tax_rule(self.admin_c.user, data={
                'tributo': 'ITBI', 'codigo': 'ITBI-2026', 'versao': 1,
                'vigencia_inicio': date(2026, 1, 1), 'parametros': {'aliquota_percentual': '2.0'}, 'ativa': True,
            })
            iss_rule = pf_svc.save_tax_rule(self.admin_c.user, data={
                'tributo': 'ISS', 'codigo': 'ISS-2026', 'versao': 1,
                'vigencia_inicio': date(2026, 1, 1), 'parametros': {'aliquota_percentual': '5.0'}, 'ativa': True,
            })

            # 1. Clean taxpayer gets CND + Public Validation succeeds
            cert_clean = ct_svc.issue_tax_certificate(self.c, contribuinte_id=buyer.pk, actor=self.admin_c.user)
            self.assertEqual(cert_clean.tipo, 'CND')

        r_val = self.client.get(reverse('rh_municipal_public:public_certificate_validate', args=[self.c.slug]) + f'?codigo={cert_clean.codigo_autenticacao}')
        self.assertEqual(r_val.status_code, 200)
        self.assertContains(r_val, 'AUTÊNTICA E VÁLIDA')

        # 2. Create overdue launch -> POSITIVA
        with tenant_context(self.c):
            overdue_launch = pf_svc.create_tax_launch(
                self.admin_c.user, taxpayer_id=buyer.pk, tax_type='IPTU',
                base_amount=Decimal('50000.00'), due_date=date(2025, 12, 31),
                rule_id=iptu_rule.pk, exercise=2025, property_id=prop.pk,
            )
            cert_pos = ct_svc.issue_tax_certificate(self.c, contribuinte_id=buyer.pk, actor=self.admin_c.user)
            self.assertEqual(cert_pos.tipo, 'POSITIVA')

            # 3. Emit DAM (44-digit Segmento 8 + Pix EMV CRC16)
            dam = ct_svc.emit_dam(self.c, launch_id=overdue_launch.pk, actor=self.admin_c.user)
            self.assertEqual(len(dam.codigo_barras), 44)
            self.assertEqual(dam.codigo_barras[0], '8')
            self.assertIn('000201', dam.pix_copia_cola)
            self.assertIn('6304', dam.pix_copia_cola)

        # 4. Evaluate ITBI parameterized at 2.0% of max(100000, 120000) = R$ 2.400,00
        r_itbi = self.client.post(reverse('rh_municipal_public:tributary_home'), {
            'action': 'evaluate_itbi',
            'property_id': prop.pk,
            'seller_id': seller.pk,
            'buyer_id': buyer.pk,
            'rule_id': itbi_rule.pk,
            'valor_venal': '100000.00',
            'valor_transacao': '120000.00',
            'cartorio_matricula': 'MAT-9988',
        })
        self.assertEqual(r_itbi.status_code, 302)
        with tenant_context(self.c):
            itbi_row = pf.SolicitacaoITBIMunicipal.objects.get(organization=self.c, imovel=prop)
            self.assertEqual(itbi_row.valor_itbi, Decimal('2400.00'))

        # Portal do Cidadão gov.br self-service DAM & CND/Positiva via HTTP POST
        govbr_homolog = reverse('rh_municipal_public:public_govbr_homolog', args=[self.c.slug])
        central_url = reverse('rh_municipal_public:public_citizen_central', args=[self.c.slug])
        self.client.get(reverse('rh_municipal_public:public_govbr_start', args=[self.c.slug]))
        state = self.client.session[f'govbr_oidc_{self.c.pk}']['state']
        self.client.post(govbr_homolog, {'state': state, 'cpf': cpf, 'nome': 'José Comprador', 'nivel': 'OURO'})
        r_portal_dam = self.client.post(central_url, {'action': 'emit_dam', 'launch_id': overdue_launch.pk})
        self.assertEqual(r_portal_dam.status_code, 302)
        r_portal_cnd = self.client.post(central_url, {'action': 'emit_cnd', 'finalidade': 'Banco do Nordeste'})
        self.assertEqual(r_portal_cnd.status_code, 302)
        with tenant_context(self.c):
            self.assertGreaterEqual(pf.GuiaArrecadacaoDAM.objects.filter(organization=self.c, lancamento=overdue_launch).count(), 2)
            self.assertGreaterEqual(pf.CertidaoTributariaMunicipal.objects.filter(organization=self.c, contribuinte=buyer).count(), 3)

        # 5. NFS-e ABRASF RPS preview works while transmit_nfse() stays strictly blocked
        with tenant_context(self.c):
            note = pf_svc.create_nfse_draft(self.admin_c.user, data={
                'prestador': seller, 'tomador': buyer, 'codigo_servico': '14.01',
                'valor_servico': Decimal('1000.00'), 'descricao': 'Consultoria Técnica', 'competencia': '2026-11',
            })
            preview = ct_svc.build_nfse_rps_preview(self.c, note)
            self.assertIn('<InfDeclaracaoPrestacaoServico>', preview['xml'])
            self.assertEqual(preview['valor_iss'], '50.00')
            with self.assertRaises(ValidationError):
                pf_svc.transmit_nfse(self.admin_c.user, note_id=note.pk)

        r_nfse = self.client.get(reverse('rh_municipal_public:nfse_home'))
        self.assertEqual(r_nfse.status_code, 200)
        self.assertContains(r_nfse, 'InfDeclaracaoPrestacaoServico')
