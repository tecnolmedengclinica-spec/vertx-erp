"""Regression tests for the selective integration; synthetic data only."""
import json

from cryptography.fernet import Fernet
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings
from django.urls import reverse

from platform_core.context import tenant_context
from . import phase_g_models as health
from . import cidadao_tributario_services as citizen_services
from . import saude_realtime_services as realtime
from .models import PermissaoMunicipal, UnidadeMunicipal
from .test_track_a_b_realtime import TrackABFixtures


class RealtimeIntegrationSecurityTests(TrackABFixtures):
    def test_anonymous_kiosk_and_queue_are_not_public(self):
        self.client.logout()
        for name in ('saude_totem', 'saude_call_panel_tv', 'saude_queue_api'):
            response = self.client.get(reverse('rh_municipal_public:' + name))
            self.assertEqual(response.status_code, 302)

    def test_foreign_organization_cannot_be_selected_by_query_string(self):
        response = self.client.get(reverse('rh_municipal_public:saude_queue_api'), {'org': self.org2.slug})
        self.assertEqual(response.status_code, 403)

    def test_foreign_unit_does_not_fall_back_to_first_unit(self):
        response = self.client.get(reverse('rh_municipal_public:saude_queue_api'), {'unidade_id': 999999})
        self.assertEqual(response.status_code, 403)

    def test_read_only_user_cannot_write_clinical_data(self):
        with tenant_context(self.c):
            self.access.papel = 'PROFISSIONAL'
            self.access.escopo = 'UNIDADE'
            self.access.unidade = self.structure[3]
            self.access.save(update_fields=['papel', 'escopo', 'unidade'])
            for capability in ('saude.appointments.view', 'saude.records.view', 'saude.dashboard.view'):
                PermissaoMunicipal.objects.create(acesso=self.access, capacidade=capability, permitido=True)
        for name in ('saude_queue_issue_api', 'saude_queue_action_api', 'saude_triage_save_api', 'saude_beds_telemetry_api'):
            response = self.client.post(reverse('rh_municipal_public:' + name), data='{}', content_type='application/json')
            self.assertEqual(response.status_code, 403, name)
        with tenant_context(self.c):
            self.assertEqual(health.SenhaFilaAtendimento.objects.count(), 0)
            self.assertEqual(health.ClassificacaoManchester.objects.count(), 0)
            self.assertEqual(health.TelemetriaMonitorLeito.objects.count(), 0)

    def test_reception_queue_does_not_return_clinical_identity(self):
        with tenant_context(self.c):
            citizen = health.CidadaoSaudeMunicipal.objects.create(organization=self.c, nome='CLINICAL IDENTITY MARKER')
            realtime.issue_queue_ticket(self.c, self.health_unit, service_code='CLN', citizen_id=citizen.pk, actor=self.admin_c.user)
            self.access.papel = 'PROFISSIONAL'
            self.access.escopo = 'UNIDADE'
            self.access.unidade = self.structure[3]
            self.access.save(update_fields=['papel', 'escopo', 'unidade'])
            PermissaoMunicipal.objects.create(acesso=self.access, capacidade='saude.appointments.view', permitido=True)
        response = self.client.get(reverse('rh_municipal_public:saude_queue_api'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'CLINICAL IDENTITY MARKER')
        self.assertIsNone(response.json()['waiting'][0]['paciente_id'])

    def test_telemetry_operator_requires_active_professional(self):
        with tenant_context(self.c):
            _, monitor = realtime.create_ward_bed_and_monitor(self.admin_c.user, self.c, unit_id=self.health_unit.pk, ala_codigo='TEST', ala_nome='Synthetic ward', bed_codigo='T01', serial_monitor='MON-TEST')
            self.professional.ativo = False
            self.professional.save(update_fields=['ativo'])
            with self.assertRaises(PermissionDenied):
                realtime.ingest_monitor_telemetry(self.c, monitor_id_or_serial=monitor.serial_dispositivo, fc_bpm=70, actor=self.admin_c.user)
            self.assertEqual(health.TelemetriaMonitorLeito.objects.count(), 0)

    def test_tv_hides_patient_identity_and_cannot_be_cached(self):
        with tenant_context(self.c):
            citizen = health.CidadaoSaudeMunicipal.objects.create(organization=self.c, nome='PRIVATE PATIENT MARKER')
            realtime.issue_queue_ticket(self.c, self.health_unit, service_code='CLN', citizen_id=citizen.pk, actor=self.admin_c.user)
        response = self.client.get(reverse('rh_municipal_public:saude_call_panel_tv'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'PRIVATE PATIENT MARKER')
        self.assertIn('no-store', response['Cache-Control'])

    def test_queue_read_does_not_create_catalog_rows(self):
        response = self.client.get(reverse('rh_municipal_public:saude_queue_api'))
        self.assertEqual(response.status_code, 200)
        with tenant_context(self.c):
            self.assertEqual(health.ServicoFilaSaude.objects.count(), 0)
            self.assertEqual(health.LocalAtendimentoSaude.objects.count(), 0)

    def test_triage_rejects_missing_patient_instead_of_first_patient(self):
        with tenant_context(self.c):
            health.CidadaoSaudeMunicipal.objects.create(organization=self.c, nome='Unrelated synthetic patient')
            with self.assertRaises(ValidationError):
                realtime.save_manchester_triage(self.admin_c.user, self.c, flowchart='Test', discriminator='Test', color='VERDE')
            self.assertEqual(health.ClassificacaoManchester.objects.count(), 0)

    def test_service_rejects_foreign_tenant_even_when_actor_has_local_permission(self):
        with tenant_context(self.c), self.assertRaises(PermissionDenied):
            realtime.compute_health_bi_panel(self.admin_c.user, self.org2)

    def test_govbr_simulation_is_disabled_by_default(self):
        self.client.logout()
        response = self.client.get(reverse('rh_municipal_public:public_govbr_start', args=[self.c.slug]))
        self.assertEqual(response.status_code, 403)

    @override_settings(MUNICIPAL_GOVBR_DEMO_ENABLED=True, DEBUG=True)
    def test_govbr_simulation_requires_state_and_callback_never_trusts_query_identity(self):
        self.client.logout()
        response = self.client.post(reverse('rh_municipal_public:public_govbr_homolog', args=[self.c.slug]), {'cpf': '12345678901', 'nome': 'Synthetic citizen'})
        self.assertEqual(response.status_code, 400)
        response = self.client.get(reverse('rh_municipal_public:public_govbr_callback', args=[self.c.slug]), {'cpf': '12345678901', 'nome': 'Forged identity'})
        self.assertEqual(response.status_code, 503)

    @override_settings(MUNICIPAL_GOVBR_DEMO_ENABLED=True, DEBUG=False)
    def test_govbr_simulation_is_blocked_with_debug_false(self):
        self.client.logout()
        response = self.client.get(reverse('rh_municipal_public:public_govbr_start', args=[self.c.slug]))
        self.assertEqual(response.status_code, 403)

    def test_monitor_gateway_is_disabled_by_default(self):
        self.client.logout()
        response = self.client.post(reverse('rh_municipal_public:saude_beds_telemetry_api'), data=json.dumps({}), content_type='application/json', HTTP_X_MONITOR_TOKEN='synthetic-token')
        self.assertEqual(response.status_code, 403)

    def test_unit_scope_denies_other_unit_in_same_municipality(self):
        with tenant_context(self.c):
            unit = UnidadeMunicipal.objects.create(organization=self.c, setor=self.structure[2], codigo='SECOND', nome='Second synthetic unit')
            health_unit = health.UnidadeSaudeMunicipal.objects.create(organization=self.c, unidade=unit, tipo='UBS')
            self.access.escopo = 'UNIDADE'
            self.access.papel = 'PROFISSIONAL'
            self.access.unidade = self.structure[3]
            self.access.save(update_fields=['papel', 'escopo', 'unidade'])
            PermissaoMunicipal.objects.create(acesso=self.access, capacidade='saude.appointments.view', permitido=True)
        response = self.client.get(reverse('rh_municipal_public:saude_queue_api'), {'unidade_id': health_unit.pk})
        self.assertEqual(response.status_code, 403)

    def test_triage_requires_actor_professional_link(self):
        with tenant_context(self.c):
            citizen = health.CidadaoSaudeMunicipal.objects.create(organization=self.c, nome='Synthetic triage patient')
            self.professional.ativo = False
            self.professional.save(update_fields=['ativo'])
            with self.assertRaises(ValidationError):
                realtime.save_manchester_triage(self.admin_c.user, self.c, citizen_id=citizen.pk, flowchart='Test', discriminator='Test', color='VERDE')
            self.assertEqual(health.ClassificacaoManchester.objects.count(), 0)

    def test_triage_free_text_is_encrypted_and_missing_key_rolls_back(self):
        key = Fernet.generate_key()
        with tenant_context(self.c):
            citizen = health.CidadaoSaudeMunicipal.objects.create(organization=self.c, nome='Synthetic triage patient')
            with override_settings(MUNICIPAL_HEALTH_ENCRYPTION_KEY=key.decode()):
                triage = realtime.save_manchester_triage(self.admin_c.user, self.c, citizen_id=citizen.pk, flowchart='Test', discriminator='Test', color='VERDE', complaint='synthetic confidential text')
            self.assertEqual(triage.queixa_principal, '')
            self.assertEqual(Fernet(key).decrypt(bytes(triage.queixa_principal_cifrada)), b'synthetic confidential text')
            count = health.ClassificacaoManchester.objects.count()
            with override_settings(MUNICIPAL_HEALTH_ENCRYPTION_KEY=''), self.assertRaises(ValidationError):
                realtime.save_manchester_triage(self.admin_c.user, self.c, citizen_id=citizen.pk, flowchart='Test', discriminator='Test', color='VERDE', complaint='never plaintext')
            self.assertEqual(health.ClassificacaoManchester.objects.count(), count)

    def test_invalid_json_is_rejected(self):
        response = self.client.post(reverse('rh_municipal_public:saude_queue_issue_api'), data='[]', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_snapshot_json_cannot_close_the_script_element(self):
        marker = '</script><script>alert("synthetic")</script>'
        with tenant_context(self.c):
            self.health_unit.unidade.nome = marker
            self.health_unit.unidade.save(update_fields=['nome'])
        response = self.client.get(reverse('rh_municipal_public:saude_call_panel'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, marker)
        self.assertContains(response, '\\u003C/script\\u003E')

    @override_settings(MUNICIPAL_HEALTH_ENCRYPTION_KEY='')
    def test_portal_complaint_without_key_rolls_back_appointment_and_patient(self):
        with tenant_context(self.c):
            with self.assertRaises(ValidationError):
                citizen_services.book_citizen_sus_appointment(self.c, cpf='12345678901', nome='Synthetic citizen', unit_id=self.health_unit.pk, complaint='never store plaintext', user=self.admin_c.user)
            self.assertEqual(health.AtendimentoSaudeMunicipal.objects.count(), 0)
            self.assertEqual(health.CidadaoSaudeMunicipal.objects.count(), 0)

    def test_portal_does_not_reuse_other_tenant_branding_from_session(self):
        session = self.client.session
        session['vertyx_branding'] = {'nome_institucional': 'PRIVATE BRANDING MARKER', 'unidade_nome': 'PRIVATE UNIT MARKER'}
        session['vertyx_branding_org'] = self.c.pk
        session.save()
        response = self.client.get(reverse('rh_municipal_public:public_citizen_central', args=[self.org2.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'PRIVATE UNIT MARKER')
        self.assertNotContains(response, 'PRIVATE BRANDING MARKER')
