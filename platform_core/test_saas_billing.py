"""Testes do ecossistema de cobrança SaaS e liquidação automática por Webhook."""

from decimal import Decimal
from django.test import TestCase, Client
from django.utils import timezone
from platform_core.models import Organization, OrganizationMembership
from platform_core.saas_models import PlanoSaaS, AssinaturaSaaS, CobrancaSaaS
from platform_core.saas_billing import (
    gerar_cobranca_pix,
    processar_webhook_asaas,
    simular_confirmacao_pagamento,
)
from django.contrib.auth import get_user_model

User = get_user_model()


class SaasBillingTestCase(TestCase):
    def setUp(self):
        self.org1 = Organization.objects.create(
            razao_social='Clínica Alpha Ltda',
            cnpj='52758249000164',
            slug='clinica-alpha',
        )
        self.org2 = Organization.objects.create(
            razao_social='Hospital Beta S.A.',
            cnpj='11222333000181',
            slug='hospital-beta',
        )
        self.plano = PlanoSaaS.objects.create(
            slug='enterprise',
            nome='Plano Enterprise',
            preco=Decimal('299.00'),
            ativo=True,
        )
        self.assinatura1 = AssinaturaSaaS.objects.create(
            organization=self.org1,
            plano=self.plano,
            status=AssinaturaSaaS.Status.ACTIVE,
            vencimento_em=timezone.localdate(),
        )

        self.user1 = User.objects.create_user(
            username='admin_alpha',
            password='TestPassword123!',
        )
        self.membership1 = OrganizationMembership.objects.create(
            organization=self.org1,
            user=self.user1,
            role='ADMIN',
        )
        from platform_core.models import MembershipERP, OrganizationERP, ERPCatalog
        erp, _ = ERPCatalog.objects.get_or_create(code='assistencia', defaults={'name': 'Assistência Técnica', 'active': True})
        if not erp.active:
            erp.active = True
            erp.save()
        OrganizationERP.objects.get_or_create(organization=self.org1, erp=erp, defaults={'enabled': True})
        MembershipERP.objects.get_or_create(membership=self.membership1, erp=erp, defaults={'enabled': True})

    def test_gerar_cobranca_pix_cria_payload_emv_valido(self):
        cobranca = gerar_cobranca_pix(self.org1)
        self.assertIsNotNone(cobranca)
        self.assertEqual(cobranca.organization, self.org1)
        self.assertEqual(cobranca.valor, Decimal('299.00'))
        self.assertEqual(cobranca.status, CobrancaSaaS.Status.PENDENTE)
        self.assertEqual(cobranca.metodo, CobrancaSaaS.Metodo.PIX)
        self.assertTrue(cobranca.pix_copia_cola.startswith('000201'))
        self.assertIn('https://api.qrserver.com', cobranca.pix_qr_code_url)

    def test_marcar_como_paga_renova_vencimento_da_assinatura(self):
        cobranca = gerar_cobranca_pix(self.org1)
        vencimento_original = self.assinatura1.vencimento_em

        cobranca.marcar_como_paga()
        cobranca.refresh_from_db()
        self.assinatura1.refresh_from_db()

        self.assertEqual(cobranca.status, CobrancaSaaS.Status.PAGA)
        self.assertIsNotNone(cobranca.pago_em)
        self.assertEqual(self.assinatura1.status, AssinaturaSaaS.Status.ACTIVE)
        # Deve ter estendido em 30 dias
        self.assertEqual(self.assinatura1.vencimento_em, vencimento_original + timezone.timedelta(days=30))

    def test_processar_webhook_asaas_confirma_pagamento(self):
        cobranca = gerar_cobranca_pix(self.org1)
        payload = {
            'event': 'PAYMENT_RECEIVED',
            'payment': {
                'id': cobranca.gateway_id,
                'value': 299.00,
                'externalReference': str(cobranca.pk),
            }
        }
        sucesso, msg = processar_webhook_asaas(payload)
        self.assertTrue(sucesso)
        cobranca.refresh_from_db()
        self.assertEqual(cobranca.status, CobrancaSaaS.Status.PAGA)

    def test_isolamento_multitenant_nao_permite_liquidar_cobranca_alheia(self):
        cobranca1 = gerar_cobranca_pix(self.org1)
        # Org 2 tenta liquidar cobrança da Org 1
        resultado = simular_confirmacao_pagamento(cobranca1.pk, organization=self.org2)
        self.assertFalse(resultado)
        cobranca1.refresh_from_db()
        self.assertEqual(cobranca1.status, CobrancaSaaS.Status.PENDENTE)

    def test_view_meu_plano_exibe_cobrancas(self):
        cobranca = gerar_cobranca_pix(self.org1)
        client = Client()
        client.force_login(self.user1)

        session = client.session
        session['organization_id'] = self.org1.pk
        session.save()

        from django.urls import reverse
        url = reverse('platform_core:meu_plano')
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'R$ 299,00')
        self.assertContains(response, 'Aguardando Pagamento')
        self.assertContains(response, cobranca.pix_copia_cola[:20])
