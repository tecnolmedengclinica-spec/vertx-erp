"""Testes de Configuração de E-mail e Disparos Transacionais (VERTYX Notificações)."""
from datetime import date
from decimal import Decimal
import uuid
from django.test import TestCase, Client
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model

from platform_core.models import Organization, ERPCatalog, OrganizationERP, OrganizationMembership, MembershipERP
from platform_core.context import tenant_context
from clientes.models import Cliente
from equipamentos.models import Equipamento, CategoriaEquipamento
from ordens.models import OrdemServico
from operacao.models import ConfiguracaoNotificacaoEmail
from operacao.email_service import obter_configuracao_email, testar_smtp, enviar_email_os_concluida


class EmailNotificationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user, _ = User.objects.get_or_create(username="admin_notif_test", defaults={"password": "pwd"})
        self.org, _ = Organization.objects.get_or_create(
            slug="org-notif-test",
            defaults={
                "razao_social": "Hospital e Maternidade Central Ltda",
                "nome_fantasia": "Hospital Central",
                "cnpj": "52758249000164",
                "email": "contato@hospitalcentral.com.br"
            }
        )
        with tenant_context(self.org):
            erp, _ = ERPCatalog.objects.get_or_create(code="assistencia", defaults={"name": "Assistência Técnica", "active": True})
            OrganizationERP.objects.get_or_create(organization=self.org, erp=erp, defaults={"enabled": True})
            self.member, _ = OrganizationMembership.objects.get_or_create(user=self.user, organization=self.org, defaults={"role": "ADMIN"})
            MembershipERP.objects.get_or_create(membership=self.member, erp=erp, defaults={"enabled": True})

            self.categoria, _ = CategoriaEquipamento.objects.get_or_create(
                organization=self.org,
                nome="Monitorização",
                defaults={"descricao": "Monitores"}
            )
            self.cliente, _ = Cliente.objects.get_or_create(
                organization=self.org,
                cpf_cnpj="44555666000199",
                defaults={
                    "nome_razao_social": "Clínica Cardiológica Modelo",
                    "email": "financeiro@clinicamodelo.com.br",
                    "tipo_pessoa": "PJ"
                }
            )
            self.equipamento, _ = Equipamento.objects.get_or_create(
                organization=self.org,
                nome="Monitor Multiparamétrico V-500",
                defaults={
                    "cliente": self.cliente,
                    "categoria": self.categoria,
                    "situacao_operacional": "OK"
                }
            )

    def test_configuracao_email_validacoes(self):
        """Valida regras de negócio da configuração SMTP (portas, TLS/SSL)."""
        with tenant_context(self.org):
            config = obter_configuracao_email(self.org)
            self.assertEqual(config.servidor_smtp, "smtp.gmail.com")
            self.assertEqual(config.porta_smtp, 587)
            self.assertTrue(config.usar_tls)
            self.assertFalse(config.usar_ssl)

            # Teste de porta inválida
            config.porta_smtp = 0
            with self.assertRaises(ValidationError):
                config.full_clean()

            # Teste de conflito TLS e SSL simultâneos
            config.porta_smtp = 587
            config.usar_tls = True
            config.usar_ssl = True
            with self.assertRaises(ValidationError):
                config.full_clean()

    def test_simulacao_teste_smtp(self):
        """Valida teste de envio com registro de status em modo simulado."""
        with tenant_context(self.org):
            config = obter_configuracao_email(self.org)
            config.servidor_smtp = "smtp.local"
            ok, msg = testar_smtp(config, "suporte@hospital.com.br")
            self.assertTrue(ok)
            self.assertIn("validados", msg.lower())
            self.assertIsNotNone(config.ultimo_teste_em)
            self.assertEqual(config.ultimo_teste_status, "SIMULADO_OK")

    def test_enviar_email_os_concluida(self):
        """Valida geração de e-mail de conclusão com laudo técnico."""
        with tenant_context(self.org):
            ordem = OrdemServico.objects.create(
                organization=self.org,
                cliente=self.cliente,
                equipamento=self.equipamento,
                tipo_atendimento="CORRETIVA",
                prioridade="ALTA",
                defeito="Não liga display",
                servico_executado="Substituição da fonte de alimentação e calibração",
                status="FINALIZADA"
            )

            ok, msg = enviar_email_os_concluida(ordem)
            self.assertTrue(ok)
            self.assertIn("sucesso", msg.lower())

    def test_view_configuracao_email_get_and_post(self):
        """Valida acesso HTTP 200 à tela de configuração e submissão de formulário."""
        client = Client()
        client.force_login(self.user)
        session = client.session
        session["organization_id"] = self.org.id
        session.save()

        # GET
        url = reverse("operacao:configuracao_email")
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Configurações de E-mail")
        self.assertContains(response, "Servidor SMTP")
        self.assertContains(response, "Obrigatório")

        # POST Salvar
        payload = {
            "action": "salvar",
            "servidor_smtp": "smtp.office365.com",
            "porta_smtp": "587",
            "email_remetente": "atendimento@hospitalcentral.com.br",
            "nome_remetente": "Central de Atendimento",
            "usuario_smtp": "atendimento@hospitalcentral.com.br",
            "senha_smtp": "segredo123",
            "usar_tls": "on",
            "ativo": "on",
            "notificar_conclusao_os": "on",
            "notificar_preventiva": "on",
            "dias_antecedencia_preventiva": "10",
            "notificar_cobranca": "on",
        }
        post_resp = client.post(url, payload)
        self.assertEqual(post_resp.status_code, 302)

        with tenant_context(self.org):
            config = ConfiguracaoNotificacaoEmail.objects.get(organization=self.org)
            self.assertEqual(config.servidor_smtp, "smtp.office365.com")
            self.assertEqual(config.dias_antecedencia_preventiva, 10)
