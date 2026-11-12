"""Testes integrados: Reforma Tributária (CBS/IBS/IS + nota.json), NF-e 55, NFS-e Contingência, NFC-e Offline e DF-e Recebidas."""
import json
import tempfile
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from operacao import models as m
from platform_core.context import tenant_context
from platform_core.models import FilialEmpresa
from fiscal.dfe import (
    gerar_rascunho_compra_de_nota,
    importar_xml_nota_recebida,
    manifestar_nota_recebida,
    sincronizar_notas_recebidas,
)
from fiscal.motors.nfe import MotorNFeHomologacao, emitir_nfe_hub
from fiscal.nfce.contingencia import emitir_nfce_pdv_com_rtc, sincronizar_nfce_contingencia
from fiscal.nfce.schema import parse
from fiscal.nfse.contingencia import emitir_nfse_com_contingencia, sincronizar_nfse_contingencia
from fiscal.tributaria import (
    ParametrosReformaTributaria,
    extrair_ibscbs_de_xml,
    gerar_nota_json_rtc,
)
from .test_block2 import Block2Fixtures


class FiscalReformaDFENFeTests(Block2Fixtures, TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        storage = override_settings(PRIVATE_MEDIA_ROOT=self.directory.name, PRIVATE_STORAGE=None)
        storage.enable()
        self.addCleanup(storage.disable)
        self.setup_operations()
        with tenant_context(self.a):
            self.filial_a = FilialEmpresa.objects.create(
                codigo='MATRIZ',
                nome='Matriz VERTYX SE',
                cnpj='11222333000181',
                endereco='Av. Beira Mar, 500',
                inscricao_estadual='271234560',
                inscricao_municipal='445566',
                ativo=True,
            )
            m.PerfilFiscalEstabelecimento.objects.create(
                estabelecimento=self.filial_a,
                municipio_ibge='2800308',
                uf='SE',
                regime='Simples Nacional',
            )
            self.cliente_a = self.da['customer']
        with tenant_context(self.b):
            self.filial_b = FilialEmpresa.objects.create(
                codigo='MATRIZ-B',
                nome='Filial Tenant B',
                cnpj='11444777000161',
                ativo=True,
            )

    def test_reforma_tributaria_calculo_nota_json_e_reducao_aliquota(self):
        params = ParametrosReformaTributaria.from_preset('2026_TESTE')
        nota_json = gerar_nota_json_rtc(
            [{'nItem': '1', 'descricao': 'Monitor Multiparamétrico', 'valor_base': '1000.00'}],
            numero='135240001924',
            default_params=params,
        )
        self.assertEqual(nota_json['numero'], '135240001924')
        self.assertIn('cbs', nota_json)
        self.assertIn('ibs', nota_json)
        self.assertTrue(nota_json['reforma_tributaria'])
        self.assertEqual(nota_json['cbs']['vCBS'], '9.00')
        self.assertEqual(nota_json['ibs']['vIBSUF'], '1.00')
        self.assertEqual(nota_json['ibs']['vIBS'], '1.00')

        com_reducao = ParametrosReformaTributaria(
            cst='200',
            c_class_trib='200001',
            aliquota_cbs=Decimal('8.8000'),
            aliquota_ibs_uf=Decimal('10.0000'),
            aliquota_ibs_mun=Decimal('2.0000'),
            reducao_cbs_pct=Decimal('60.0000'),
            reducao_ibs_pct=Decimal('60.0000'),
            aliquota_is=Decimal('5.0000'),
            cst_is='000',
            c_class_trib_is='000001',
        )
        reduzido = gerar_nota_json_rtc([{'valor_base': '500.00'}], numero='2', default_params=com_reducao)
        self.assertEqual(reduzido['cbs']['vCBS'], '17.60')
        self.assertEqual(reduzido['ibs']['vIBS'], '24.00')
        self.assertEqual(reduzido['is']['vIS'], '25.00')

    def test_nfe_modelo_55_xml_danfe_contingencia_cce_cancelamento(self):
        with tenant_context(self.a):
            res = emitir_nfe_hub(
                self.admin,
                self.filial_a,
                cliente=self.cliente_a,
                descricao='Bisturi Eletrônico Microprocessado',
                valor=Decimal('2000.00'),
                modo_emissao='SVC_AN',
                motivo_contingencia='Sefaz Estadual em manutenção programada SVC-AN',
                regime_transicao='2026_TESTE',
            )
            doc = res['documento']
            self.assertEqual(doc.tipo, 'NFE')
            self.assertEqual(doc.modelo, '55')
            self.assertEqual(doc.status, 'AUTORIZADA')
            self.assertEqual(len(doc.chave), 44)
            self.assertTrue(res['danfe_pdf'].startswith(b'%PDF-'))

            root = parse(res['xml'])
            self.assertEqual(root.findtext('.//{*}tpEmis'), '6')
            self.assertIsNotNone(root.find('.//{*}IBSCBS'))
            self.assertIsNotNone(root.find('.//{*}IBSCBSTot'))

            extraido = extrair_ibscbs_de_xml(root)
            self.assertEqual(extraido['cbs']['vCBS'], '18.00')
            self.assertEqual(extraido['ibs']['vIBS'], '2.00')
            self.assertEqual(doc.detalhesnfe.nota_json['cbs']['vCBS'], '18.00')

            motor = MotorNFeHomologacao()
            cce = motor.carta_correcao_documento(self.admin, doc.pk, 'Correção do peso bruto nos volumes transportados.')
            self.assertEqual(cce['n_seq'], 1)
            canc = motor.cancelar_documento(self.admin, doc.pk, 'Desistência do pedido antes da coleta pela transportadora.')
            doc.refresh_from_db()
            self.assertEqual(canc['status'], 'CANCELADA')
            self.assertEqual(doc.status, 'CANCELADA')

    def test_nfse_contingencia_rps_fallback_e_sincronizacao_adn(self):
        with tenant_context(self.a):
            res_cont = emitir_nfse_com_contingencia(
                self.admin,
                self.filial_a,
                cliente=self.cliente_a,
                codigo_servico='010701',
                municipio='2800308',
                descricao='Calibração de Ventilador Pulmonar',
                valor=Decimal('1000.00'),
                simular_falha_adn=True,
                motivo_contingencia='Timeout Webservice ADN Nacional',
                regime_transicao='2026_TESTE',
            )
            doc = res_cont['documento']
            self.assertEqual(doc.status, 'CONTINGENCIA')
            self.assertEqual(doc.detalhesnfse.modo_emissao, 'CONTINGENCIA_RPS')
            self.assertTrue(doc.detalhesnfse.numero_rps_contingencia.startswith('RPS-'))
            self.assertEqual(doc.detalhesnfse.nota_json['cbs']['vCBS'], '9.00')
            self.assertTrue(res_cont['danfse_pdf'].startswith(b'%PDF-'))

            sync = sincronizar_nfse_contingencia(self.admin, documento_id=doc.pk)
            self.assertEqual(sync['total_sincronizadas'], 1)
            doc.refresh_from_db()
            self.assertEqual(doc.status, 'AUTORIZADA')
            self.assertEqual(doc.detalhesnfse.modo_emissao, 'NORMAL')
            self.assertIsNotNone(doc.detalhesnfse.sincronizada_em)

    def test_nfce_rtc_e_contingencia_offline_pdv_tpemis_9(self):
        with tenant_context(self.a):
            res = emitir_nfce_pdv_com_rtc(
                self.admin,
                self.filial_a,
                itens=[{'sku': 'PDV-10', 'descricao': 'Termômetro Infravermelho', 'valor': '200.00'}],
                cliente=self.cliente_a,
                contingencia_offline=True,
                regime_transicao='2026_TESTE',
            )
            doc = res['documento']
            self.assertEqual(doc.status, 'CONTINGENCIA')
            self.assertEqual(doc.detalhesnfce.modo_emissao, 'CONTINGENCIA_OFFLINE')
            root = parse(res['xml'])
            self.assertEqual(root.findtext('.//{*}mod'), '65')
            self.assertEqual(root.findtext('.//{*}tpEmis'), '9')
            self.assertEqual(root.findtext('.//{*}vCBS'), '1.80')
            self.assertEqual(root.findtext('.//{*}vIBS'), '0.20')

            sync = sincronizar_nfce_contingencia(self.admin, documento_id=doc.pk)
            doc.refresh_from_db()
            self.assertEqual(sync['total_sincronizadas'], 1)
            self.assertEqual(doc.status, 'AUTORIZADA')
            self.assertEqual(doc.detalhesnfce.modo_emissao, 'NORMAL')

    def test_notas_recebidas_dfe_manifestacao_compra_e_isolamento_tenant(self):
        with tenant_context(self.a):
            res = sincronizar_notas_recebidas(self.admin, self.filial_a)
            self.assertEqual(res['total'], 1)
            nota = m.NotaFiscalRecebida.objects.get(pk=res['importadas'][0])
            self.assertTrue(nota.reforma_tributaria)
            self.assertEqual(nota.valor_total, Decimal('1250.00'))
            self.assertEqual(nota.valor_cbs, Decimal('11.25'))
            self.assertEqual(nota.valor_ibs, Decimal('1.25'))
            self.assertEqual(nota.manifestacao_status, 'CIENCIA')
            self.assertEqual(nota.nota_json['cbs']['vCBS'], '11.25')

            # Idempotência de importação manual com mesma chave
            with nota.xml_armazenado.arquivo.open('rb') as fh:
                xml_bytes = fh.read()
            nota_rep = importar_xml_nota_recebida(self.admin, self.filial_a, xml_bytes)
            self.assertEqual(nota_rep.pk, nota.pk)

            # Confirmação e geração de rascunho de Compra
            evt = manifestar_nota_recebida(self.admin, nota.pk, 'CONFIRMADA')
            self.assertEqual(evt.codigo_evento, '210200')
            with self.assertRaises(ValidationError):
                evt.delete()

            compra = gerar_rascunho_compra_de_nota(self.admin, nota.pk, deposito_id=self.depot.pk)
            nota.refresh_from_db()
            self.assertEqual(nota.compra_id, compra.pk)
            self.assertIsNotNone(nota.fornecedor_id)

        # Acesso autorizado no tenant correto e isolamento multi-tenant
        self.client.force_login(self.admin)
        resp_ok = self.client.get(reverse('operacao:fiscal_nota_recebida_detalhe', args=[nota.pk]))
        self.assertEqual(resp_ok.status_code, 200)
        self.assertContains(resp_ok, 'nota.json')

        self.client.force_login(self.admin_b)
        resp = self.client.get(reverse('operacao:fiscal_nota_recebida_detalhe', args=[nota.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_hub_fiscal_views_end_to_end(self):
        self.client.force_login(self.admin)
        resp_hub = self.client.get(reverse('operacao:fiscal_hub'))
        self.assertEqual(resp_hub.status_code, 200)
        self.assertContains(resp_hub, 'nota.json')

        resp_post = self.client.post(
            reverse('operacao:fiscal_hub'),
            {
                'acao': 'emitir_nfe',
                'estabelecimento_id': self.filial_a.pk,
                'cliente_id': self.cliente_a.pk,
                'descricao': 'Autoclave Bancada 21L',
                'valor': '3500.00',
                'modo_emissao': 'NORMAL',
                'regime_transicao': '2026_TESTE',
            },
        )
        self.assertEqual(resp_post.status_code, 302)
        with tenant_context(self.a):
            doc = m.DocumentoFiscal.objects.filter(tipo='NFE').order_by('-pk').first()
        self.assertIsNotNone(doc)

        resp_danfe = self.client.get(reverse('operacao:nfe_danfe', args=[doc.pk]))
        self.assertEqual(resp_danfe.status_code, 200)
        self.assertEqual(resp_danfe['Content-Type'], 'application/pdf')

        resp_json = self.client.get(reverse('operacao:fiscal_nota_json', args=[doc.pk]))
        self.assertEqual(resp_json.status_code, 200)
        data = json.loads(resp_json.content.decode('utf-8'))
        self.assertIn('cbs', data)
        self.assertIn('ibs', data)

        resp_rec = self.client.post(reverse('operacao:fiscal_notas_recebidas'), {'acao': 'sincronizar_dfe'})
        self.assertEqual(resp_rec.status_code, 302)
        resp_rec_list = self.client.get(reverse('operacao:fiscal_notas_recebidas'))
        self.assertEqual(resp_rec_list.status_code, 200)
        self.assertContains(resp_rec_list, 'DISTRIBUIDORA MEDICA NACIONAL')
