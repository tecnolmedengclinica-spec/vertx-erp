"""Security regression for the imported municipal health preview screens."""
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import reverse

from platform_core.context import tenant_context
from platform_core.models import ERPProductModule, PlatformAudit
from platform_core.saas_models import AssinaturaSaaS, PlanoProdutoModulo, PlanoSaaS
from .models import AcessoMunicipal, PermissaoMunicipal
from .phase_g_views import get_tenant_branding
from .public_services import scoped_queryset
from .models import UnidadeMunicipal
from .tests import MunicipalFixtures


class HealthPreviewSecurityTests(MunicipalFixtures):
    def setUp(self):
        super().setUp()
        plan = PlanoSaaS.objects.create(slug='health-preview-test', nome='Plano sintético', erp=self.publico)
        for code in ('gestao_publica', 'saude_sus'):
            module, _ = ERPProductModule.objects.get_or_create(
                erp=self.publico, code=code, defaults={'name': code},
            )
            PlanoProdutoModulo.objects.create(plano=plan, modulo=module)
        AssinaturaSaaS.objects.create(organization=self.c, plano=plan, status='ATIVA')
        with tenant_context(self.c):
            AcessoMunicipal.objects.create(membership=self.admin_c, papel='ADMIN_PREFEITURA',
                                           escopo='PREFEITURA', atribuido_por=self.admin_c.user)
        self.url = reverse('rh_municipal_public:saude_customization')
        self.data = {'nome_institucional': 'Prefeitura DEMO', 'unidade_nome': 'UBS DEMO',
                     'municipio': 'Município DEMO', 'uf': 'SE', 'codigo_ibge': '',
                     'cor_primaria': '#123456', 'cor_secundaria': '#abcdef'}

    def test_anonymous_cannot_open_health_previews_or_modify_an_organization(self):
        self.client.logout()
        for name in ('saude_customization', 'saude_call_panel', 'saude_call_panel_tv', 'saude_totem',
                     'saude_triage_manchester', 'saude_pec_soap', 'saude_pharmacy_dispensation',
                     'saude_professionals_screen', 'saude_acs_map'):
            with self.subTest(route=name):
                self.assertEqual(self.client.get(reverse('rh_municipal_public:' + name)).status_code, 302)
        old_name = self.a.nome_fantasia
        self.assertEqual(self.client.post(self.url, self.data).status_code, 302)
        self.a.refresh_from_db()
        self.assertEqual(self.a.nome_fantasia, old_name)

    def test_foreign_product_cannot_write_to_a_prefeitura(self):
        self.client.force_login(self.admin_a.user)
        session = self.client.session
        session['organization_id'] = self.a.pk
        session.save()
        self.assertEqual(self.client.post(self.url, self.data).status_code, 403)
        self.c.refresh_from_db()
        self.assertNotEqual(self.c.nome_fantasia, self.data['nome_institucional'])

    def test_read_only_professional_cannot_customize(self):
        with tenant_context(self.c):
            access = AcessoMunicipal.objects.create(membership=self.server_user, papel='PROFISSIONAL',
                escopo='UNIDADE', unidade=self.structure[3], atribuido_por=self.admin_c.user)
            PermissaoMunicipal.objects.create(acesso=access, capacidade='saude.units.view', permitido=True)
        self.client.force_login(self.server_user.user)
        self.assertEqual(self.client.post(self.url, self.data).status_code, 403)

    def test_customization_is_validated_scoped_and_audited(self):
        before = (self.a.nome_fantasia, self.b.nome_fantasia)
        response = self.client.post(self.url, {**self.data, 'organization_id': self.a.pk},
                                    HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        for org in (self.a, self.b, self.c):
            org.refresh_from_db()
        self.assertEqual((self.a.nome_fantasia, self.b.nome_fantasia), before)
        self.assertEqual(self.c.nome_fantasia, self.data['nome_institucional'])
        self.assertTrue(PlatformAudit.objects.filter(organization=self.c, action='SAUDE_IDENTIDADE_ATUALIZADA').exists())

    def test_invalid_customization_never_updates_database_or_session(self):
        response = self.client.post(self.url, {**self.data, 'cor_primaria': '<script>alert(1)</script>'})
        self.assertEqual(response.status_code, 400)
        self.c.refresh_from_db()
        self.assertNotEqual(self.c.nome_fantasia, self.data['nome_institucional'])
        self.assertNotIn('vertyx_branding', self.client.session)

    def test_branding_does_not_fall_back_to_first_tenant_or_previous_session(self):
        request = RequestFactory().get('/')
        request.user = AnonymousUser()
        request.session = {'vertyx_branding': {'nome_institucional': 'OTHER_TENANT_SECRET'},
                           'vertyx_branding_org': self.a.pk}
        with tenant_context(None):
            self.assertNotIn('OTHER_TENANT_SECRET', str(get_tenant_branding(request)))
        request.user = self.admin_c.user
        with tenant_context(self.c):
            self.assertNotIn('OTHER_TENANT_SECRET', str(get_tenant_branding(request)))

    def test_scoped_rows_can_be_locked_without_losing_tenant_boundary(self):
        with tenant_context(self.c):
            AcessoMunicipal.objects.create(membership=self.server_user, papel='PROFISSIONAL',
                escopo='UNIDADE', unidade=self.structure[3], atribuido_por=self.admin_c.user)
            rows = scoped_queryset(self.server_user.user, UnidadeMunicipal.objects.select_for_update())
            self.assertFalse(rows.query.distinct)
            self.assertEqual(list(rows.values_list('pk', flat=True)), [self.structure[3].pk])
            self.assertFalse(rows.exclude(organization=self.c).exists())
