import base64
import io
import tempfile
from datetime import timedelta
from unittest.mock import patch
from PIL import Image, ImageDraw
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from clientes.models import Cliente, Unidade
from contas.models import Usuario, Tecnico
from equipamentos.models import CategoriaEquipamento, Equipamento
from . import services
from .forms import GeoForm, AssinaturaForm, FotoForm
from .models import OrdemServico, Status, Assinatura, Auditoria, Foto, Conferencia
from .views import token_conferencia
from platform_core.context import tenant_context
from platform_core.models import Organization, OrganizationERP, OrganizationMembership, MembershipERP, ERPCatalog


def png(blank=False):
    img = Image.new('RGB', (900, 320), 'white')
    if not blank:
        ImageDraw.Draw(img).line([(30, 170), (90, 40), (150, 240), (280, 130), (480, 180)], fill='black', width=4)
    buffer = io.BytesIO()
    img.save(buffer, 'PNG')
    return buffer.getvalue()


class AtendimentoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization, _ = Organization.objects.get_or_create(
            slug='tecnolmed', defaults={'razao_social': 'Organização sintética de teste'},
        )
        erp, _ = ERPCatalog.objects.get_or_create(code='assistencia', defaults={'name': 'ERP Assistência'})
        OrganizationERP.objects.get_or_create(organization=cls.organization, erp=erp)
        with tenant_context(cls.organization):
            cls.create_legacy_fixtures()

    @classmethod
    def create_legacy_fixtures(cls):
        cls.admin = Usuario.objects.create_user('admin', perfil='ADMIN')
        cls.gestor = Usuario.objects.create_user('gestor', perfil='GESTOR')
        cls.user = Usuario.objects.create_user('tecnico', perfil='TECNICO')
        cls.outro = Usuario.objects.create_user('outro', perfil='TECNICO')
        cls.financeiro = Usuario.objects.create_user('financeiro', perfil='FINANCEIRO')
        erp = ERPCatalog.objects.get(code='assistencia')
        for user in [cls.admin, cls.gestor, cls.user, cls.outro, cls.financeiro]:
            membership = OrganizationMembership.objects.create(organization=cls.organization, user=user, role=user.perfil)
            MembershipERP.objects.create(membership=membership, erp=erp)
        cls.tecnico = Tecnico.objects.create(nome='Técnico de teste', usuario=cls.user, registro_profissional='CRT TESTE')
        cls.cliente = Cliente.objects.create(nome_razao_social='Hospital de teste', cpf_cnpj='00000000000100')
        cls.unidade = Unidade.objects.create(nome='Matriz', cliente=cls.cliente)
        cls.categoria = CategoriaEquipamento.objects.create(nome='Hospitalar')
        cls.equipamento = Equipamento.objects.create(nome='Monitor', cliente=cls.cliente, unidade=cls.unidade, categoria=cls.categoria, codigo_interno='PAT-1', numero_serie='SER-1')

    def setUp(self):
        context = tenant_context(self.organization)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.client.force_login(self.user)
        self.ordem = OrdemServico.objects.create(cliente=self.cliente, unidade=self.unidade, equipamento=self.equipamento, tecnico=self.tecnico, defeito='Não liga')

    def url(self, name, *args):
        return reverse('ordens:' + name, args=[self.ordem.pk, *args])

    def geo(self, resultado='OBTIDA'):
        form = GeoForm({'resultado': resultado, **({'latitude': '-9.665', 'longitude': '-35.735', 'precisao': '12.5'} if resultado == 'OBTIDA' else {})})
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data

    def atendimento(self):
        services.iniciar(self.user, self.ordem.pk, self.geo())
        self.ordem.refresh_from_db()
        self.ordem.diagnostico = 'Falha na fonte'
        self.ordem.servico_executado = 'Substituição e testes'
        self.ordem.pecas = '1 fonte'
        self.ordem.save()
        return services.preparar(self.user, self.ordem.pk, self.geo('NEGADA'))

    def assinatura_data(self, conferencia):
        return {'token': token_conferencia(conferencia), 'nome': 'Maria Responsável', 'cargo': 'Gerente', 'imagem': 'data:image/png;base64,' + base64.b64encode(png()).decode(), 'confirmacao': 'on'}

    def assinar(self, conferencia=None):
        conferencia = conferencia or self.atendimento()
        form = AssinaturaForm(self.assinatura_data(conferencia))
        self.assertTrue(form.is_valid(), form.errors)
        return services.finalizar(self.user, self.ordem.pk, conferencia.pk, form.cleaned_data)

    def test_criar_os_integrada_e_auditoria(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse('ordens:criar'), {'cliente': self.cliente.pk, 'unidade': self.unidade.pk, 'equipamento': self.equipamento.pk, 'tecnico': self.tecnico.pk, 'tipo_atendimento': 'CORRETIVA', 'defeito': 'Falha'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OrdemServico.objects.count(), 2)
        nova = OrdemServico.objects.order_by('pk').last()
        self.assertTrue(nova.historico.filter(acao='CRIACAO').exists())
        self.assertNotEqual(nova.numero, self.ordem.numero)

    def test_relacionamento_inconsistente_rejeitado(self):
        outra = Cliente.objects.create(nome_razao_social='Outra', cpf_cnpj='123')
        self.ordem.cliente = outra
        with self.assertRaises(ValidationError):
            self.ordem.save()

    def test_visualizar_os_e_cadastros_existentes(self):
        for url in [self.url('detalhe'), reverse('ordens:lista'), reverse('clientes:cliente_detail', args=[self.cliente.pk]), reverse('equipamentos:equipamento_detail', args=[self.equipamento.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_iniciar_geo_permitida(self):
        response = self.client.post(self.url('iniciar'), {'resultado': 'OBTIDA', 'latitude': '-9.665', 'longitude': '-35.735', 'precisao': '10'})
        self.assertEqual(response.status_code, 302)
        self.ordem.refresh_from_db()
        self.assertEqual(self.ordem.status, Status.EXECUCAO)
        self.assertIsNotNone(self.ordem.inicio)
        loc = self.ordem.localizacoes.get()
        self.assertEqual(str(loc.latitude), '-9.6650000')
        self.assertTrue(loc.mapa_url.startswith('https://www.google.com/'))
        self.assertEqual(loc.usuario, self.user)

    def test_geo_negada_indisponivel_timeout_sem_coordenadas(self):
        for result in ['NEGADA', 'INDISPONIVEL', 'TIMEOUT', 'NAO_SUPORTADA']:
            with self.subTest(result=result):
                self.assertEqual(self.geo(result)['latitude'], None)
        services.iniciar(self.user, self.ordem.pk, self.geo('NEGADA'))
        self.assertIsNone(self.ordem.localizacoes.get().latitude)
        self.assertEqual(self.ordem.localizacoes.get().mapa_url, '')

    def test_geo_invalida(self):
        for values in [{'resultado': 'OBTIDA', 'latitude': 'NaN', 'longitude': '1'}, {'resultado': 'OBTIDA', 'latitude': '91', 'longitude': '0'}, {'resultado': 'OBTIDA', 'latitude': '1', 'longitude': '181'}, {'resultado': 'OBTIDA', 'latitude': '1', 'longitude': '1', 'precisao': '-1'}, {'resultado': 'OBTIDA', 'latitude': '1', 'longitude': '1', 'precisao': 'inf'}, {'resultado': 'NEGADA', 'latitude': '1'}]:
            self.assertFalse(GeoForm(values).is_valid(), values)

    def test_inicio_nao_pode_ser_repetido(self):
        services.iniciar(self.user, self.ordem.pk, self.geo())
        with self.assertRaises(ValidationError):
            services.iniciar(self.user, self.ordem.pk, self.geo())
        self.assertEqual(self.ordem.localizacoes.count(), 1)

    def test_adicionar_foto_sem_substituir_e_acesso_protegido(self):
        services.iniciar(self.user, self.ordem.pk, self.geo())
        with tempfile.TemporaryDirectory() as directory:
            with override_settings(PRIVATE_STORAGE={'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': directory}}):
                for _ in range(2):
                    response = self.client.post(self.url('foto_adicionar'), {'descricao': 'Fonte', 'arquivo': SimpleUploadedFile('fonte.png', png(), content_type='image/png')})
                    self.assertEqual(response.status_code, 302)
                self.assertEqual(self.ordem.fotos.count(), 2)
                fotos = list(self.ordem.fotos.all())
                self.assertNotEqual(fotos[0].arquivo.name, fotos[1].arquivo.name)
                response = self.client.get(self.url('foto', fotos[0].pk))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(b''.join(response.streaming_content).startswith(b'\xff\xd8'))
                self.client.force_login(self.outro)
                self.assertEqual(self.client.get(self.url('foto', fotos[0].pk)).status_code, 403)

    def test_foto_invalida_rejeitada(self):
        form = FotoForm({}, {'arquivo': SimpleUploadedFile('fake.png', b'not-image', content_type='image/png')})
        self.assertFalse(form.is_valid())

    def test_editar_diagnostico_servico_com_auditoria(self):
        services.iniciar(self.user, self.ordem.pk, self.geo())
        self.ordem.refresh_from_db()
        response = self.client.post(self.url('editar'), {'revisao': self.ordem.revisao, 'diagnostico': 'Fonte', 'servico_executado': 'Troca', 'pecas': '1 fonte', 'tecnico': '999'})
        self.assertEqual(response.status_code, 302)
        self.ordem.refresh_from_db()
        self.assertEqual(self.ordem.tecnico, self.tecnico)
        event = self.ordem.historico.get(acao='ALTERACAO')
        self.assertEqual(event.alteracoes['diagnostico'], {'anterior': '', 'posterior': 'Fonte'})

    def test_edicao_concorrente_rejeitada(self):
        services.iniciar(self.user, self.ordem.pk, self.geo())
        self.assertEqual(self.client.post(self.url('editar'), {'revisao': 1, 'diagnostico': 'Desatualizado'}).status_code, 400)
        self.ordem.refresh_from_db()
        self.assertEqual(self.ordem.diagnostico, '')

    def test_conferencia_e_geos_separadas(self):
        conferencia = self.atendimento()
        self.assertEqual(self.ordem.localizacoes.count(), 2)
        self.assertEqual(conferencia.dados['localizacao_inicio']['resultado'], 'Obtida')
        self.assertEqual(conferencia.dados['localizacao_final']['resultado'], 'Permissão negada')
        response = self.client.get(self.url('conferencia', token_conferencia(conferencia)))
        self.assertContains(response, 'signature-canvas')
        self.assertContains(response, 'PAT-1')
        self.assertContains(response, 'SER-1')
        self.assertContains(response, 'Limpar assinatura')

    def test_preparar_sem_diagnostico_rejeitado(self):
        services.iniciar(self.user, self.ordem.pk, self.geo())
        self.assertEqual(self.client.post(self.url('preparar'), {'resultado': 'NEGADA'}).status_code, 400)

    def test_assinar_finalizar(self):
        conferencia = self.atendimento()
        response = self.client.post(self.url('finalizar'), self.assinatura_data(conferencia))
        self.assertEqual(response.status_code, 302)
        self.ordem.refresh_from_db()
        self.assertEqual(self.ordem.status, Status.FINALIZADA)
        self.assertEqual(self.ordem.conclusao, conferencia.localizacao_final.registrado_em)
        self.assertEqual(Assinatura.objects.count(), 1)
        self.assertTrue(self.ordem.historico.filter(acao='ASSINATURA').exists())
        self.assertTrue(self.ordem.historico.filter(acao='FINALIZACAO').exists())

    def test_assinatura_em_branco_e_payload_invalido(self):
        conferencia = self.atendimento()
        for image in ['data:image/png;base64,' + base64.b64encode(png(True)).decode(), 'data:image/svg+xml;base64,AAAA', 'data:image/png;base64,!!!']:
            data = self.assinatura_data(conferencia)
            data['imagem'] = image
            self.assertFalse(AssinaturaForm(data).is_valid())

    def test_confirmacao_obrigatoria(self):
        data = self.assinatura_data(self.atendimento())
        data.pop('confirmacao')
        self.assertEqual(self.client.post(self.url('finalizar'), data).status_code, 400)
        self.assertEqual(Assinatura.objects.count(), 0)

    def test_assinatura_nao_substituivel_e_os_bloqueada(self):
        conferencia = self.atendimento()
        assinatura = self.assinar(conferencia)
        self.assertEqual(self.client.post(self.url('finalizar'), self.assinatura_data(conferencia)).status_code, 400)
        self.assertEqual(self.client.post(self.url('editar'), {'diagnostico': 'Alterado'}).status_code, 400)
        assinatura.nome = 'Outro'
        with self.assertRaises(ValidationError):
            assinatura.save()
        self.ordem.refresh_from_db()
        self.ordem.diagnostico = 'Alterado'
        with self.assertRaises(ValidationError):
            self.ordem.save()
        self.assertEqual(Assinatura.objects.count(), 1)

    def test_conferencia_invalidada_por_edicao(self):
        conferencia = self.atendimento()
        self.ordem.refresh_from_db()
        self.client.post(self.url('editar'), {'revisao': self.ordem.revisao, 'diagnostico': 'Outra falha', 'servico_executado': 'Outro serviço'})
        self.assertEqual(self.client.post(self.url('finalizar'), self.assinatura_data(conferencia)).status_code, 400)

    def test_conferencia_invalidada_por_cadastro_relacionado(self):
        conferencia = self.atendimento()
        self.equipamento.numero_serie = 'Mudou'
        self.equipamento.save()
        try:
            self.assertEqual(self.client.post(self.url('finalizar'), self.assinatura_data(conferencia)).status_code, 400)
        finally:
            self.equipamento.numero_serie = 'SER-1'
            self.equipamento.save()

    def test_token_alterado_rejeitado(self):
        data = self.assinatura_data(self.atendimento())
        data['token'] += 'X'
        self.assertEqual(self.client.post(self.url('finalizar'), data).status_code, 400)

    def test_conferencia_expirada(self):
        conferencia = self.atendimento()
        with patch('ordens.services.timezone.now', return_value=timezone.now() + timedelta(minutes=31)):
            with self.assertRaises(ValidationError):
                self.assinar(conferencia)

    def test_pdf_e_snapshot_preservado(self):
        assinatura = self.assinar()
        self.cliente.nome_razao_social = 'Cadastro modificado'
        self.cliente.save()
        try:
            self.assertEqual(assinatura.conferencia.dados['cliente'], 'Hospital de teste')
            response = self.client.get(self.url('pdf', assinatura.pk))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content.startswith(b'%PDF-'))
            self.assertGreater(len(response.content), 3000)
        finally:
            self.cliente.nome_razao_social = 'Hospital de teste'
            self.cliente.save()

    def test_reabertura_preserva_assinatura_e_nova_versao(self):
        assinatura = self.assinar()
        with self.assertRaises(PermissionDenied):
            services.reabrir(self.user, self.ordem.pk, 'Correção necessária')
        services.reabrir(self.admin, self.ordem.pk, 'Correção necessária')
        self.ordem.refresh_from_db()
        self.assertEqual(self.ordem.ciclo, 2)
        self.assertEqual(self.ordem.status, Status.ABERTA)
        self.assertEqual(Assinatura.objects.count(), 1)
        self.assertTrue(self.ordem.historico.filter(acao='REABERTURA', justificativa='Correção necessária').exists())
        nova = self.assinar()
        self.assertEqual(nova.ciclo, 2)
        self.assertNotEqual(nova.pk, assinatura.pk)
        self.assertEqual(Assinatura.objects.count(), 2)
        self.assertEqual(self.client.get(self.url('pdf', assinatura.pk)).status_code, 200)

    def test_permissoes_por_url_em_todas_acoes(self):
        conferencia = self.atendimento()
        assinatura = self.assinar(conferencia)
        for user in [self.outro, self.financeiro]:
            self.client.force_login(user)
            for name, args in [('detalhe', []), ('editar', []), ('conferencia', [token_conferencia(conferencia)]), ('assinatura_imagem', [assinatura.pk]), ('pdf', [assinatura.pk])]:
                self.assertEqual(self.client.get(self.url(name, *args)).status_code, 403)
            for name in ['iniciar', 'preparar', 'finalizar', 'reabrir', 'foto_adicionar', 'editar']:
                self.assertEqual(self.client.post(self.url(name), {}).status_code, 403)
            self.assertNotContains(self.client.get(reverse('ordens:lista')), self.ordem.numero)

    def test_tecnico_nao_cria_os(self):
        self.assertEqual(self.client.post(reverse('ordens:criar'), {}).status_code, 403)

    def test_login_csrf_e_metodos(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(self.url('iniciar'), {'resultado': 'NEGADA'}).status_code, 403)
        self.assertEqual(self.client.get(self.url('iniciar')).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get(self.url('detalhe')).status_code, 302)
        self.assertEqual(self.client.get(reverse('equipamentos:unidades_por_cliente')).status_code, 302)

    def test_auditoria_e_conferencia_imutaveis(self):
        conferencia = self.atendimento()
        with self.assertRaises(ValidationError):
            conferencia.save()
        audit = self.ordem.historico.first()
        with self.assertRaises(ValidationError):
            audit.delete()
        self.assertNotIn('password', str(list(self.ordem.historico.values('alteracoes'))))

    def test_os_antiga_sem_localizacao_ou_assinatura(self):
        response = self.client.get(self.url('detalhe'))
        self.assertContains(response, 'Não registrada')
        self.assertEqual(self.ordem.assinaturas.count(), 0)

    def test_html_mobile_camera_e_canvas(self):
        self.atendimento()
        response = self.client.get(self.url('detalhe'))
        self.assertContains(response, 'name="viewport"')
        self.assertContains(response, 'capture="environment"')
        self.assertContains(response, 'accept="image/*"')

    def test_admin_nao_edita_assinatura_nem_os(self):
        self.admin.is_staff = self.admin.is_superuser = True
        self.admin.save()
        self.client.force_login(self.admin)
        assinatura = self.assinar()
        for model, pk in [('ordemservico', self.ordem.pk), ('assinatura', assinatura.pk)]:
            self.assertEqual(self.client.post(reverse(f'admin:ordens_{model}_change', args=[pk]), {}).status_code, 403)

    def test_orm_em_lote_nao_substitui_historico(self):
        assinatura = self.assinar()
        for qs in [OrdemServico.objects.filter(pk=self.ordem.pk), Assinatura.objects.filter(pk=assinatura.pk), Auditoria.objects.filter(ordem=self.ordem)]:
            with self.assertRaises(ValidationError):
                qs.update(pk=1)
            with self.assertRaises(ValidationError):
                qs.delete()

    def test_banco_impede_coordenadas_falsas_e_inicio_duplicado(self):
        from .models import Localizacao
        with self.assertRaises(IntegrityError), transaction.atomic():
            Localizacao.objects.create(ordem=self.ordem, ciclo=1, evento='INICIO', resultado='NEGADA', latitude=1, longitude=2, usuario=self.user)
        services.iniciar(self.user, self.ordem.pk, self.geo())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Localizacao.objects.create(ordem=self.ordem, ciclo=1, evento='INICIO', resultado='NEGADA', usuario=self.user)

    def test_pdf_detecta_assinatura_corrompida(self):
        from .reports import gerar_pdf
        assinatura = self.assinar()
        assinatura.imagem_sha256 = '0' * 64
        with self.assertRaises(ValidationError):
            gerar_pdf(assinatura)

    def test_token_conferencia_nao_pode_ser_usado_por_gestor(self):
        conferencia = self.atendimento()
        self.client.force_login(self.gestor)
        self.assertEqual(self.client.post(self.url('finalizar'), self.assinatura_data(conferencia)).status_code, 400)
