"""Homologation never opens SMTP or banking connections, even with saved settings."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings
from .banking import BaseBancoProvider
from .email_service import enviar_email_os_concluida, testar_smtp


class HomologationExternalGuardsTests(SimpleTestCase):
    @override_settings(EXTERNAL_EMAIL_ENABLED=False)
    def test_smtp_probe_is_blocked_before_reading_credentials_or_opening_network(self):
        with patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as ssl:
            ok, message = testar_smtp(None, 'synthetic@example.invalid')
        self.assertFalse(ok)
        self.assertIn('desabilitado', message)
        smtp.assert_not_called()
        ssl.assert_not_called()

    @override_settings(EXTERNAL_EMAIL_ENABLED=False)
    def test_order_notification_is_blocked_before_loading_tenant_configuration(self):
        with patch('operacao.email_service.obter_configuracao_email') as config:
            ok, _ = enviar_email_os_concluida(None)
        self.assertFalse(ok)
        config.assert_not_called()

    @override_settings(EXTERNAL_BANKING_ENABLED=False)
    def test_production_banking_is_blocked_before_resolving_any_secret(self):
        provider = BaseBancoProvider(SimpleNamespace(ambiente='PRODUCAO'))
        with patch.object(provider, '_credencial') as credential, patch('urllib.request.urlopen') as request:
            with self.assertRaises(ValidationError):
                provider._http_json('POST', 'https://provider.example.invalid')
        credential.assert_not_called()
        request.assert_not_called()

    @override_settings(EXTERNAL_FISCAL_ENABLED=False)
    def test_adn_is_blocked_before_loading_certificate_or_opening_network(self):
        from fiscal.nfse.adn import ClienteADN
        client = ClienteADN(ambiente='PRODUCAO')
        with patch.object(client, 'criar_contexto_ssl') as certificate, patch('urllib.request.urlopen') as request:
            with self.assertRaises(ValidationError):
                client.emitir_dps(b'<synthetic/>')
        certificate.assert_not_called()
        request.assert_not_called()

    @override_settings(EXTERNAL_FISCAL_ENABLED=False)
    def test_nfe_all_transport_operations_are_blocked(self):
        from fiscal.nfe.sefaz import ClienteSEFAZNFe
        transport = Mock()
        client = ClienteSEFAZNFe(transporte=transport, simulacao=False)
        for operation in (
            lambda: client.autorizar(b'<synthetic/>', chave='0' * 44),
            lambda: client.consultar_chave('0' * 44),
            lambda: client.enviar_evento(b'<synthetic/>', chave='0' * 44, tp_evento='110111'),
        ):
            with self.assertRaises(ValidationError):
                operation()
        transport.enviar.assert_not_called()
