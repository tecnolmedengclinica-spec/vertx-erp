import uuid
from decimal import Decimal
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase, Client, TransactionTestCase
from django.db import connection, connections
from unittest import skipUnless
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.urls import reverse
from django.utils import timezone
from platform_core.models import Organization, OrganizationMembership, OrganizationERP, MembershipERP, ERPCatalog
from platform_core.context import tenant_context
from clientes.models import Cliente, Unidade
from equipamentos.models import CategoriaEquipamento, Equipamento
from contas.models import Usuario, Tecnico
from ordens.models import OrdemServico, Status
from ordens import services as os_services
from ordens.forms import AssinaturaForm
from platform_core.tests import sample_signature
from . import models as m, forms, services, reporting


class Dados:
    def prepare_data(self):
        self.erp, _ = ERPCatalog.objects.get_or_create(code='assistencia', defaults={'name': 'Assistência'})
        self.a = Organization.objects.create(slug='gestao-a', razao_social='Empresa A')
        self.b = Organization.objects.create(slug='gestao-b', razao_social='Empresa B')
        for org in [self.a, self.b]:
            OrganizationERP.objects.create(organization=org, erp=self.erp)
        self.users = {role: self.user(role.lower(), self.a, role) for role in ['ADMIN', 'GESTOR', 'TECNICO', 'ESTOQUE', 'FINANCEIRO', 'CONTABILIDADE']}
        self.admin = self.users['ADMIN']
        self.admin_b = self.user('admin-b', self.b, 'ADMIN')
        self.tech_b = self.user('tech-b', self.b, 'TECNICO')
        self.da = self.data(self.a, self.users['TECNICO'])
        self.db = self.data(self.b, self.tech_b)
        self.client.force_login(self.admin)

    def user(self, name, org, role):
        user = Usuario.objects.create_user(name, perfil='ADMIN')
        membership = OrganizationMembership.objects.create(user=user, organization=org, role=role)
        MembershipERP.objects.create(membership=membership, erp=self.erp)
        return user

    def data(self, org, user):
        with tenant_context(org):
            customer = Cliente.objects.create(nome_razao_social='Cliente ' + org.slug, cpf_cnpj='MESMO-DOC')
            unit = Unidade.objects.create(cliente=customer, nome='Unidade')
            category = CategoriaEquipamento.objects.create(nome='Hospitalar')
            equipment = Equipamento.objects.create(nome='Equipamento ' + org.slug, categoria=category, cliente=customer, unidade=unit)
            tech = Tecnico.objects.create(nome='Técnico ' + org.slug, usuario=user)
            order = OrdemServico.objects.create(cliente=customer, unidade=unit, equipamento=equipment, tecnico=tech, defeito='Defeito')
            part = m.Peca.objects.create(sku='MESMO-SKU', descricao='Peça ' + org.slug, estoque_minimo=2, preco_venda=Decimal('15.50'), custo_aquisicao=10)
            return dict(customer=customer, unit=unit, equipment=equipment, tech=tech, order=order, part=part)

    def quote(self, **kwargs):
        values = dict(cliente=self.da['customer'], unidade=self.da['unit'], equipamento=self.da['equipment'], validade=timezone.localdate() + timezone.timedelta(days=20), criado_por=self.admin, diagnostico='Fonte defeituosa', servicos='Trocar fonte')
        values.update(kwargs)
        quote = m.Orcamento.objects.create(**values)
        m.ItemOrcamento.objects.create(orcamento=quote, tipo='SERVICO', descricao='Manutenção', quantidade=2, valor_unitario=Decimal('10.25'), desconto=Decimal('0.50'))
        return quote

    def approve(self, quote):
        services.status_orcamento(self.admin, quote.pk, 'enviar')
        services.status_orcamento(self.admin, quote.pk, 'aprovar')
        quote.refresh_from_db()

    def contract_form(self, **kwargs):
        today = timezone.localdate()
        values = dict(numero='CT-1', cliente=self.da['customer'].pk, unidades=[self.da['unit'].pk], equipamentos=[self.da['equipment'].pk], data_inicial=today.isoformat(), data_final=(today + timezone.timedelta(days=75)).isoformat(), valor='100', periodicidade_cobranca='MENSAL', tipo='Manutenção', quantidade_visitas='3', periodicidade_preventiva_meses='1', status='ATIVO')
        values.update(kwargs)
        return forms.ContratoForm(values)

    def entry(self, **kwargs):
        values = dict(tipo='RECEBER', descricao='Receita', valor=Decimal('30'), vencimento=timezone.localdate(), criado_por=self.admin)
        values.update(kwargs)
        return m.Lancamento.objects.create(**values)

    def finalize(self, order):
        os_services.iniciar(self.admin, order.pk, {'resultado': 'NEGADA', 'latitude': None, 'longitude': None, 'precisao': None})
        order.refresh_from_db()
        order.diagnostico, order.servico_executado = 'Diagnóstico', 'Serviço executado'
        order.save()
        conference = os_services.preparar(self.admin, order.pk, {'resultado': 'NEGADA', 'latitude': None, 'longitude': None, 'precisao': None})
        signature = AssinaturaForm({'token': 'teste', 'nome': 'Responsável', 'cargo': 'Gerente', 'imagem': sample_signature(), 'confirmacao': True})
        assert signature.is_valid(), signature.errors
        return os_services.finalizar(self.admin, order.pk, conference.pk, signature.cleaned_data)


class GestaoTests(Dados, TestCase):
    def setUp(self):
        self.prepare_data()

    def test_sku_unico_por_empresa_e_nao_global(self):
        with tenant_context(self.a):
            self.assertEqual(m.Peca.objects.count(), 1)
            with self.assertRaises(ValidationError):
                m.Peca.objects.create(sku='MESMO-SKU', descricao='Duplicada')
        with tenant_context(self.b):
            self.assertEqual(m.Peca.objects.count(), 1)

    def test_entrada_saida_ajuste_e_saldo_negativo_rollback(self):
        with tenant_context(self.a):
            part = self.da['part']
            services.movimentar(self.admin, part.pk, 'ENTRADA', 10)
            services.movimentar(self.admin, part.pk, 'SAIDA', 3)
            with self.assertRaises(ValidationError):
                services.movimentar(self.admin, part.pk, 'SAIDA', 8)
            with self.assertRaises(ValidationError):
                services.movimentar(self.admin, part.pk, 'AJUSTE', 5)
            services.movimentar(self.admin, part.pk, 'AJUSTE', 0, 'Contagem física')
            part.refresh_from_db()
            self.assertEqual(part.quantidade_atual, 0)
            self.assertEqual(m.Movimentacao.objects.count(), 3)
            part.quantidade_atual = 100
            with self.assertRaises(ValidationError):
                part.save()

    def test_movimento_idempotente_nao_duplica_baixa(self):
        with tenant_context(self.a):
            token = uuid.uuid4()
            first = services.movimentar(self.admin, self.da['part'].pk, 'ENTRADA', 3, request_id=token)
            again = services.movimentar(self.admin, self.da['part'].pk, 'ENTRADA', 3, request_id=token)
            self.assertEqual(first.pk, again.pk)
            self.assertEqual(m.Movimentacao.objects.count(), 1)
            with self.assertRaises(ValidationError):
                services.movimentar(self.admin, self.da['part'].pk, 'ENTRADA', 4, request_id=token)

    def test_uso_e_devolucao_os_por_tecnico_atribuido(self):
        with tenant_context(self.a):
            part, order = self.da['part'], self.da['order']
            services.movimentar(self.admin, part.pk, 'ENTRADA', 10)
            services.movimentar(self.users['TECNICO'], part.pk, 'OS', 3, ordem_id=order.pk)
            services.movimentar(self.users['TECNICO'], part.pk, 'DEVOLUCAO', 1, ordem_id=order.pk)
            with self.assertRaises(ValidationError):
                services.movimentar(self.users['TECNICO'], part.pk, 'DEVOLUCAO', 3, ordem_id=order.pk)
            with self.assertRaises(PermissionDenied):
                services.movimentar(self.users['TECNICO'], part.pk, 'ENTRADA', 1)
            other = self.user('outro-tecnico', self.a, 'TECNICO')
            with self.assertRaises(PermissionDenied):
                services.movimentar(other, part.pk, 'OS', 1, ordem_id=order.pk)
            part.refresh_from_db()
            order.refresh_from_db()
            self.assertEqual(part.quantidade_atual, 8)
            self.assertEqual(order.revisao, 3)
            self.assertEqual(order.historico.filter(acao='MOVIMENTACAO_ESTOQUE').count(), 2)

    def test_peca_inativa_ou_vencida_bloqueada(self):
        with tenant_context(self.a):
            part = self.da['part']
            services.movimentar(self.admin, part.pk, 'ENTRADA', 10)
            part.ativo = False
            part.refresh_from_db()
            part.ativo = False
            part.save()
            with self.assertRaises(ValidationError):
                services.movimentar(self.admin, part.pk, 'SAIDA', 1)
            part.ativo, part.validade = True, timezone.localdate() - timezone.timedelta(days=1)
            part.save()
            with self.assertRaises(ValidationError):
                services.movimentar(self.admin, part.pk, 'OS', 1, ordem_id=self.da['order'].pk)

    def test_os_finalizada_nao_recebe_movimento_snapshot_antigo_preservado(self):
        with tenant_context(self.a):
            part, order = self.da['part'], self.da['order']
            services.movimentar(self.admin, part.pk, 'ENTRADA', 10)
            services.movimentar(self.admin, part.pk, 'OS', 1, ordem_id=order.pk)
            signature = self.finalize(order)
            self.assertIn('MESMO-SKU', signature.conferencia.dados['pecas'])
            before = signature.conferencia.digest
            with self.assertRaises(ValidationError):
                services.movimentar(self.admin, part.pk, 'OS', 1, ordem_id=order.pk)
            signature.conferencia.refresh_from_db()
            self.assertEqual(signature.conferencia.digest, before)

    def test_historico_estoque_e_financeiro_imutavel(self):
        with tenant_context(self.a):
            movement = services.movimentar(self.admin, self.da['part'].pk, 'ENTRADA', 1)
            with self.assertRaises(ValidationError):
                movement.delete()
            with self.assertRaises(ValidationError):
                movement.save()
            with self.assertRaises(ValidationError):
                m.Movimentacao.objects.all().delete()
            entry = self.entry()
            services.liquidar(self.admin, entry.pk, timezone.localdate())
            with self.assertRaises(ValidationError):
                entry.eventos.first().delete()

    def test_criar_editar_listar_peca_fornecedor_e_alerta(self):
        response = self.client.post(reverse('gestao:fornecedor_novo'), {'nome': 'Fornecedor', 'ativo': 'on'})
        self.assertEqual(response.status_code, 302)
        with tenant_context(self.a):
            supplier = m.Fornecedor.objects.get(nome='Fornecedor')
        data = {'sku': 'SKU-NOVO', 'descricao': 'Fonte', 'unidade_medida': 'UN', 'estoque_minimo': '2', 'custo_aquisicao': '1', 'preco_venda': '3', 'ativo': 'on', 'fornecedor': supplier.pk}
        self.assertEqual(self.client.post(reverse('gestao:peca_novo'), data).status_code, 302)
        with tenant_context(self.a):
            part = m.Peca.objects.get(sku='SKU-NOVO')
        data['descricao'] = 'Fonte atualizada'
        self.assertEqual(self.client.post(reverse('gestao:peca_editar', args=[part.pk]), data).status_code, 302)
        self.assertContains(self.client.get(reverse('gestao:peca_lista') + '?minimo=1'), 'Fonte atualizada')

    def test_orcamento_totais_decimal_desconto_e_itens(self):
        with tenant_context(self.a):
            quote = self.quote(desconto=Decimal('1.50'))
            self.assertEqual(quote.subtotal, Decimal('20.00'))
            self.assertEqual(quote.total, Decimal('18.50'))
            self.approve(quote)
            item = quote.itens.first()
            item.descricao = 'Mudança indevida'
            with self.assertRaises(ValidationError):
                item.save()
            with self.assertRaises(ValidationError):
                services.remover_item(self.admin, item.pk)

    def test_orcamento_sem_itens_desconto_excessivo_expirado_bloqueia(self):
        with tenant_context(self.a):
            quote = self.quote(desconto=30)
            with self.assertRaises(ValidationError):
                services.status_orcamento(self.admin, quote.pk, 'enviar')
            quote.desconto = 0
            quote.data = timezone.localdate() - timezone.timedelta(days=2)
            quote.validade = timezone.localdate() - timezone.timedelta(days=1)
            quote.save()
            self.assertEqual(quote.situacao, 'VENCIDO')
            with self.assertRaises(ValidationError):
                services.status_orcamento(self.admin, quote.pk, 'enviar')

    def test_converter_aprovado_copia_dados_uma_os_sem_baixa_estoque(self):
        with tenant_context(self.a):
            quote = self.quote()
            m.ItemOrcamento.objects.create(orcamento=quote, tipo='PECA', peca=self.da['part'], descricao='Peça de reposição', quantidade=2, valor_unitario=10)
            self.approve(quote)
            order = services.converter_orcamento(self.admin, quote.pk, self.da['tech'].pk)
            again = services.converter_orcamento(self.admin, quote.pk, self.da['tech'].pk)
            self.assertEqual(order.pk, again.pk)
            self.assertEqual(order.diagnostico, quote.diagnostico)
            self.assertIn('Peça de reposição', order.pecas)
            self.assertEqual(order.valor_cobranca, 40)
            self.assertEqual(m.Movimentacao.objects.count(), 0)

    def test_converter_orcamento_vinculado_reutiliza_os(self):
        with tenant_context(self.a):
            quote = self.quote(ordem=self.da['order'])
            self.approve(quote)
            count = OrdemServico.objects.count()
            order = services.converter_orcamento(self.admin, quote.pk, self.da['tech'].pk)
            self.assertEqual(order.pk, self.da['order'].pk)
            self.assertEqual(OrdemServico.objects.count(), count)

    def test_orcamento_recebivel_opcional_e_sem_duplicar_na_finalizacao_os(self):
        with tenant_context(self.a):
            quote = self.quote(gerar_recebivel=True)
            self.approve(quote)
            order = services.converter_orcamento(self.admin, quote.pk, self.da['tech'].pk)
            self.assertEqual(order.valor_cobranca, 0)
            self.finalize(order)
            self.assertEqual(m.Lancamento.objects.count(), 1)
            self.assertEqual(m.Lancamento.objects.first().orcamento_id, quote.pk)

    def test_orcamento_criacao_edicao_itens_status_pdf_por_http(self):
        data = {'cliente': self.da['customer'].pk, 'unidade': self.da['unit'].pk, 'equipamento': self.da['equipment'].pk, 'data': timezone.localdate().isoformat(), 'validade': (timezone.localdate() + timezone.timedelta(days=10)).isoformat(), 'desconto': '0'}
        self.assertEqual(self.client.post(reverse('gestao:orcamento_novo'), data).status_code, 302)
        with tenant_context(self.a):
            quote = m.Orcamento.objects.first()
        self.assertEqual(self.client.post(reverse('gestao:item_novo', args=[quote.pk]), {'tipo': 'SERVICO', 'descricao': 'Teste', 'quantidade': '1', 'valor_unitario': '10', 'desconto': '0'}).status_code, 302)
        self.assertEqual(self.client.get(reverse('gestao:orcamento_status', args=[quote.pk, 'enviar'])).status_code, 405)
        self.assertEqual(self.client.post(reverse('gestao:orcamento_status', args=[quote.pk, 'enviar'])).status_code, 302)
        response = self.client.get(reverse('gestao:orcamento_pdf', args=[quote.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'%PDF-'))

    def test_contrato_gera_cobrancas_e_planejamento_idempotente(self):
        with tenant_context(self.a):
            form = self.contract_form()
            self.assertTrue(form.is_valid(), form.errors)
            contract = services.salvar_contrato(self.admin, form)
            self.assertEqual(contract.agenda.count(), 3)
            self.assertEqual(contract.lancamentos.count(), 3)
            services.gerar_cobrancas(self.admin, contract.pk)
            services.gerar_planejamento(self.admin, contract.pk)
            self.assertEqual(contract.agenda.count(), 3)
            self.assertEqual(contract.lancamentos.count(), 3)

    def test_contrato_periodicidade_preserva_dia_ancora(self):
        import datetime
        dates = list(services.datas_periodicas(datetime.date(2026, 1, 31), datetime.date(2026, 3, 31), 1))
        self.assertEqual(dates, [datetime.date(2026, 1, 31), datetime.date(2026, 2, 28), datetime.date(2026, 3, 31)])

    def test_contrato_suspenso_nao_gera_cobranca_nem_preventiva(self):
        with tenant_context(self.a):
            form = self.contract_form(status='SUSPENSO')
            self.assertTrue(form.is_valid(), form.errors)
            contract = services.salvar_contrato(self.admin, form)
            self.assertEqual(contract.agenda.count(), 0)
            self.assertEqual(contract.lancamentos.count(), 0)

    def test_contrato_cobertura_coerente_e_mesma_organizacao(self):
        with tenant_context(self.a):
            form = self.contract_form(equipamentos=[self.db['equipment'].pk])
            self.assertFalse(form.is_valid())
            customer = Cliente.objects.create(nome_razao_social='Outro cliente', cpf_cnpj='OUTRO')
            unit = Unidade.objects.create(cliente=customer, nome='Outra')
            form = self.contract_form(unidades=[unit.pk])
            self.assertFalse(form.is_valid())

    def test_contrato_condicoes_ja_lancadas_preservadas(self):
        with tenant_context(self.a):
            form = self.contract_form()
            self.assertTrue(form.is_valid(), form.errors)
            contract = services.salvar_contrato(self.admin, form)
            contract.valor = 200
            with self.assertRaises(ValidationError):
                contract.save()

    def test_planejamento_os_preventiva_finalizacao_atualiza_equipamento(self):
        with tenant_context(self.a):
            form = self.contract_form(valor='0')
            self.assertTrue(form.is_valid(), form.errors)
            contract = services.salvar_contrato(self.admin, form)
            agenda = contract.agenda.first()
            order = services.agendar_os(self.admin, agenda.pk, self.da['tech'].pk)
            self.assertEqual(services.agendar_os(self.admin, agenda.pk, self.da['tech'].pk).pk, order.pk)
            self.assertEqual(order.tipo_atendimento, 'PREVENTIVA')
            self.finalize(order)
            agenda.refresh_from_db()
            self.assertEqual(agenda.situacao, 'CONCLUIDA')
            self.da['equipment'].refresh_from_db()
            self.assertEqual(self.da['equipment'].data_ultima_preventiva, timezone.localdate())

    def test_financeiro_criacao_liquidacao_cancelamento_e_datas(self):
        with tenant_context(self.a):
            entry = self.entry()
            with self.assertRaises(ValidationError):
                services.liquidar(self.admin, entry.pk, timezone.localdate() + timezone.timedelta(days=1))
            services.liquidar(self.users['FINANCEIRO'], entry.pk, timezone.localdate(), 'Recebido')
            entry.refresh_from_db()
            self.assertEqual(entry.status, 'LIQUIDADO')
            self.assertEqual(entry.eventos.count(), 1)
            with self.assertRaises(ValidationError):
                services.liquidar(self.admin, entry.pk, timezone.localdate())
            with self.assertRaises(ValidationError):
                services.cancelar_lancamento(self.admin, entry.pk, 'Não permitido')
            unpaid = self.entry(descricao='Cancelar')
            services.cancelar_lancamento(self.admin, unpaid.pk, 'Lançamento indevido')
            unpaid.refresh_from_db()
            self.assertEqual(unpaid.status, 'CANCELADO')

    def test_os_com_valor_gera_recebivel_uma_vez_e_zero_nao_gera(self):
        with tenant_context(self.a):
            order = self.da['order']
            order.valor_cobranca = 45
            order.save()
            self.finalize(order)
            order.refresh_from_db()
            services.gerar_da_os(self.admin, order)
            self.assertEqual(order.lancamentos.count(), 1)
            self.assertEqual(order.lancamentos.first().valor, 45)

    def test_financeiro_dashboard_caixa_e_vencidos_isolados(self):
        with tenant_context(self.a):
            today = timezone.localdate()
            incoming = self.entry(valor=40)
            outgoing = self.entry(tipo='PAGAR', valor=10)
            services.liquidar(self.admin, incoming.pk, today)
            services.liquidar(self.admin, outgoing.pk, today)
            self.entry(valor=50, vencimento=today - timezone.timedelta(days=1))
            result = reporting.financial_dashboard(today - timezone.timedelta(days=5), today)
            self.assertEqual(result['resultado'], 30)
            self.assertEqual(result['vencido_receber'], 50)
            self.assertEqual(result['a_receber'], 50)
        with tenant_context(self.b):
            self.assertEqual(reporting.financial_dashboard(today, today)['a_receber'], 0)

    def test_financeiro_cadastros_crud_e_contabilidade_somente_leitura(self):
        self.client.force_login(self.users['FINANCEIRO'])
        for resource in ['categoria_financeira', 'centro_custo', 'forma_pagamento']:
            self.assertEqual(self.client.post(reverse('gestao:' + resource + '_novo'), {'nome': resource, 'ativo': 'on'}).status_code, 302)
            self.assertEqual(self.client.get(reverse('gestao:' + resource + '_lista')).status_code, 200)
        with tenant_context(self.a):
            entry = self.entry()
        self.client.force_login(self.users['CONTABILIDADE'])
        self.assertEqual(self.client.get(reverse('gestao:financeiro')).status_code, 200)
        self.assertEqual(self.client.get(reverse('gestao:lancamento_detalhe', args=[entry.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse('gestao:financeiro_operacao', args=[entry.pk, 'liquidar']), {'data': timezone.localdate().isoformat()}).status_code, 403)
        self.assertEqual(self.client.post(reverse('gestao:lancamento_novo'), {}).status_code, 403)

    def test_permissoes_por_perfil_e_menu_sem_concessao_pelo_perfil_global(self):
        expected = {'ADMIN': [1,1,1,1,1], 'GESTOR': [1,1,1,1,1], 'TECNICO': [0,0,0,0,0], 'ESTOQUE': [1,0,0,0,1], 'FINANCEIRO': [0,0,0,1,1], 'CONTABILIDADE': [0,0,0,1,1]}
        urls = [reverse('gestao:' + name) for name in ['peca_lista', 'orcamento_lista', 'contrato_lista', 'financeiro', 'relatorios']]
        for role, user in self.users.items():
            self.client.force_login(user)
            for url, access in zip(urls, expected[role]):
                with self.subTest(role=role, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200 if access else 403)
                    if access:
                        stock_url = reverse('gestao:peca_lista')
                        if role in {'ADMIN', 'GESTOR', 'ESTOQUE'}:
                            self.assertContains(response, f'href="{stock_url}"')
                        else:
                            self.assertNotContains(response, f'href="{stock_url}"')
            self.assertEqual(self.client.get(reverse('gestao:peca_novo')).status_code, 200 if role in ['ADMIN','GESTOR','ESTOQUE'] else 403)

    def test_isolamento_detalhes_edicao_pdf_post_e_queryset(self):
        with tenant_context(self.b):
            quote = m.Orcamento.objects.create(cliente=self.db['customer'], equipamento=self.db['equipment'], unidade=self.db['unit'], validade=timezone.localdate(), criado_por=self.admin_b)
            entry = m.Lancamento.objects.create(tipo='PAGAR', descricao='Despesa secreta B', valor=10, vencimento=timezone.localdate(), criado_por=self.admin_b)
        for resource, obj in [('peca', self.db['part']), ('orcamento', quote), ('lancamento', entry)]:
            self.assertEqual(self.client.get(reverse('gestao:' + resource + '_detalhe', args=[obj.pk])).status_code, 404)
            self.assertEqual(self.client.post(reverse('gestao:' + resource + '_editar', args=[obj.pk]), {}).status_code, 404)
        self.assertEqual(self.client.get(reverse('gestao:orcamento_pdf', args=[quote.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('gestao:financeiro_operacao', args=[entry.pk, 'cancelar']), {'observacao': 'Invasão'}).status_code, 404)
        self.assertNotContains(self.client.get(reverse('gestao:peca_lista')), self.db['part'].descricao)
        self.assertEqual(m.Peca.objects.count(), 0)

    def test_fk_cruzada_rejeitada_em_model_e_formulario(self):
        with tenant_context(self.a):
            self.assertFalse(self.contract_form(cliente=self.db['customer'].pk).is_valid())
            form = forms.OrcamentoForm({'cliente': self.db['customer'].pk, 'equipamento': self.db['equipment'].pk, 'unidade': self.db['unit'].pk, 'data': timezone.localdate(), 'validade': timezone.localdate(), 'desconto': '0'})
            self.assertFalse(form.is_valid())
            with self.assertRaises(ValidationError):
                self.entry(cliente=self.db['customer'])
        response = self.client.post(reverse('gestao:movimentar'), {'peca': self.db['part'].pk, 'tipo': 'ENTRADA', 'quantidade': '1', 'request_id': uuid.uuid4()})
        self.assertEqual(response.status_code, 400)

    def test_revogar_membership_e_grants_bloqueia_modulos(self):
        grant = MembershipERP.objects.get(membership__user=self.admin, membership__organization=self.a)
        grant.enabled = False
        grant.save()
        self.assertEqual(self.client.get(reverse('gestao:peca_lista')).status_code, 403)
        self.assertEqual(self.client.post(reverse('gestao:peca_novo'), {}).status_code, 403)

    def test_todos_relatorios_renderizam_csv_e_pdf_e_ids_invalidos(self):
        for key in reporting.REPORTS:
            url = reverse('gestao:relatorio', args=[key])
            with self.subTest(report=key):
                self.assertEqual(self.client.get(url).status_code, 200)
                self.assertEqual(self.client.get(url + '?export=csv').status_code, 200)
                response = self.client.get(url + '?export=pdf')
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.content.startswith(b'%PDF-'))
        self.assertEqual(self.client.get(reverse('gestao:relatorio', args=['os_periodo']) + '?cliente=' + str(self.db['customer'].pk)).status_code, 400)

    def test_relatorios_filtram_cliente_unidade_tecnico_status_equipamento_periodo(self):
        url = reverse('gestao:relatorio', args=['os_periodo'])
        response = self.client.get(url, {'cliente': self.da['customer'].pk, 'unidade': self.da['unit'].pk, 'tecnico': self.da['tech'].pk, 'equipamento': self.da['equipment'].pk, 'status': 'ABERTA', 'inicio': timezone.localdate().isoformat(), 'fim': timezone.localdate().isoformat(), 'export': 'csv'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.da['order'].numero, response.content.decode())
        self.assertNotIn(self.db['customer'].nome_razao_social, response.content.decode())
        self.assertEqual(self.client.get(url + '?status=INVALIDO').status_code, 400)
        self.assertEqual(self.client.get(url + '?inicio=2026-05-10&fim=2026-05-01').status_code, 400)

    def test_relatorios_perfis_e_tecnico_somente_suas_os(self):
        self.client.force_login(self.users['TECNICO'])
        self.assertEqual(self.client.get(reverse('gestao:relatorio', args=['receitas'])).status_code, 403)
        self.assertEqual(self.client.get(reverse('gestao:relatorio', args=['contratos_ativos'])).status_code, 403)
        with tenant_context(self.a):
            another_tech = Tecnico.objects.create(nome='Sem atribuição')
            order = OrdemServico.objects.create(cliente=self.da['customer'], unidade=self.da['unit'], equipamento=self.da['equipment'], tecnico=another_tech, defeito='Outra OS')
        content = self.client.get(reverse('gestao:relatorio', args=['os_periodo']) + '?export=csv').content.decode()
        self.assertIn(self.da['order'].numero, content)
        self.assertNotIn(order.numero, content)

    def test_csv_neutraliza_formulas_e_pdf_escapa_texto(self):
        content = reporting.csv_bytes(['Cliente'], [['=HYPERLINK("https://invalid")'], ['+SUM(1)'], ['@SUM(2)'], [Decimal('-5')]]).decode()
        self.assertIn("'=HYPERLINK", content)
        self.assertIn("'+SUM", content)
        self.assertIn("'@SUM", content)
        self.assertTrue(reporting.pdf_bytes('<Teste>', self.a, ['Descrição'], [['<script>& texto']]).startswith(b'%PDF-'))

    def test_painel_preserva_indicadores_e_restricao_financeira(self):
        response = self.client.get(reverse('core:painel'))
        self.assertContains(response, 'Clientes ativos')
        self.assertContains(response, 'Equipamentos cadastrados')
        self.assertContains(response, 'OS abertas')
        self.assertContains(response, 'Contas a receber')
        self.client.force_login(self.users['TECNICO'])
        self.assertRedirects(self.client.get(reverse('core:painel')), reverse('operacao:tecnico'))
        self.assertNotContains(self.client.get(reverse('operacao:tecnico')), 'Contas a receber')
        for url in [reverse('ordens:lista'), reverse('clientes:cliente_list'), reverse('equipamentos:equipamento_list')]:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('contas:tecnico_list')).status_code, 200)

    def test_contabilidade_pode_ser_cadastrada_pelo_admin_da_organizacao(self):
        data = {'username': 'contabil-novo', 'perfil': 'CONTABILIDADE', 'password1': 'Senha-local-apenas-teste-987', 'password2': 'Senha-local-apenas-teste-987', 'acesso_assistencia': 'on'}
        self.assertEqual(self.client.post(reverse('contas:usuario_create'), data).status_code, 302)
        self.assertTrue(OrganizationMembership.objects.filter(organization=self.a, user__username='contabil-novo', role='CONTABILIDADE').exists())

    def test_relatorio_financeiro_filtra_origem_orcamento_contrato_e_os(self):
        with tenant_context(self.a):
            quote = self.quote(gerar_recebivel=True)
            self.approve(quote)
            contract_form = self.contract_form()
            self.assertTrue(contract_form.is_valid(), contract_form.errors)
            contract = services.salvar_contrato(self.admin, contract_form)
            self.entry(cliente=self.da['customer'], descricao='Manual sem equipamento')
            query = {'cliente': self.da['customer'], 'unidade': self.da['unit'], 'equipamento': self.da['equipment']}
            _, rows = reporting.report_data(self.admin, 'receitas', query)
            self.assertEqual(len(rows), 4)
            self.assertTrue(any(row[0] == quote.numero for row in rows))
            self.assertFalse(any(row[0] == 'Manual sem equipamento' for row in rows))
            services.agendar_os(self.admin, contract.agenda.first().pk, self.da['tech'].pk)
            query['tecnico'] = self.da['tech']
            _, rows = reporting.report_data(self.admin, 'receitas', query)
            self.assertEqual(len(rows), 3)

    def test_contrato_http_planejamento_e_edicao_de_observacoes(self):
        with tenant_context(self.a):
            data = self.contract_form().data.copy()
        response = self.client.post(reverse('gestao:contrato_novo'), data)
        self.assertEqual(response.status_code, 302)
        with tenant_context(self.a):
            contract = m.Contrato.objects.get(numero='CT-1')
            agenda = contract.agenda.first()
        self.assertContains(self.client.get(reverse('gestao:planejamento')), 'CT-1')
        data['observacoes'] = 'SLA confirmado'
        self.assertEqual(self.client.post(reverse('gestao:contrato_editar', args=[contract.pk]), data).status_code, 302)
        self.assertEqual(self.client.post(reverse('gestao:agenda_os', args=[agenda.pk]), {'tecnico': self.da['tech'].pk}).status_code, 302)
        self.client.force_login(self.users['TECNICO'])
        self.assertRedirects(self.client.get(reverse('gestao:planejamento')), reverse('operacao:calendario'))
        self.assertContains(self.client.get(reverse('operacao:calendario')), 'CT-1')
        self.assertEqual(self.client.post(reverse('gestao:agenda_os', args=[agenda.pk]), {'tecnico': self.da['tech'].pk}).status_code, 403)

    def test_financeiro_http_crud_liquidacao_e_fk_estrangeira(self):
        self.client.force_login(self.users['FINANCEIRO'])
        data = {'tipo': 'PAGAR', 'descricao': 'Compra de ferramentas', 'valor': '50', 'data_emissao': timezone.localdate(), 'vencimento': timezone.localdate()}
        self.assertEqual(self.client.post(reverse('gestao:lancamento_novo'), data).status_code, 302)
        with tenant_context(self.a):
            entry = m.Lancamento.objects.get(descricao=data['descricao'])
        data['valor'] = '55'
        self.assertEqual(self.client.post(reverse('gestao:lancamento_editar', args=[entry.pk]), data).status_code, 302)
        with tenant_context(self.b):
            category = m.CategoriaFinanceira.objects.create(nome='Categoria B')
        data['categoria'] = category.pk
        self.assertEqual(self.client.post(reverse('gestao:lancamento_novo'), data).status_code, 400)
        self.assertEqual(self.client.post(reverse('gestao:financeiro_operacao', args=[entry.pk, 'liquidar']), {'data': timezone.localdate()}).status_code, 302)
        with tenant_context(self.a):
            entry.refresh_from_db()
            self.assertEqual(entry.valor, 55)
            self.assertEqual(entry.eventos.count(), 1)

    def test_classificar_cobranca_automatica_preserva_condicoes_financeiras(self):
        with tenant_context(self.a):
            quote = self.quote(gerar_recebivel=True)
            self.approve(quote)
            entry = quote.lancamentos.get()
            category = m.CategoriaFinanceira.objects.create(nome='Serviços')
            payment = m.FormaPagamento.objects.create(nome='Pix')
        response = self.client.post(reverse('gestao:lancamento_editar', args=[entry.pk]), {'categoria': category.pk, 'forma_pagamento': payment.pk, 'observacoes': 'Classificação revisada', 'valor': '0.01', 'cliente': self.db['customer'].pk})
        self.assertEqual(response.status_code, 302)
        with tenant_context(self.a):
            entry.refresh_from_db()
            self.assertEqual(entry.valor, 20)
            self.assertEqual(entry.cliente_id, self.da['customer'].pk)
            self.assertEqual(entry.categoria_id, category.pk)
            self.assertEqual(entry.forma_pagamento_id, payment.pk)
            entry.valor = 1
            with self.assertRaises(ValidationError):
                entry.save()

    def test_valor_de_cobranca_em_branco_preserva_os_sem_cobranca(self):
        from ordens.forms import OrdemForm
        with tenant_context(self.a):
            form = OrdemForm({'cliente': self.da['customer'].pk, 'unidade': self.da['unit'].pk, 'equipamento': self.da['equipment'].pk, 'tecnico': self.da['tech'].pk, 'tipo_atendimento': 'CORRETIVA', 'defeito': 'Defeito novo', 'valor_cobranca': ''})
            self.assertTrue(form.is_valid(), form.errors)
            order = os_services.criar(self.admin, form)
            self.assertEqual(order.valor_cobranca, 0)

    def test_encerrar_contrato_preserva_preventiva_ja_concluida(self):
        with tenant_context(self.a):
            form = self.contract_form(valor='0')
            self.assertTrue(form.is_valid(), form.errors)
            contract = services.salvar_contrato(self.admin, form)
            agenda = contract.agenda.first()
            order = services.agendar_os(self.admin, agenda.pk, self.da['tech'].pk)
            self.finalize(order)
            contract.status = 'ENCERRADO'
            contract.save()
            agenda.refresh_from_db()
            self.assertEqual(agenda.situacao, 'CONCLUIDA')
            self.assertEqual(contract.agenda.last().situacao, 'CANCELADA')

    def test_item_http_edicao_remocao_e_desconto_excessivo(self):
        with tenant_context(self.a):
            quote = self.quote()
            item = quote.itens.first()
        url = reverse('gestao:item_editar', args=[quote.pk, item.pk])
        data = {'tipo': 'SERVICO', 'descricao': 'Serviço revisado', 'quantidade': 1, 'valor_unitario': 12, 'desconto': 13}
        self.assertEqual(self.client.post(url, data).status_code, 400)
        data['desconto'] = 2
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(reverse('gestao:item_remover', args=[quote.pk, item.pk])).status_code, 302)
        with tenant_context(self.a):
            self.assertEqual(quote.itens.count(), 0)

    def test_movimento_invalida_conferencia_aberta(self):
        with tenant_context(self.a):
            order = self.da['order']
            os_services.iniciar(self.admin, order.pk, {'resultado': 'NEGADA', 'latitude': None, 'longitude': None, 'precisao': None})
            order.refresh_from_db()
            order.diagnostico, order.servico_executado = 'Diagnóstico', 'Serviço'
            order.save()
            conference = os_services.preparar(self.admin, order.pk, {'resultado': 'NEGADA', 'latitude': None, 'longitude': None, 'precisao': None})
            services.movimentar(self.admin, self.da['part'].pk, 'ENTRADA', 10)
            services.movimentar(self.admin, self.da['part'].pk, 'OS', 1, ordem_id=order.pk)
            signature = AssinaturaForm({'token': 'teste', 'nome': 'Responsável', 'cargo': 'Gerente', 'imagem': sample_signature(), 'confirmacao': True})
            self.assertTrue(signature.is_valid())
            with self.assertRaises(ValidationError):
                os_services.finalizar(self.admin, order.pk, conference.pk, signature.cleaned_data)

    def test_paginacao_preserva_filtro_e_saldo_overflow_retorna_erro(self):
        with tenant_context(self.a):
            for index in range(26):
                m.Peca.objects.create(sku=f'FILTRO-{index}', descricao='Filtrada')
            services.movimentar(self.admin, self.da['part'].pk, 'ENTRADA', Decimal('99999999999.999'))
        response = self.client.get(reverse('gestao:peca_lista'), {'q': 'Filtrada', 'minimo': '1'})
        self.assertContains(response, 'q=Filtrada')
        response = self.client.post(reverse('gestao:movimentar'), {'peca': self.da['part'].pk, 'tipo': 'ENTRADA', 'quantidade': '1', 'request_id': uuid.uuid4()})
        self.assertEqual(response.status_code, 400)


@skipUnless(connection.vendor == 'postgresql', 'Bloqueio concorrente exige PostgreSQL.')
class ConcorrenciaGestaoTests(Dados, TransactionTestCase):
    def setUp(self):
        self.prepare_data()

    def simultaneous(self, callback):
        barrier = Barrier(2)
        def worker(index):
            try:
                user = Usuario.objects.get(pk=self.admin.pk)
                with tenant_context(self.a):
                    barrier.wait(timeout=10)
                    try:
                        operation = callback[index] if isinstance(callback, list) else callback
                        return ('ok', operation(user))
                    except ValidationError:
                        return ('invalid', None)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))
        return results

    def test_duas_saidas_concorrentes_nao_criam_saldo_negativo(self):
        with tenant_context(self.a):
            services.movimentar(self.admin, self.da['part'].pk, 'ENTRADA', 10)
        results = self.simultaneous(lambda user: services.movimentar(user, self.da['part'].pk, 'SAIDA', 7).pk)
        self.assertEqual(sorted(x[0] for x in results), ['invalid', 'ok'])
        with tenant_context(self.a):
            self.assertEqual(m.Peca.objects.get(pk=self.da['part'].pk).quantidade_atual, 3)
            self.assertEqual(m.Movimentacao.objects.count(), 2)

    def test_duas_conversoes_concorrentes_criam_uma_os(self):
        with tenant_context(self.a):
            quote = self.quote()
            self.approve(quote)
            before = OrdemServico.objects.count()
        results = self.simultaneous(lambda user: services.converter_orcamento(user, quote.pk, self.da['tech'].pk).pk)
        self.assertEqual(results[0], results[1])
        with tenant_context(self.a):
            self.assertEqual(OrdemServico.objects.count(), before + 1)

    def test_duas_liquidacoes_concorrentes_criam_um_evento(self):
        with tenant_context(self.a):
            entry = self.entry()
        results = self.simultaneous(lambda user: services.liquidar(user, entry.pk, timezone.localdate()).pk)
        self.assertEqual(sorted(x[0] for x in results), ['invalid', 'ok'])
        with tenant_context(self.a):
            self.assertEqual(m.EventoFinanceiro.objects.count(), 1)

    def test_aprovacao_orcamento_e_finalizacao_os_concorrentes_nao_duplicam_cobranca(self):
        with tenant_context(self.a):
            order = self.da['order']
            order.valor_cobranca = 45
            order.save()
            quote = self.quote(ordem=order, gerar_recebivel=True)
            services.status_orcamento(self.admin, quote.pk, 'enviar')
            os_services.iniciar(self.admin, order.pk, {'resultado': 'NEGADA', 'latitude': None, 'longitude': None, 'precisao': None})
            order.refresh_from_db()
            order.diagnostico, order.servico_executado = 'Diagnóstico', 'Serviço'
            order.save()
            conference = os_services.preparar(self.admin, order.pk, {'resultado': 'NEGADA', 'latitude': None, 'longitude': None, 'precisao': None})
            signature = AssinaturaForm({'token': 'teste', 'nome': 'Responsável', 'cargo': 'Gerente', 'imagem': sample_signature(), 'confirmacao': True})
            self.assertTrue(signature.is_valid())
        results = self.simultaneous([
            lambda user: services.status_orcamento(user, quote.pk, 'aprovar').pk,
            lambda user: os_services.finalizar(user, order.pk, conference.pk, signature.cleaned_data).pk,
        ])
        self.assertEqual(results[1][0], 'ok')
        with tenant_context(self.a):
            self.assertEqual(m.Lancamento.objects.count(), 1)
