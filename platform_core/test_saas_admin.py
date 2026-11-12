from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .context import tenant_context
from .models import (
    ConfiguracaoEmpresa, ERPCatalog, MembershipERP, ModuloOrganizacao, Organization,
    OrganizationERP, OrganizationMembership, PermissaoMembership,
)
from .modules import module_enabled, product_module_enabled
from .saas_models import (
    CORE_SAAS_MODULES, AdministradorPlataforma, AjusteModuloSaaS, AssinaturaSaaS,
    AuditoriaSaaS, HistoricoPlanoSaaS, PlanoSaaS, PrimeiroAcessoSaaS,
)
from .saas_services import (
    alterar_plano, alterar_status_cliente, criar_cliente, criar_prefeitura_homologacao,
    metricas_uso, reemitir_primeiro_acesso, verificar_limite,
)


class SaaSAdminTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.platform = User.objects.create_user(
            username='platform-admin', email='platform@example.test', password='SafePass!2371',
        )
        AdministradorPlataforma.objects.create(user=self.platform, created_by=self.platform)
        self.plan = PlanoSaaS.objects.create(
            slug='essencial', nome='Essencial', preco='80.00',
            modulos=['assistencia', 'estoque', 'pdv'],
            limites={'usuarios': 5, 'filiais': 2, 'armazenamento_mb': 500,
                     'vendas_mes': None, 'ordens_mes': None},
        )
        self.actor = self.platform

    def company(self, cnpj='39966868000186'):
        return {
            'razao_social': 'Empresa de Homologação Ltda.',
            'nome_fantasia': 'Empresa Homologação', 'cnpj': cnpj,
            'email': 'empresa@example.test', 'telefone': '+55 11 99999-1234',
            'endereco': 'Rua de Teste, 100', 'cidade': 'São Paulo', 'uf': 'SP',
        }

    def admin_data(self, email='admin-cliente@example.test'):
        return {'nome': 'Ana Administradora', 'email': email, 'whatsapp': '+55 11 98888-1234'}

    def make_tenant(self, *, cnpj='39966868000186', email='admin-cliente@example.test'):
        return criar_cliente(actor=self.actor, company=self.company(cnpj), admin=self.admin_data(email), plan_id=self.plan.pk)

    def unused_test_cnpj(self):
        for number in range(90000000, 90001000):
            base = f'{number:08d}0001'
            first_weights = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
            first_sum = sum(int(digit) * weight for digit, weight in zip(base, first_weights))
            first_remainder = first_sum % 11
            first_digit = 0 if first_remainder < 2 else 11 - first_remainder
            partial = f'{base}{first_digit}'
            second_weights = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
            second_sum = sum(int(digit) * weight for digit, weight in zip(partial, second_weights))
            second_remainder = second_sum % 11
            second_digit = 0 if second_remainder < 2 else 11 - second_remainder
            candidate = f'{partial}{second_digit}'
            if not Organization.objects.filter(cnpj=candidate).exists():
                return candidate
        self.fail('Não foi possível encontrar um CNPJ sintético livre para o teste.')

    def test_onboarding_atomico_cria_tenant_admin_plano_modulos_e_token_digest(self):
        result = self.make_tenant()
        org, user = result['organization'], result['user']
        self.assertEqual(result['subscription'].status, AssinaturaSaaS.Status.IMPLANTACAO)
        self.assertEqual(result['membership'].role, 'ADMIN')
        self.assertFalse(user.has_usable_password())
        self.assertTrue(OrganizationMembership.objects.filter(pk=result['membership'].pk, organization=org, user=user).exists())
        self.assertEqual(AssinaturaSaaS.objects.get(organization=org).plano, self.plan)
        self.assertEqual(set(ModuloOrganizacao.all_objects.filter(organization=org, habilitado=True).values_list('codigo', flat=True)),
                         {'assistencia', 'clientes', 'produtos', 'estoque', 'estoque_basico',
                          'estoque_completo', 'financeiro', 'financeiro_basico', 'relatorios_basicos', 'pdv', 'fiscal'})
        token_row = PrimeiroAcessoSaaS.objects.get(pk=result['token_id'])
        self.assertNotIn(result['token'], token_row.token_digest)
        self.assertEqual(token_row.token_digest, PrimeiroAcessoSaaS.digest(result['token']))
        self.assertEqual(AuditoriaSaaS.objects.filter(organization=org).count(), 4)
        with tenant_context(org):
            config = ConfiguracaoEmpresa.objects.get(organization=org)
            self.assertEqual(config.limites, {'usuarios': 5, 'filiais': 2, 'armazenamento_mb': 500})

    def test_onboarding_transacao_reverte_tudo_se_modulos_falham(self):
        before_orgs = Organization.objects.count()
        before_users = get_user_model().objects.count()
        with patch('platform_core.saas_services.aplicar_modulos', side_effect=ValidationError('falha sintética')):
            with self.assertRaises(ValidationError):
                self.make_tenant()
        self.assertEqual(Organization.objects.count(), before_orgs)
        self.assertEqual(get_user_model().objects.count(), before_users)
        self.assertFalse(AssinaturaSaaS.objects.exists())
        self.assertFalse(PrimeiroAcessoSaaS.objects.exists())

    def test_primeiro_acesso_token_unico_define_senha_sem_exibir_ou_armazenar_token(self):
        result = self.make_tenant()
        token = result['token']
        url = reverse('platform_core:saas_first_access', args=[token])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {'new_password1': 'Client!Pass9342', 'new_password2': 'Client!Pass9342'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Acesso configurado')
        result['user'].refresh_from_db()
        self.assertTrue(result['user'].check_password('Client!Pass9342'))
        row = PrimeiroAcessoSaaS.objects.get(pk=result['token_id'])
        self.assertIsNotNone(row.usado_em)
        self.assertEqual(self.client.post(url, {'new_password1': 'Another!Pass9342', 'new_password2': 'Another!Pass9342'}).status_code, 410)
        self.assertNotContains(response, token)

    def test_primeiro_acesso_preserva_csrf_com_origem_e_rejeita_origin_null(self):
        # Use only synthetic tenants in the transactional test database.
        accepted = self.make_tenant(cnpj=self.unused_test_cnpj(), email='csrf-ok@example.test')
        client = Client(enforce_csrf_checks=True)
        accepted_url = reverse('platform_core:saas_first_access', args=[accepted['token']])
        page = client.get(accepted_url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page['Referrer-Policy'], 'origin')
        csrf_token = client.cookies['csrftoken'].value
        response = client.post(
            accepted_url,
            {'csrfmiddlewaretoken': csrf_token, 'new_password1': 'Client!Pass9342',
             'new_password2': 'Client!Pass9342'},
            HTTP_ORIGIN='http://testserver',
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Acesso configurado')
        accepted_row = PrimeiroAcessoSaaS.objects.get(pk=accepted['token_id'])
        self.assertIsNotNone(accepted_row.usado_em)

        rejected = self.make_tenant(cnpj=self.unused_test_cnpj(), email='csrf-null@example.test')
        rejected_client = Client(enforce_csrf_checks=True)
        rejected_url = reverse('platform_core:saas_first_access', args=[rejected['token']])
        rejected_page = rejected_client.get(rejected_url)
        self.assertEqual(rejected_page.status_code, 200)
        rejected_response = rejected_client.post(
            rejected_url,
            {'csrfmiddlewaretoken': rejected_client.cookies['csrftoken'].value,
             'new_password1': 'Client!Pass9342', 'new_password2': 'Client!Pass9342'},
            HTTP_ORIGIN='null',
        )
        self.assertEqual(rejected_response.status_code, 403)
        rejected['user'].refresh_from_db()
        self.assertFalse(rejected['user'].has_usable_password())
        self.assertIsNone(PrimeiroAcessoSaaS.objects.get(pk=rejected['token_id']).usado_em)

    def test_primeiro_acesso_expirado_nao_define_senha(self):
        result = self.make_tenant()
        row = PrimeiroAcessoSaaS.objects.get(pk=result['token_id'])
        row.expira_em = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=['expira_em'])
        response = self.client.post(reverse('platform_core:saas_first_access', args=[result['token']]), {
            'new_password1': 'Client!Pass9342', 'new_password2': 'Client!Pass9342',
        })
        self.assertEqual(response.status_code, 410)
        result['user'].refresh_from_db()
        self.assertFalse(result['user'].has_usable_password())

    def test_cliente_tenant_admin_nao_acessa_rotas_saas(self):
        result = self.make_tenant()
        self.client.force_login(result['user'])
        for url in [reverse('platform_core:saas_home'), reverse('platform_core:saas_clients'), reverse('platform_core:saas_plans')]:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(reverse('platform_core:saas_home')).headers['Cache-Control'], 'private, no-store')

    def test_dashboard_clientes_planos_pwa_e_service_worker_renderizam(self):
        self.client.force_login(self.platform)
        for name in ['saas_home', 'saas_clients', 'saas_plans', 'saas_new_client']:
            response = self.client.get(reverse(f'platform_core:{name}'))
            self.assertEqual(response.status_code, 200, name)
            self.assertContains(response, 'VERTYX Admin')
        manifest = self.client.get(reverse('platform_core:saas_manifest'))
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.json()['start_url'], '/saas/')
        self.assertEqual(manifest.json()['scope'], '/saas/')
        self.assertEqual(manifest.json()['name'], 'VERTYX Admin')
        worker = self.client.get(reverse('platform_core:saas_sw'))
        self.assertEqual(worker.status_code, 200)
        self.assertEqual(worker['Service-Worker-Allowed'], '/saas/')
        self.assertNotContains(worker, 'caches.open')
        from django.contrib.staticfiles import finders
        css = open(finders.find('css/saas-admin.css'), encoding='utf-8').read()
        base = open('templates/platform_core/saas/base.html', encoding='utf-8').read()
        self.assertIn('@media(max-width:640px)', css)
        self.assertIn('name="viewport"', base)

    def test_assistente_visual_de_criacao_entrega_link_whatsapp_sem_senha(self):
        self.client.force_login(self.platform)
        step1 = self.client.post(reverse('platform_core:saas_new_client'), self.company())
        self.assertRedirects(step1, reverse('platform_core:saas_new_client_step2'))
        step2 = self.client.post(reverse('platform_core:saas_new_client_step2'), {
            'plano': self.plan.pk,
            'admin-nome': 'Ana Administradora', 'admin-email': 'novo-admin@example.test',
            'admin-whatsapp': '+55 11 98888-1234',
        })
        self.assertEqual(step2.status_code, 302)
        detail = self.client.get(step2.url)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'CLIENTE CRIADO COM SUCESSO')
        self.assertContains(detail, 'Enviar acesso pelo WhatsApp')
        self.assertContains(detail, 'defina sua própria senha')
        self.assertNotContains(detail, 'password=')
        access = detail.context['created_access']
        self.assertIn('/primeiro-acesso/', access['url'])
        response = self.client.post(reverse('platform_core:saas_share_access', args=[Organization.objects.get(cnpj='39966868000186').public_id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('https://api.whatsapp.com/send?text='))
        self.assertFalse(self.client.session.get('saas_pending_access'))
        self.assertTrue(AuditoriaSaaS.objects.filter(action='LINK_WHATSAPP_ABERTO', details__confirmado_envio=False).exists())

    def test_alteracao_plano_registra_historico_sincroniza_modulos_e_preserva_permissoes_individuais(self):
        result = self.make_tenant()
        membership = result['membership']
        with tenant_context(result['organization']):
            permission = PermissaoMembership.objects.create(
                organization=result['organization'], membership=membership,
                capacidade='operar_pdv', permitido=True,
            )
        next_plan = PlanoSaaS.objects.create(
            slug='profissional-v2-teste', nome='Profissional', preco='249.00',
            modulos=['assistencia', 'rh'], limites={'usuarios': 10, 'filiais': 3},
        )
        subscription = alterar_plano(actor=self.actor, organization=result['organization'], plan_id=next_plan.pk, reason='homologação')
        self.assertEqual(subscription.plano, next_plan)
        self.assertEqual(HistoricoPlanoSaaS.objects.filter(assinatura=subscription).count(), 1)
        permission.refresh_from_db()
        self.assertTrue(permission.permitido)
        with tenant_context(result['organization']):
            modules = dict(ModuloOrganizacao.objects.values_list('codigo', 'habilitado'))
            config = ConfiguracaoEmpresa.objects.get(organization=result['organization'])
        self.assertTrue(modules['rh'])
        self.assertTrue(modules['pdv'])
        self.assertTrue(modules['fiscal'])
        self.assertEqual(config.limites, {'usuarios': 10, 'filiais': 3})

    def test_suspensao_reativacao_preserva_dados_e_audita(self):
        result = self.make_tenant()
        org = result['organization']
        alterar_status_cliente(actor=self.actor, organization=org, activate=False)
        org.refresh_from_db()
        self.assertEqual(org.status, Organization.Status.SUSPENDED)
        self.assertTrue(OrganizationMembership.objects.filter(pk=result['membership'].pk).exists())
        self.assertTrue(AssinaturaSaaS.objects.filter(organization=org).exists())
        alterar_status_cliente(actor=self.actor, organization=org, activate=True)
        org.refresh_from_db()
        self.assertEqual(org.status, Organization.Status.ACTIVE)
        self.assertEqual(AuditoriaSaaS.objects.filter(organization=org, action__in=['CLIENTE_SUSPENSO', 'CLIENTE_REATIVADO']).count(), 2)

    def test_cliente_em_implantacao_so_conta_ativo_apos_ativacao_explicita(self):
        self.client.force_login(self.platform)
        dashboard = self.client.get(reverse('platform_core:saas_home'))
        active_before = dashboard.context['counts']['ativas']
        result = self.make_tenant()
        dashboard = self.client.get(reverse('platform_core:saas_home'))
        self.assertEqual(dashboard.context['counts']['ativas'], active_before)
        self.assertEqual(dashboard.context['counts']['implantacao'], 1)
        detail_url = reverse('platform_core:saas_client_detail', args=[result['organization'].public_id])
        detail = self.client.get(detail_url)
        self.assertContains(detail, 'Concluir implantação e ativar')
        response = self.client.post(
            reverse('platform_core:saas_client_status', args=[result['organization'].public_id, 'ativar']),
            {'confirmar': 'sim'},
        )
        self.assertEqual(response.status_code, 302)
        result['subscription'].refresh_from_db()
        self.assertEqual(result['subscription'].status, AssinaturaSaaS.Status.ACTIVE)
        dashboard = self.client.get(reverse('platform_core:saas_home'))
        self.assertEqual(dashboard.context['counts']['ativas'], active_before + 1)
        self.assertEqual(dashboard.context['counts']['implantacao'], 0)

    def test_limite_usuarios_e_filiais_e_aplicado_sem_limites_falsos(self):
        result = self.make_tenant()
        org = result['organization']
        for index in range(4):
            user = get_user_model().objects.create_user(username=f'member-{index}', password='unused')
            OrganizationMembership.objects.create(organization=org, user=user, role='TECNICO')
        with self.assertRaises(ValidationError):
            verificar_limite(org, 'usuarios')
        verificar_limite(org, 'vendas_mes')  # Plano sem limite configurado não inventa bloqueio.

    def test_reemissao_rotaciona_token_audita_ids_e_nunca_persiste_segredo(self):
        result = self.make_tenant()
        org = result['organization']
        old = PrimeiroAcessoSaaS.objects.get(pk=result['token_id'])
        self.client.force_login(self.platform)
        response = self.client.post(reverse('platform_core:saas_reissue_access', args=[org.public_id]))
        self.assertRedirects(response, reverse('platform_core:saas_client_detail', args=[org.public_id]))
        old.refresh_from_db()
        self.assertIsNotNone(old.usado_em)
        pending = self.client.session['saas_pending_access'][str(org.public_id)]
        new = PrimeiroAcessoSaaS.objects.get(pk=pending['token_id'])
        self.assertNotEqual(new.token_digest, old.token_digest)
        self.assertEqual(new.token_digest, PrimeiroAcessoSaaS.digest(pending['token']))
        self.assertLess(abs((new.expira_em - new.criado_em) - timedelta(hours=48)), timedelta(seconds=1))
        audit = AuditoriaSaaS.objects.get(action='PRIMEIRO_ACESSO_REEMITIDO', organization=org)
        self.assertEqual(audit.actor, self.platform)
        self.assertEqual(audit.details['token_anterior_invalidado_id'], old.pk)
        self.assertEqual(audit.details['novo_acesso_id'], new.pk)
        self.assertNotIn(pending['token'], str(audit.details))
        self.assertNotIn(pending['token'], new.token_digest)
        self.assertEqual(self.client.get(reverse('platform_core:saas_first_access', args=[result['token']])).status_code, 410)
        self.assertEqual(self.client.get(reverse('platform_core:saas_first_access', args=[pending['token']])).status_code, 200)
        detail = self.client.get(reverse('platform_core:saas_client_detail', args=[org.public_id]))
        self.assertContains(detail, 'Reemitir acesso')
        self.assertContains(detail, 'Enviar acesso pelo WhatsApp')
        self.assertNotContains(detail, 'Senha provisória')

    def test_reemissao_nao_permite_resetar_conta_ja_ativada_e_admin_tenant_nao_pode(self):
        result = self.make_tenant()
        result['user'].set_password('Own!Password9342')
        result['user'].save()
        self.client.force_login(self.platform)
        with self.assertRaises(ValidationError):
            reemitir_primeiro_acesso(actor=self.platform, organization=result['organization'])
        self.client.force_login(result['user'])
        response = self.client.post(reverse('platform_core:saas_reissue_access', args=[result['organization'].public_id]))
        self.assertEqual(response.status_code, 403)

    def test_criacao_de_usuario_bloqueia_no_tenant_da_sessao_ignorando_organization_id_adulterado(self):
        first = self.make_tenant()
        second = self.make_tenant(cnpj='11222333000181', email='admin-two@example.test')
        first['subscription'].plano.limites = {'usuarios': 1}
        first['subscription'].plano.save()
        self.client.force_login(first['user'])
        response = self.client.post(reverse('contas:usuario_create'), {
            'username': 'must-not-exist', 'first_name': 'Novo', 'last_name': 'Usuário',
            'email': 'new-member@example.test', 'perfil': 'TECNICO', 'telefone': '',
            'password1': 'Strong!Password8912', 'password2': 'Strong!Password8912',
            'organization_id': second['organization'].pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Limite de usuários do plano atingido.')
        self.assertFalse(get_user_model().objects.filter(username='must-not-exist').exists())
        self.assertEqual(OrganizationMembership.objects.filter(organization=first['organization']).count(), 1)
        self.assertEqual(OrganizationMembership.objects.filter(organization=second['organization']).count(), 1)

    def test_formulario_da_empresa_nao_expoe_plano_ou_limites_para_adulteracao_tenant(self):
        from .company import EmpresaForm
        form = EmpresaForm({'plano': 'ENTERPRISE', 'limites': '{"usuarios":999999}'})
        self.assertNotIn('plano', form.fields)
        self.assertNotIn('limites', form.fields)

    def test_dashboard_mostra_metricas_reais_e_armazenamento_por_prefixo_do_tenant(self):
        from django.core.files.base import ContentFile
        from ordens.storage import private_storage
        first = self.make_tenant()
        second = self.make_tenant(cnpj='11222333000181', email='admin-two@example.test')
        user = get_user_model().objects.create_user(username='active-extra', password='unused')
        OrganizationMembership.objects.create(organization=first['organization'], user=user, role='TECNICO')
        with tenant_context(first['organization']):
            from .models import FilialEmpresa
            FilialEmpresa.objects.create(codigo='FIL-1', nome='Filial 1')
        key_a = private_storage.save(f'organizations/{first["organization"].public_id}/tenant-test/a.bin', ContentFile(b'1234567'))
        key_b = private_storage.save(f'organizations/{second["organization"].public_id}/tenant-test/b.bin', ContentFile(b'1234567890'))
        try:
            metrics_a = metricas_uso(first['organization'])
            metrics_b = metricas_uso(second['organization'])
            self.assertEqual(metrics_a['usuarios']['used'], 2)
            self.assertEqual(metrics_a['filiais']['used'], 1)
            self.assertEqual(metrics_a['armazenamento_mb']['used'], 7)
            self.assertEqual(metrics_b['armazenamento_mb']['used'], 10)
            self.client.force_login(self.platform)
            dashboard = self.client.get(reverse('platform_core:saas_home'))
            self.assertContains(dashboard, '2 / 5')
            self.assertContains(dashboard, '1 / 2')
            detail = self.client.get(reverse('platform_core:saas_client_detail', args=[first['organization'].public_id]))
            self.assertEqual(detail.context['quota_usage']['armazenamento_mb']['used'], 7)
        finally:
            private_storage.delete(key_a)
            private_storage.delete(key_b)

    def test_bloqueio_de_storage_acima_do_limite_sem_apagar_arquivos(self):
        from django.core.exceptions import ValidationError
        result = self.make_tenant()
        self.plan.limites = {'usuarios': 5, 'filiais': 2, 'armazenamento_mb': 1}
        self.plan.save()
        with patch('platform_core.saas_services._storage_usage_bytes', return_value=1024 * 1024):
            with self.assertRaisesMessage(ValidationError, 'Limite de armazenamento do plano atingido.'):
                verificar_limite(result['organization'], 'armazenamento_mb', incremento=1)
            self.assertEqual(PrimeiroAcessoSaaS.objects.filter(organization=result['organization']).count(), 1)

    def test_plano_abaixo_do_consumo_exibe_aviso_preserva_dados_e_bloqueia_novos(self):
        result = self.make_tenant()
        extra = get_user_model().objects.create_user(username='preserved-member', password='unused')
        OrganizationMembership.objects.create(organization=result['organization'], user=extra, role='TECNICO')
        lower = PlanoSaaS.objects.create(slug='limite-menor', nome='Limite menor', modulos=['assistencia'],
                                         limites={'usuarios': 1, 'filiais': 0, 'armazenamento_mb': 1})
        self.client.force_login(self.platform)
        url = reverse('platform_core:saas_client_change_plan', args=[result['organization'].public_id])
        response = self.client.post(url, {'plano': lower.pk, 'motivo': 'teste limite inferior'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'O cliente possui utilização acima do limite do novo plano.')
        result['subscription'].refresh_from_db()
        self.assertEqual(result['subscription'].plano, self.plan)
        response = self.client.post(url, {'plano': lower.pk, 'motivo': 'teste limite inferior', 'confirmar_limites': 'sim'})
        self.assertRedirects(response, reverse('platform_core:saas_client_detail', args=[result['organization'].public_id]))
        result['subscription'].refresh_from_db()
        self.assertEqual(result['subscription'].plano, lower)
        self.assertTrue(OrganizationMembership.objects.filter(pk=extra.memberships.get(organization=result['organization']).pk).exists())
        self.assertEqual(HistoricoPlanoSaaS.objects.filter(assinatura=result['subscription']).count(), 1)
        with self.assertRaisesMessage(ValidationError, 'Limite de usuários do plano atingido.'):
            verificar_limite(result['organization'], 'usuarios')

    def test_plano_zero_usuarios_e_token_digest_invalido_sao_rejeitados(self):
        with self.assertRaises(ValidationError):
            PlanoSaaS.objects.create(slug='zero', nome='Inválido', limites={'usuarios': 0})
        result = self.make_tenant()
        row = PrimeiroAcessoSaaS.objects.get(pk=result['token_id'])
        row.token_digest = result['token']
        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_tela_de_planos_configura_preco_modulos_e_limites_sem_mudar_catalogo_comercial(self):
        self.client.force_login(self.platform)
        response = self.client.get(reverse('platform_core:saas_plan_create'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Plano comercial público')
        self.assertContains(response, 'Disponível para contratação online')
        self.assertContains(response, 'Cadastro público bloqueado por padrão')
        self.assertContains(response, 'Nível de indicadores')
        response = self.client.post(reverse('platform_core:saas_plan_create'), {
            'slug': 'enterprise-local', 'nome': 'Enterprise Local', 'descricao': 'Plano de homologação',
            'preco': '399.00', 'periodicidade': 'MENSAL', 'ativo': 'on',
            'nivel_indicadores': 'COMPLETO',
            'modulos': ['assistencia', 'fiscal'], 'limite_usuarios': '25',
            'limite_filiais': '4', 'limite_armazenamento_mb': '2048',
            'limite_vendas_mes': '', 'limite_ordens_mes': '',
        })
        self.assertRedirects(response, reverse('platform_core:saas_plans'))
        plan = PlanoSaaS.objects.get(slug='enterprise-local')
        self.assertEqual(plan.preco, 399)
        self.assertEqual(set(plan.modulos), set(CORE_SAAS_MODULES))
        self.assertEqual(plan.limites['usuarios'], 25)
        self.assertEqual(plan.nivel_indicadores, 'COMPLETO')
        self.assertIsNone(plan.limites['vendas_mes'])
        self.assertTrue(AuditoriaSaaS.objects.filter(action='PLANO_CRIADO').exists())

    def test_admin_saas_nao_precisa_ser_admin_de_tenant_e_usuario_comum_nao_tem_capacidade(self):
        self.client.force_login(self.platform)
        self.assertEqual(self.client.get(reverse('platform_core:saas_home')).status_code, 200)
        tenant_admin = get_user_model().objects.create_user(username='tenant-admin', password='Tenant!1234')
        org = Organization.objects.create(slug='tenant-admin-org', razao_social='Cliente Tenant Ltda.')
        OrganizationMembership.objects.create(user=tenant_admin, organization=org, role='ADMIN')
        self.client.force_login(tenant_admin)
        self.assertEqual(self.client.get(reverse('platform_core:saas_home')).status_code, 403)

    def test_admin_pode_adicionar_modulo_fora_do_plano_e_remover_modulo_do_cliente(self):
        result = self.make_tenant()
        org = result['organization']
        tenant_admin = result['user']
        tenant_admin.set_password('Tenant!9876')
        tenant_admin.save()
        alterar_status_cliente(actor=self.platform, organization=org, activate=True)

        # Estado inicial: PDV (core) ativo; Compras (fora do plano) bloqueado.
        self.client.force_login(tenant_admin)
        self.assertEqual(self.client.get('/pdv/').status_code, 200)
        self.assertEqual(self.client.get('/compras/').status_code, 403)
        with tenant_context(org):
            self.assertTrue(module_enabled('pdv'))
            self.assertFalse(module_enabled('compras'))

        # Admin visualiza o editor na ficha do cliente e na tela Editar.
        self.client.force_login(self.platform)
        detail_url = reverse('platform_core:saas_client_detail', args=[org.public_id])
        edit_url = reverse('platform_core:saas_client_edit', args=[org.public_id])
        modules_url = reverse('platform_core:saas_client_modules', args=[org.public_id])
        self.assertContains(self.client.get(detail_url), 'Módulos e produtos liberados')
        self.assertContains(self.client.get(edit_url), 'Salvar dados cadastrais')
        self.assertContains(self.client.get(edit_url), 'Salvar módulos do cliente')

        # Adiciona 'compras' (fora do plano) e remove 'pdv' (core) especificamente para este cliente.
        desired = (set(CORE_SAAS_MODULES) - {'pdv'}) | {'compras', 'estoque_completo'}
        resp = self.client.post(modules_url, {
            'modulos': sorted(desired),
            'sync_erps': '1',
            'erps': ['assistencia'],
            'motivo': 'Pacote personalizado com Compras e sem PDV',
            'acao': 'salvar',
        })
        self.assertRedirects(resp, detail_url)
        self.assertEqual(AjusteModuloSaaS.objects.get(organization=org, codigo='compras').modo, AjusteModuloSaaS.Modo.ALLOW)
        self.assertEqual(AjusteModuloSaaS.objects.get(organization=org, codigo='pdv').modo, AjusteModuloSaaS.Modo.DENY)
        self.assertTrue(AuditoriaSaaS.objects.filter(organization=org, action='CLIENTE_MODULOS_ATUALIZADOS').exists())

        # Tenant passa a acessar Compras (rota, policy e menu lateral) e perde acesso ao PDV.
        self.client.force_login(tenant_admin)
        compras_resp = self.client.get('/compras/')
        self.assertEqual(compras_resp.status_code, 200)
        self.assertEqual(self.client.get('/pdv/').status_code, 403)
        with tenant_context(org):
            self.assertTrue(module_enabled('compras'))
            self.assertFalse(module_enabled('pdv'))
            nav_urls = [
                link['url']
                for group in compras_resp.context['executive_navigation']
                for link in group['links']
            ]
            self.assertIn('/compras/', nav_urls)
            self.assertNotIn('/pdv/', nav_urls)

        # Edição cadastral via EditarClienteForm preserva ajustes manuais de módulos.
        self.client.force_login(self.platform)
        edit_resp = self.client.post(edit_url, {
            'razao_social': org.razao_social,
            'nome_fantasia': 'Cliente Editado Com Módulos',
            'cnpj': org.cnpj,
            'email': org.email,
            'telefone': org.telefone,
            'endereco': org.endereco,
            'cidade': 'Aracaju',
            'uf': 'SE',
        })
        self.assertRedirects(edit_resp, detail_url)
        self.assertTrue(AjusteModuloSaaS.objects.filter(organization=org, codigo='compras', modo=AjusteModuloSaaS.Modo.ALLOW).exists())

        # Restaura para o padrão do plano.
        restore_resp = self.client.post(modules_url, {'acao': 'restaurar', 'motivo': 'Voltar ao plano padrão'})
        self.assertRedirects(restore_resp, detail_url)
        self.assertFalse(AjusteModuloSaaS.objects.filter(organization=org).exists())
        self.assertTrue(AuditoriaSaaS.objects.filter(organization=org, action='CLIENTE_MODULOS_RESTAURADOS').exists())
        with tenant_context(org):
            self.assertFalse(module_enabled('compras'))
            self.assertTrue(module_enabled('pdv'))

    def test_admin_pode_editar_modulos_em_prefeitura_sem_plano_e_erp_toggle(self):
        from rh_municipal.phase_e_services import public_module_available

        created = criar_prefeitura_homologacao(
            actor=self.platform,
            razao_social='Prefeitura Editável de Homologação',
            nome_fantasia='Prefeitura Editável',
            administrador_nome='Gestor Municipal',
            administrador_email='gestor-mun-edit@example.test',
        )
        org = created['organization']
        mun_admin = created['user']
        mun_admin.set_password('MunPass!234')
        mun_admin.save()

        self.assertTrue(product_module_enabled(mun_admin, 'gestao-publica', 'gestao_publica', org))
        self.assertFalse(product_module_enabled(mun_admin, 'gestao-publica', 'saude_sus', org))
        self.assertFalse(public_module_available(org, 'ouvidoria'))

        self.client.force_login(self.platform)
        modules_url = reverse('platform_core:saas_client_modules', args=[org.public_id])
        resp = self.client.post(modules_url, {
            'modulos': ['gestao_publica', 'saude_sus', 'ouvidoria'],
            'sync_erps': '1',
            'erps': ['gestao-publica'],
            'motivo': 'Liberação Saúde SUS e Ouvidoria',
            'next': 'edit',
        })
        self.assertRedirects(resp, reverse('platform_core:saas_client_edit', args=[org.public_id]))
        self.assertTrue(product_module_enabled(mun_admin, 'gestao-publica', 'saude_sus', org))
        self.assertTrue(public_module_available(org, 'ouvidoria'))

        # Remoção manual bloqueia imediatamente mesmo em organização sem AssinaturaSaaS.
        self.client.post(modules_url, {
            'modulos': ['saude_sus'],
            'sync_erps': '1',
            'erps': ['gestao-publica'],
            'motivo': 'Desabilitar base e ouvidoria',
        })
        self.assertFalse(product_module_enabled(mun_admin, 'gestao-publica', 'gestao_publica', org))
        self.assertFalse(public_module_available(org, 'ouvidoria'))

    def test_admin_tenant_nao_pode_alterar_modulos_saas_nem_enviar_codigo_invalido(self):
        result = self.make_tenant()
        org = result['organization']
        tenant_admin = result['user']
        modules_url = reverse('platform_core:saas_client_modules', args=[org.public_id])

        self.client.force_login(tenant_admin)
        self.assertEqual(self.client.post(modules_url, {'modulos': ['compras']}).status_code, 403)
        self.assertFalse(AjusteModuloSaaS.objects.filter(organization=org).exists())

        self.client.force_login(self.platform)
        self.assertEqual(self.client.post(modules_url, {'modulos': ['codigo_que_nao_existe']}).status_code, 400)

    def test_saas_admin_suporta_alternancia_para_modo_claro_com_logo_adaptativa(self):
        from django.contrib.staticfiles import finders

        self.client.force_login(self.platform)
        response = self.client.get(reverse('platform_core:saas_home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-theme-toggle')
        self.assertContains(response, "localStorage.getItem('vertyx_theme')")
        self.assertContains(response, 'vertyx-logo-white.png')
        self.assertContains(response, 'vertyx-logo.png')

        css = open(finders.find('css/saas-admin.css'), encoding='utf-8').read()
        self.assertIn('[data-theme="light"]', css)
        self.assertIn('.vx-logo-light', css)
        self.assertIn('[data-theme="light"] .module-toggle-card', css)
