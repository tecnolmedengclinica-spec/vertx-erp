"""Testes do Motor de NFS-e Nacional (Padrão ADN/SEFIN, DPS XML, XMLDSig e DANFSE)."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import uuid
from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError
from lxml import etree
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes
from cryptography import x509
from cryptography.x509.oid import NameOID

from platform_core.models import Organization, FilialEmpresa
from platform_core.context import tenant_context
from operacao.models import (
    DocumentoFiscal, DetalhesNFSe, ConfiguracaoFiscal,
    SerieFiscal, SolicitacaoFiscal
)
from fiscal.motors import resolver
from fiscal.motors.nfse_adn import MotorNFSeADN
from fiscal.nfse.contracts import (
    DadosDPS, PrestadorDPS, TomadorDPS, ServicoDPS, ValoresDPS,
    TipoAmbiente, EnderecoNacional, OpcaoSimplesNacional
)
from fiscal.nfse.dps import construir_xml_dps, dps_para_xml_bytes, gerar_chave_dps
from fiscal.nfse.signature import assinar_xml_dps
from fiscal.nfse.danfse import gerar_danfse_pdf
from fiscal.nfse.adn import ClienteADN


def gerar_certificado_teste():
    """Gera par de chaves RSA e certificado X.509 ICP-Brasil simulado para testes locais."""
    chave_privada = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )
    sujeito = emissor = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "SP"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VERTYX TECNOLOGIA"),
        x509.NameAttribute(NameOID.COMMON_NAME, "VERTYX TESTE A1:11222333000181"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(sujeito)
        .issuer_name(emissor)
        .public_key(chave_privada.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow() - timedelta(days=1))
        .not_valid_after(datetime.utcnow() + timedelta(days=365))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .sign(chave_privada, hashes.SHA256())
    )
    return chave_privada, cert


class NFSeNacionalTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.user, _ = User.objects.get_or_create(username="admin_fiscal_test", defaults={"password": "pwd"})
        self.org, _ = Organization.objects.get_or_create(
            slug="vertyx-med-test",
            defaults={
                "razao_social": "Vertyx Engenharia Clínica Ltda",
                "nome_fantasia": "Vertyx Med",
                "cnpj": "39966868000186"
            }
        )
        with tenant_context(self.org):
            from platform_core.models import ERPCatalog, OrganizationERP, MembershipERP, OrganizationMembership
            erp, _ = ERPCatalog.objects.get_or_create(code="assistencia", defaults={"name": "Assistência Técnica", "active": True})
            OrganizationERP.objects.get_or_create(organization=self.org, erp=erp, defaults={"enabled": True})
            self.member, _ = OrganizationMembership.objects.get_or_create(user=self.user, organization=self.org, defaults={"role": "ADMIN"})
            MembershipERP.objects.get_or_create(membership=self.member, erp=erp, defaults={"enabled": True})

            self.filial, _ = FilialEmpresa.objects.get_or_create(
                organization=self.org,
                codigo="001",
                defaults={
                    "nome": "Matriz São Paulo",
                    "cnpj": self.org.cnpj,
                    "inscricao_estadual": "12345678",
                    "endereco": "Av. Paulista, 1000",
                    "ativo": True
                }
            )
            self.config, _ = ConfiguracaoFiscal.objects.get_or_create(
                organization=self.org,
                estabelecimento=self.filial,
                tipo="NFSE",
                ambiente="HOMOLOGACAO",
                versao=1,
                defaults={
                    "motor": "NFSE_ADN_HOM",
                    "habilitada": True
                }
            )
            self.serie, _ = SerieFiscal.objects.get_or_create(
                organization=self.org,
                estabelecimento=self.filial,
                tipo="NFSE",
                ambiente="HOMOLOGACAO",
                serie="1"
            )

    def test_resolver_nfse_adn(self):
        """Verifica a resolução do motor NFS-e Nacional sem afetar NFC-e."""
        m1 = resolver("NFSE_ADN", "HOMOLOGACAO")
        self.assertIsInstance(m1, MotorNFSeADN)

        m2 = resolver("NFSE", "HOMOLOGACAO", motor="NFSE_ADN_HOM")
        self.assertIsInstance(m2, MotorNFSeADN)

        # Chamada direta sem parâmetro de motor continua levantando ValidationError para proteger suite legado
        with self.assertRaises(ValidationError):
            resolver("NFSE", "HOMOLOGACAO")

    def test_construcao_xml_dps(self):
        """Valida a construção do XML da DPS seguindo a especificação v1.00."""
        dados = DadosDPS(
            serie="1",
            numero_dps=105,
            data_competencia="2026-11-09",
            data_hora_emissao="2026-11-09T18:00:00",
            ambiente=TipoAmbiente.HOMOLOGACAO,
            prestador=PrestadorDPS(
                cnpj="11222333000181",
                razao_social="Vertyx Engenharia Clínica Ltda",
                inscricao_municipal="123456",
                opcao_simples=OpcaoSimplesNacional.NAO_OPTANTE
            ),
            tomador=TomadorDPS(
                cnpj_cpf="44555666000199",
                razao_social_nome="Hospital Geral Central",
                endereco=EnderecoNacional(
                    municipio_ibge="3550308",
                    cep="01310100",
                    logradouro="Rua Hospitalar",
                    numero="500",
                    bairro="Bela Vista",
                    uf="SP"
                )
            ),
            servico=ServicoDPS(
                codigo_tributacao_nacional="14.01.01",
                discriminacao="Calibração e manutenção preventiva de monitores multiparamétricos",
                municipio_prestacao_ibge="3550308",
                aliquota_iss=Decimal("5.00")
            ),
            valores=ValoresDPS(
                valor_servicos=Decimal("3500.00")
            )
        )

        chave = gerar_chave_dps(dados)
        self.assertEqual(len(chave), 42)

        xml_elem = construir_xml_dps(dados)
        xml_bytes = etree.tostring(xml_elem, encoding="utf-8")
        self.assertIn(b'<infDPS Id="DPS' + chave.encode(), xml_bytes)
        self.assertIn(b"<tpAmb>2</tpAmb>", xml_bytes)
        self.assertIn(b"<cTribNac>140101</cTribNac>", xml_bytes)
        self.assertIn(b"<vServ>3500.00</vServ>", xml_bytes)

    def test_assinatura_digital_xmldsig(self):
        """Valida a assinatura XMLDSig ICP-Brasil RSA-SHA256 da DPS."""
        dados = DadosDPS(
            serie="1",
            numero_dps=106,
            data_competencia="2026-11-09",
            data_hora_emissao="2026-11-09T18:00:00",
            ambiente=TipoAmbiente.HOMOLOGACAO,
            prestador=PrestadorDPS(
                cnpj="11222333000181",
                razao_social="Vertyx Engenharia Clínica Ltda"
            ),
            tomador=TomadorDPS(
                cnpj_cpf="44555666000199",
                razao_social_nome="Hospital Geral Central"
            ),
            servico=ServicoDPS(
                codigo_tributacao_nacional="14.01.01",
                discriminacao="Manutenção de Equipamentos Médicos",
                municipio_prestacao_ibge="3550308"
            ),
            valores=ValoresDPS(valor_servicos=Decimal("1500.00"))
        )
        xml_bruto = dps_para_xml_bytes(dados)
        chave_priv, cert = gerar_certificado_teste()

        xml_assinado = assinar_xml_dps(xml_bruto, chave_priv, cert)
        self.assertIn(b"<Signature", xml_assinado)
        self.assertIn(b"<SignedInfo>", xml_assinado)
        self.assertIn(b"<SignatureValue>", xml_assinado)
        self.assertIn(b"<X509Certificate>", xml_assinado)

        # Parsing do documento assinado
        root = etree.fromstring(xml_assinado)
        ns = {"ds": "http://www.w3.org/2000/09/xmldsig#"}
        sig = root.find("ds:Signature", namespaces=ns)
        self.assertIsNotNone(sig)

    def test_geracao_danfse_pdf(self):
        """Valida a renderização gráfica do DANFSE com QR Code."""
        danfse_dados = {
            'chave_acesso': '35503082261122233300018100001000000000001068',
            'numero_nfse': '106',
            'data_emissao': '09/11/2026 18:30',
            'competencia': '2026-11-09',
            'prestador': {
                'razao_social': 'VERTYX ENGENHARIA CLÍNICA LTDA',
                'cnpj': '11.222.333/0001-81',
                'municipio': '3550308 - São Paulo/SP',
                'im': '12345678'
            },
            'tomador': {
                'razao_social_nome': 'HOSPITAL GERAL CENTRAL',
                'cnpj_cpf': '44.555.666/0001-99',
                'endereco': 'Av. Paulista, 500, São Paulo/SP',
                'email': 'financeiro@hospitalcentral.com.br'
            },
            'servico': {
                'discriminacao': 'Manutenção preventiva em ventiladores pulmonares.',
                'codigo_tributacao': '14.01.01'
            },
            'valores': {
                'valor_servicos': '3.500,00',
                'valor_deducoes': '0,00',
                'base_calculo': '3.500,00',
                'aliquota': '5,00',
                'valor_iss': '175,00',
                'valor_liquido': '3.500,00'
            }
        }
        pdf_bytes = gerar_danfse_pdf(danfse_dados)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertGreater(len(pdf_bytes), 1500)

    def test_emissao_completa_motor_nfse_adn(self):
        """Valida o ciclo completo de emissão com persistência de SolicitacaoFiscal."""
        with tenant_context(self.org):
            doc = DocumentoFiscal(
                organization=self.org,
                estabelecimento=self.filial,
                configuracao_fiscal=self.config,
                serie_fiscal=self.serie,
                tipo="NFSE",
                modelo="NFSE",
                ambiente="HOMOLOGACAO",
                numero=201,
                valor=Decimal("4200.00"),
                status="PENDENTE",
                descricao="Manutenção Corretiva em Tomógrafo Computadorizado",
                criado_por=self.user
            )
            doc._service_write = True
            doc.save()

            detalhes = DetalhesNFSe(
                documento=doc,
                competencia=date(2026, 11, 9),
                codigo_servico="14.01.01",
                municipio_incidencia="3550308",
                identificador_dps_rps="DPS201"
            )
            detalhes._service_write = True
            detalhes.save()

            solicitacao = SolicitacaoFiscal(
                organization=self.org,
                documento=doc,
                request_id=uuid.uuid4(),
                operacao="EMITIR",
                solicitante=self.user,
                payload_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            )
            solicitacao._service_write = True
            solicitacao.save()

            motor = MotorNFSeADN()
            chave_priv, cert = gerar_certificado_teste()

            resultado = motor.emitir(solicitacao, chave_privada=chave_priv, certificado=cert)
            self.assertEqual(resultado["status"], "AUTORIZADA")
            self.assertIn("chave", resultado)
            self.assertIn("protocolo", resultado)
            self.assertGreater(resultado["danfse_pdf_tamanho"], 1000)

            solicitacao.refresh_from_db()
            self.assertEqual(solicitacao.resultado_simulado["status"], "AUTORIZADA")

            # Valida endpoint de visualização do DANFSE PDF
            from django.urls import reverse
            self.client.force_login(self.user)
            session = self.client.session
            session["organization_id"] = self.org.id
            session.save()

            response = self.client.get(reverse("operacao:nfse_danfse", args=[doc.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertTrue(response.content.startswith(b"%PDF-"))
