"""Testes da API de Integração Bancária (Pix BRCode, Boleto, Webhook idempotente, Extrato API e Isolamento Tenant)."""
import json
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from gestao.models import Lancamento
from operacao import models as m
from operacao.banking import (
    configurar_integracao_bancaria,
    crc16_ccitt,
    emitir_cobranca_bancaria,
    gerar_boleto_dados,
    gerar_pix_brcode,
    sincronizar_extrato_api,
)
from platform_core.context import tenant_context
from .test_block2 import Block2Fixtures


class BankingAPITests(Block2Fixtures, TestCase):
    def setUp(self):
        self.setup_operations()
        self.hoje = timezone.localdate()
        with tenant_context(self.a):
            self.conta_a = m.ContaBancaria.objects.create(
                nome='Banco do Brasil API',
                banco='Banco do Brasil',
                agencia='1234',
                numero='55667-8',
                tipo='BANCO',
                saldo_inicial=Decimal('1000.00'),
            )
            self.receber_a = Lancamento.objects.create(
                tipo='RECEBER',
                descricao='Manutenção Hospitalar Fatura 101',
                cliente=self.da['customer'],
                valor=Decimal('450.00'),
                vencimento=self.hoje,
                criado_por=self.admin,
            )
        with tenant_context(self.b):
            self.conta_b = m.ContaBancaria.objects.create(
                nome='Banco Tenant B',
                banco='Inter',
                tipo='BANCO',
                saldo_inicial=Decimal('500.00'),
            )

    def test_pix_brcode_crc16_e_boleto_febraban(self):
        brcode = gerar_pix_brcode(
            chave_pix='11222333000181',
            nome_recebedor='VERTYX TECNOLOGIA',
            cidade='ARACAJU',
            valor=Decimal('450.00'),
            txid='VTXTEST123456',
        )
        self.assertTrue(brcode.startswith('000201'))
        self.assertIn('br.gov.bcb.pix', brcode)
        self.assertEqual(brcode[-4:], crc16_ccitt(brcode[:-4]))

        linha, barras = gerar_boleto_dados(
            banco_codigo='001',
            nosso_numero='1234567890',
            valor=Decimal('450.00'),
            vencimento=self.hoje,
        )
        self.assertEqual(len(barras), 44)
        self.assertTrue(linha.startswith('0019'))

    def test_configurar_integracao_rejeita_segredo_bruto_e_salva_hash(self):
        with tenant_context(self.a):
            with self.assertRaises(ValidationError):
                configurar_integracao_bancaria(
                    self.admin,
                    self.conta_a.pk,
                    provedor='ASAAS',
                    credencial_referencia='password=meu-token-secreto',
                )

            integ, secret = configurar_integracao_bancaria(
                self.admin,
                self.conta_a.pk,
                provedor='INTER',
                ambiente='SANDBOX',
                credencial_referencia='env://VERTYX_INTER_TOKEN',
                chave_pix='11222333000181',
                webhook_secret='segredo-teste-inter',
            )
            self.assertTrue(integ.webhook_token_hash)
            self.assertNotIn('segredo-teste-inter', integ.webhook_token_hash)
            self.assertEqual(secret, 'segredo-teste-inter')

    def test_cobranca_idempotente_e_webhook_liquida_financeiro(self):
        with tenant_context(self.a):
            integ, secret = configurar_integracao_bancaria(
                self.admin,
                self.conta_a.pk,
                provedor='ASAAS',
                ambiente='SANDBOX',
                chave_pix='11222333000181',
                webhook_secret='wh-secret-a',
            )
            req_id = uuid.uuid4()
            cob1 = emitir_cobranca_bancaria(
                self.admin,
                integ.pk,
                valor=Decimal('450.00'),
                vencimento=self.hoje,
                tipo='HIBRIDO',
                lancamento_id=self.receber_a.pk,
                request_id=req_id,
            )
            cob2 = emitir_cobranca_bancaria(
                self.admin,
                integ.pk,
                valor=Decimal('450.00'),
                vencimento=self.hoje,
                tipo='HIBRIDO',
                lancamento_id=self.receber_a.pk,
                request_id=req_id,
            )
            self.assertEqual(cob1.pk, cob2.pk)
            self.assertTrue(cob1.pix_copia_cola)
            self.assertTrue(cob1.linha_digitavel)

        url = reverse('operacao:webhook_banco_publico', args=[self.a.public_id, integ.pk])
        payload = {
            'evento_id': 'EVT-ASAAS-9001',
            'tipo_evento': 'PAYMENT_RECEIVED',
            'external_id': cob1.external_id,
            'txid': cob1.txid,
            'valor_pago': '450.00',
            'data_pagamento': self.hoje.isoformat(),
        }

        # Token errado -> 403
        resp_bad = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN='token-errado',
        )
        self.assertEqual(resp_bad.status_code, 403)

        # Cross-tenant org_id -> 404
        resp_other_org = self.client.post(
            reverse('operacao:webhook_banco_publico', args=[self.b.public_id, integ.pk]),
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN=secret,
        )
        self.assertEqual(resp_other_org.status_code, 404)

        # Webhook válido -> Liquida cobrança, Lancamento e MovimentoCaixa
        resp_ok = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN=secret,
        )
        self.assertEqual(resp_ok.status_code, 200)

        # Replay idempotente -> Não duplica movimento/pagamento
        resp_replay = self.client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_WEBHOOK_TOKEN=secret,
        )
        self.assertEqual(resp_replay.status_code, 200)

        with tenant_context(self.a):
            cob1.refresh_from_db()
            self.receber_a.refresh_from_db()
            self.assertEqual(cob1.status, 'PAGA')
            self.assertEqual(self.receber_a.status, 'LIQUIDADO')
            self.assertEqual(self.conta_a.saldo, Decimal('1450.00'))
            self.assertEqual(m.Pagamento.objects.filter(lancamento=self.receber_a).count(), 1)
            self.assertEqual(m.WebhookBancarioEvento.objects.count(), 1)

    def test_sincronizacao_extrato_api_e_views_integracao(self):
        with tenant_context(self.a):
            integ, _ = configurar_integracao_bancaria(
                self.admin,
                self.conta_a.pk,
                provedor='BANCO_DO_BRASIL',
                ambiente='SANDBOX',
                chave_pix='11222333000181',
            )
            cob = emitir_cobranca_bancaria(
                self.admin,
                integ.pk,
                valor=Decimal('450.00'),
                vencimento=self.hoje,
                tipo='PIX',
                lancamento_id=self.receber_a.pk,
            )
            res1 = sincronizar_extrato_api(self.admin, integ.pk)
            self.assertEqual(res1, {'importados': 1, 'conciliados': 1})
            # Segunda chamada idempotente não duplica extrato
            res2 = sincronizar_extrato_api(self.admin, integ.pk)
            self.assertEqual(res2, {'importados': 0, 'conciliados': 0})

            cob.refresh_from_db()
            self.receber_a.refresh_from_db()
            self.assertEqual(cob.status, 'PAGA')
            self.assertEqual(self.receber_a.status, 'LIQUIDADO')
            self.assertIsNotNone(m.ExtratoBancario.objects.first().movimento_id)

        self.client.force_login(self.admin)
        resp_view = self.client.get(reverse('operacao:bancos_integracao'))
        self.assertEqual(resp_view.status_code, 200)
        self.assertContains(resp_view, cob.txid)
