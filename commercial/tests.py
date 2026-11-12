from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlparse
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse, resolve
from platform_core.context import tenant_context
from platform_core.models import (
    ERPProductModule, Organization, OrganizationMembership, OrganizationERP,
    MembershipERP, ERPCatalog,
)
from platform_core.saas_models import CORE_SAAS_MODULES, PlanoSaaS
from contas.models import Usuario
from clientes.models import Cliente
from .catalog import DEFAULT_PLANS, DEFAULT_RESOURCES
from .models import PlanoComercial, InteresseComercial


class CommercialTests(TestCase):
    def setUp(self):
        # Independente da ordem da suíte e de flushes de TransactionTestCase.
        for values in DEFAULT_PLANS:
            PlanoComercial.objects.update_or_create(slug=values['slug'], defaults={**values, 'recursos':deepcopy(DEFAULT_RESOURCES)})
        self.plan=PlanoComercial.objects.get(slug='profissional')
        # Commercial offers and SaaS entitlements are separate catalogs. These
        # tests explicitly configure the public professional offer they need.
        PlanoSaaS.objects.update_or_create(
            slug='profissional', defaults={
                'nome':'Profissional', 'plano_comercial':self.plan,
                'preco':self.plan.preco, 'periodicidade':'MENSAL',
                'publico_ativo':True, 'ativo':True,
                'modulos':list(CORE_SAAS_MODULES),
                'limites':{'usuarios':10,'filiais':3,'armazenamento_mb':None,
                           'vendas_mes':None,'ordens_mes':None},
            },
        )

    def payload(self, client=None, **changes):
        response=(client or self.client).get('/demonstracao/?plano=profissional')
        data={'token':response.context['form'].initial['token'],'website':'','plano':'profissional','periodicidade':'MENSAL','nome':'Pessoa sintética','empresa':'Empresa sintética','email':'lead@example.invalid','telefone':'','objetivo':'ASSISTENCIA','mensagem':'Pedido sintético','consentimento':'on'}
        data.update(changes)
        return data

    def role(self,role,slug='commercial-a'):
        org,_=Organization.objects.get_or_create(slug=slug,defaults={'razao_social':'ORGANIZAÇÃO PRIVADA '+slug})
        erp,_=ERPCatalog.objects.get_or_create(code='assistencia',defaults={'name':'Assistência de teste'})
        for code, name in (
            ('assistencia','Assistência'),
            ('pwa_tecnico','PWA Técnico'),
            ('pwa_vendedor','PWA Vendedor'),
        ):
            ERPProductModule.objects.get_or_create(
                erp=erp, code=code,
                defaults={'name':name,'active':True,'sort_order':0},
            )
        OrganizationERP.objects.get_or_create(organization=org,erp=erp)
        user=Usuario.objects.create_user(username='marketing-'+role+'-'+slug,password='only-test-password',perfil='ADMIN' if role=='VENDEDOR' else role)
        membership=OrganizationMembership.objects.create(user=user,organization=org,role=role)
        MembershipERP.objects.create(membership=membership,erp=erp)
        return user,org

    def test_home_publica_sem_erp_privado(self):
        response=self.client.get('/')
        self.assertContains(response,'Gestão inteligente')
        self.assertContains(response,'commercial-site')
        self.assertNotContains(response,'menuPrincipal')
        self.assertNotContains(response,'/static/js/vertyx.js')
        self.assertNotContains(response,'serviceWorker.register')

    def test_vitrine_geral_apresenta_produtos_sem_expor_administracao(self):
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        for label in (
            'Tecnologia para transformar a gestão.',
            'ERP EMPRESARIAL',
            'ERP GESTÃO PÚBLICA',
            'APLICATIVOS E PWA',
            'Assistência Técnica, PDV, vendas, estoque, financeiro, contratos e ordens de serviço',
            'Conhecer Gestão Pública',
            'Uma plataforma.',
        ):
            with self.subTest(label=label):self.assertContains(response,label)
        self.assertContains(response,'href="/gestao-publica/"')
        for private_path in ('/vertyx/saas/','/vertyx/saas/bootstrap-inicial/','/admin/'):
            with self.subTest(path=private_path):self.assertNotContains(response,private_path)
        for url in ('/gestao-publica/','/gestao-publica/login/'):
            with self.subTest(url=url):self.assertEqual(self.client.get(url).status_code,200)

    def test_quatro_planos_precos_destaque_e_limites_indefinidos(self):
        response=self.client.get('/planos/')
        self.assertEqual(len(response.context['plans']),4)
        self.assertEqual([p['price'] for p in response.context['plans']],['149','249','399',''])
        self.assertEqual([p['plan'].slug for p in response.context['plans'] if p['plan'].destaque],[])
        self.assertNotContains(response,'MAIS ESCOLHIDO')
        self.assertContains(response,'A definir na proposta')
        self.assertContains(response,'Fale conosco')
        self.assertNotContains(response,'Ilimitado')

    def test_catalogo_comercial_mantem_o_nucleo_operacional_incluido_em_todos_os_planos(self):
        response=self.client.get('/planos/')
        for plan in PlanoComercial.objects.filter(ativo=True):
            for key in ('clientes','produtos','estoque','pdv','financeiro','fiscal'):
                with self.subTest(plan=plan.slug,feature=key):
                    self.assertEqual(plan.recursos.get(key),'INCLUIDO')
        self.assertContains(response,'Fiscal / NF')
        self.assertContains(response,'emissão ou transmissão de documentos fiscais depende de configuração tributária')

    def test_preco_nome_limites_configuraveis(self):
        from platform_core.saas_models import PlanoSaaS
        self.plan.preco=Decimal('179.90');self.plan.nome='Plano configurado';self.plan.save()
        saas=PlanoSaaS.objects.get(plano_comercial=self.plan,publico_ativo=True)
        saas.preco=self.plan.preco;saas.limites={**saas.limites,'usuarios':8};saas.save()
        response=self.client.get('/planos/')
        self.assertContains(response,'179,90')
        self.assertContains(response,'Plano configurado')
        self.assertContains(response,'Usuários: 8')

    def test_ordem_ativo_e_destaque_configuraveis(self):
        self.plan.ativo=False;self.plan.save()
        custom=PlanoComercial.objects.get(slug='personalizado');custom.ordem=1;custom.destaque=True;custom.save()
        response=self.client.get('/planos/')
        self.assertEqual(len(response.context['plans']),3)
        self.assertEqual(response.context['plans'][0]['plan'].slug,'personalizado')
        self.assertNotContains(response,'data-plan="profissional"')

    def test_recursos_configuraveis_sem_promessas_futuras(self):
        self.plan.recursos={'estoque':'INCLUIDO','fiscal':'FUTURO','gateway':'FUTURO'};self.plan.save()
        response=self.client.get('/planos/')
        self.assertContains(response,'Incluído')
        for promise in ['NF-e','NFS-e','NFC-e','Emissão fiscal','certificado A1']:
            self.assertNotContains(response,promise)
        self.assertContains(response,'não realiza cobrança nem integra cartão, PIX ou gateway')

    def test_recurso_desconhecido_ou_estado_invalido_rejeitado(self):
        for resources in [{'x':'INCLUIDO'},{'erp':'SIM'},[]]:
            self.plan.recursos=resources
            with self.assertRaises(ValidationError):self.plan.save()

    def test_valores_negativos_rejeitados(self):
        self.plan.preco=Decimal('-0.01')
        with self.assertRaises(ValidationError):self.plan.save()

    def test_anual_sem_preco_nao_inventa_desconto(self):
        response=self.client.get('/planos/?periodo=ANUAL')
        self.assertFalse(response.context['annual_available'])
        self.assertEqual(response.context['period'],'MENSAL')
        self.assertContains(response,'Condições anuais sob consulta')

    def test_preco_anual_com_decimal_e_arredondamento(self):
        self.plan.preco=Decimal('249.99');self.plan.desconto_anual=Decimal('12.50');self.plan.save()
        self.assertEqual(self.plan.valor_anual,Decimal('2624.90'))
        response=self.client.get('/planos/?periodo=ANUAL')
        self.assertContains(response,'2.624,90')
        self.assertContains(response,'/ano')
        data=self.payload(periodicidade='ANUAL')
        self.assertEqual(self.client.post('/demonstracao/',data).status_code,302)
        self.assertEqual(InteresseComercial.objects.get().oferta['preco'],'2624.90')

    def test_anual_por_consulta_nao_gera_valor_falso(self):
        self.assertEqual(self.client.post('/demonstracao/',self.payload(periodicidade='ANUAL')).status_code,302)
        self.assertIsNone(InteresseComercial.objects.get().oferta['preco'])

    def test_plano_primariamente_anual_periodo_e_link_corretos(self):
        self.plan.periodicidade='ANUAL';self.plan.preco=Decimal('1900.00');self.plan.save()
        response=self.client.get('/planos/')
        card=next(c for c in response.context['plans'] if c['plan'].pk==self.plan.pk)
        self.assertEqual(card['period'],'ano');self.assertIn('periodo=ANUAL',card['cta'])
        self.assertEqual(self.client.post('/demonstracao/',self.payload()).status_code,400)
        self.assertEqual(self.client.post('/demonstracao/',self.payload(periodicidade='ANUAL')).status_code,302)

    def test_links_planos_formulario_e_login_existente(self):
        response=self.client.get('/planos/')
        for card in response.context['plans']:
            target=self.client.get(card['cta'])
            if card['contracting_available']:
                self.assertEqual(target.status_code,200)
                self.assertEqual(target.context['plan'].plano_comercial,card['plan'])
            else:
                self.assertEqual(target.status_code,200)
                self.assertEqual(target.context['form'].initial['plano'],card['plan'])
        self.assertContains(response,'href="/login/"')
        self.assertTemplateUsed(self.client.get('/login/'),'registration/login.html')
        self.assertTemplateUsed(self.client.get('/contas/login/'),'registration/login.html')

    def test_menu_mobile_acesso_unico_oficial_sem_redirecionar_visitante(self):
        from contas.views import LoginView
        before=(Organization.objects.count(),Usuario.objects.count(),OrganizationMembership.objects.count(),InteresseComercial.objects.count())
        for url in ['/', '/planos/']:
            response=self.client.get(url)
            self.assertEqual(response.status_code,200)
            self.assertNotIn('Location',response)
            html=response.content.decode()
            menu=html.split('<nav id="public-menu"',1)[1].split('</nav>',1)[0]
            labels=['Planos e preços','Sobre nós','Entrar','Solicitar demonstração']
            positions=[menu.index(label) for label in labels]
            self.assertEqual(positions,sorted(positions))
            self.assertNotIn('Soluções',menu)
            self.assertNotIn('solutions-dropdown',menu)
            self.assertNotIn('ERP Gestão Pública',menu)
            self.assertEqual(menu.count('href="/login/"'),1)
            self.assertIn('class="button button-quiet header-login"',menu)
            self.assertContains(self.client.get('/login/'),'name="username"')
            self.assertContains(self.client.get('/login/'),'name="password"')
        self.assertIs(resolve('/login/').func.view_class,LoginView)
        self.assertIs(resolve('/contas/login/').func.view_class,LoginView)
        self.assertEqual(before,(Organization.objects.count(),Usuario.objects.count(),OrganizationMembership.objects.count(),InteresseComercial.objects.count()))

    def test_tecnico01_login_do_menu_usa_membership_e_preserva_isolamento(self):
        user,org=self.role('TECNICO','commercial-mobile-login')
        user.username='tecnico01';user.perfil='ADMIN';user.save()
        self.assertContains(self.client.get('/'),'Entrar')
        response=self.client.post('/login/',{'username':'tecnico01','password':'only-test-password','next':'/assistencia/'},follow=True)
        self.assertEqual(response.redirect_chain[0],('/tecnico/',302))
        self.assertContains(response,'Meus atendimentos')
        self.assertEqual(self.client.session['organization_id'],org.pk)
        for url in ['/financeiro/','/contas/usuarios/','/configuracoes/']:
            self.assertEqual(self.client.get(url).status_code,403)
        _,foreign=self.role('ADMIN','commercial-mobile-other')
        self.assertEqual(self.client.post(reverse('platform_core:select_organization'),{'organization':str(foreign.public_id)}).status_code,404)
        self.assertEqual(self.client.session['organization_id'],org.pk)

    def test_lead_registrado_sem_organizacao_usuario_ou_assinatura(self):
        before=(Organization.objects.count(),Usuario.objects.count(),OrganizationMembership.objects.count())
        response=self.client.post('/demonstracao/',self.payload())
        self.assertRedirects(response,'/solicitacao-recebida/')
        lead=InteresseComercial.objects.get()
        self.assertEqual(lead.oferta['preco'],'249.00')
        self.assertTrue(lead.consentimento)
        self.assertEqual(before,(Organization.objects.count(),Usuario.objects.count(),OrganizationMembership.objects.count()))
        self.assertNotContains(self.client.get('/solicitacao-recebida/'),lead.email)

    def test_oferta_preservada_apos_alteracao_preco(self):
        self.client.post('/demonstracao/',self.payload())
        self.plan.preco=Decimal('888.00');self.plan.save()
        self.assertEqual(InteresseComercial.objects.get().oferta['preco'],'249.00')

    def test_envio_repetido_idempotente(self):
        data=self.payload()
        for _ in range(2):self.assertEqual(self.client.post('/demonstracao/',data).status_code,302)
        self.assertEqual(InteresseComercial.objects.count(),1)

    def test_reuso_token_com_payload_alterado_bloqueado(self):
        data=self.payload();self.client.post('/demonstracao/',data)
        data['email']='different@example.invalid'
        self.assertEqual(self.client.post('/demonstracao/',data).status_code,400)
        self.assertEqual(InteresseComercial.objects.count(),1)

    def test_token_outro_visitante_e_token_adulterado_bloqueados(self):
        data=self.payload();other=Client()
        self.assertEqual(other.post('/demonstracao/',data).status_code,400)
        data['token']='adulterado'
        self.assertEqual(self.client.post('/demonstracao/',data).status_code,400)
        self.assertEqual(InteresseComercial.objects.count(),0)

    def test_token_expirado_bloqueado(self):
        data=self.payload()
        with patch('commercial.services.signing.time.time',return_value=9999999999):
            self.assertEqual(self.client.post('/demonstracao/',data).status_code,400)
        self.assertFalse(InteresseComercial.objects.exists())

    def test_token_ausente_mostra_erro_visivel(self):
        data=self.payload();data.pop('token')
        self.assertContains(self.client.post('/demonstracao/',data),'Recarregue a página',status_code=400)

    def test_csrf_obrigatorio(self):
        strict=Client(enforce_csrf_checks=True)
        data=self.payload(client=strict)
        self.assertEqual(strict.post('/demonstracao/',data).status_code,403)
        self.assertFalse(InteresseComercial.objects.exists())

    def test_consentimento_obrigatorio_e_honeypot(self):
        for changes in [{'consentimento':''},{'website':'spam.example.invalid'}]:
            self.assertEqual(self.client.post('/demonstracao/',self.payload(**changes)).status_code,400)
        self.assertFalse(InteresseComercial.objects.exists())

    def test_limite_envios_email_e_normalizacao(self):
        for _ in range(5):self.assertEqual(self.client.post('/demonstracao/',self.payload(email='LEAD@example.invalid')).status_code,302)
        self.assertEqual(self.client.post('/demonstracao/',self.payload()).status_code,400)
        self.assertEqual(InteresseComercial.objects.filter(email='lead@example.invalid').count(),5)

    def test_plano_inativo_ou_inexistente_nao_aceito(self):
        data=self.payload();self.plan.ativo=False;self.plan.save()
        self.assertEqual(self.client.get('/demonstracao/?plano=profissional').status_code,404)
        self.assertEqual(self.client.get('/demonstracao/?plano=inexistente').status_code,404)
        self.assertEqual(self.client.post('/demonstracao/',data).status_code,400)

    def test_confirmacao_sem_envio_redireciona(self):
        self.assertRedirects(self.client.get('/solicitacao-recebida/'),'/demonstracao/')

    def test_paginas_informativas_e_robots(self):
        for url in ['/contato/','/suporte/','/termos/','/privacidade/','/robots.txt']:
            self.assertEqual(self.client.get(url).status_code,200)
        self.assertContains(self.client.get('/robots.txt'),'Disallow: /ordens/')

    def test_anonimo_nao_acessa_dados_internos(self):
        for url in ['/clientes/','/equipamentos/','/ordens/','/financeiro/','/tecnico/','/pdv/']:
            self.assertEqual(self.client.get(url).status_code,302)
            self.assertIn('/pdv/entrar/' if url=='/pdv/' else '/contas/login/',self.client.get(url)['Location'])

    def test_nenhum_tenant_cliente_lead_exposto_no_catalogo(self):
        user,a=self.role('ADMIN')
        _,b=self.role('GESTOR','commercial-b')
        for org in [a,b]:
            with tenant_context(org):Cliente.objects.create(nome_razao_social='CLIENTE SEGREDO '+org.slug,cpf_cnpj='PRIVATE-'+org.slug)
        self.client.post('/demonstracao/',self.payload(email='PRIVATE@example.invalid'))
        for url in ['/','/planos/','/suporte/']:
            response=self.client.get(url)
            for text in ['ORGANIZAÇÃO PRIVADA','CLIENTE SEGREDO','PRIVATE@example.invalid']:
                self.assertNotContains(response,text)
        self.client.force_login(user)
        self.assertNotContains(self.client.get('/planos/'),'CLIENTE SEGREDO')

    def test_catalogo_e_leads_somente_superusuario_plataforma(self):
        user,_=self.role('ADMIN');user.is_staff=True;user.save()
        self.client.force_login(user)
        for model in [PlanoComercial,InteresseComercial]:
            self.assertEqual(self.client.get('/admin/commercial/'+model._meta.model_name+'/').status_code,403)
            request=RequestFactory().get('/admin/');request.user=user
            self.assertFalse(admin.site._registry[model].has_view_permission(request))
        user.is_superuser=True;user.save()
        request.user=user
        self.assertTrue(admin.site._registry[PlanoComercial].has_change_permission(request))
        self.assertFalse(admin.site._registry[InteresseComercial].has_delete_permission(request))

    def test_admin_gestor_login_e_erp_preservados(self):
        for role in ['ADMIN','GESTOR']:
            user,_=self.role(role)
            self.assertEqual(self.client.post('/login/',{'username':user.username,'password':'only-test-password'}).status_code,302)
            # Anonymous visitors see the commercial root; authenticated users
            # keep the established ERP entry redirect.
            self.assertRedirects(self.client.get('/'),'/vertyx/',fetch_redirect_response=False)
            self.assertEqual(self.client.get('/assistencia/').status_code,200)
            self.client.logout()

    def test_tecnico_login_operacional_e_urls_admin_bloqueadas(self):
        user,_=self.role('TECNICO')
        self.assertRedirects(self.client.post('/login/',{'username':user.username,'password':'only-test-password'}),'/tecnico/')
        self.assertRedirects(self.client.get('/vertyx/'),'/tecnico/')
        self.assertEqual(self.client.get('/planos/').status_code,200)
        for url in ['/financeiro/','/contas/usuarios/','/configuracoes/']:
            self.assertEqual(self.client.get(url).status_code,403)

    def test_vendedor_entrada_pdv(self):
        user,_=self.role('VENDEDOR')
        self.client.post('/login/',{'username':user.username,'password':'only-test-password'})
        self.assertRedirects(self.client.get('/vertyx/'),'/vendedor/')
        self.assertContains(self.client.get('/vertyx/?organizacoes=1'),'href="/vendedor/"')

    def test_seo_heading_e_escape_conteudo_configurado(self):
        self.plan.descricao='<script>alert("x")</script>';self.plan.save()
        response=self.client.get('/planos/')
        self.assertContains(response,'<h1>Planos e preços</h1>',html=True)
        self.assertContains(response,'rel="canonical"')
        self.assertContains(response,'property="og:image"')
        self.assertContains(response,'name="description"')
        self.assertContains(response,'&lt;script&gt;')
        self.assertNotContains(response,'<script>alert')
