"""Conector de Comunicação REST + mTLS com o Ambiente de Dados Nacional (ADN) da NFS-e."""
import base64
import gzip
import json
import ssl
import urllib.request
import urllib.error
from typing import Dict, Any, Tuple
from dataclasses import dataclass
from django.conf import settings
from django.core.exceptions import ValidationError

URL_ADN_HOMOLOGACAO = "https://adn.producaorestrita.nfse.gov.br"
URL_ADN_PRODUCAO = "https://adn.nfse.gov.br"


@dataclass
class RespostaADN:
    sucesso: bool
    status_http: int
    chave_acesso: str = ""
    protocolo: str = ""
    mensagem: str = ""
    xml_retorno: bytes = b""
    erros: list = None


class ClienteADN:
    """Cliente HTTP para comunicação com a API REST do Ambiente de Dados Nacional (ADN)."""

    def __init__(self, ambiente: str = 'HOMOLOGACAO', timeout: int = 30):
        self.ambiente = ambiente
        self.base_url = URL_ADN_HOMOLOGACAO if ambiente == 'HOMOLOGACAO' else URL_ADN_PRODUCAO
        self.timeout = timeout

    def preparar_payload_dps(self, dps_xml_bytes: bytes) -> str:
        """Comprime o XML da DPS assinada em GZip e codifica em Base64."""
        buf = gzip.compress(dps_xml_bytes)
        return base64.b64encode(buf).decode('ascii')

    def criar_contexto_ssl(self, cert_pem_path: str = None, key_pem_path: str = None) -> ssl.SSLContext:
        """Cria o contexto SSL para autenticação mTLS com o certificado digital ICP-Brasil."""
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        if cert_pem_path and key_pem_path:
            ctx.load_cert_chain(certfile=cert_pem_path, keyfile=key_pem_path)
        return ctx

    def emitir_dps(
        self,
        dps_xml_assinado: bytes,
        cert_pem_path: str = None,
        key_pem_path: str = None,
        simulacao: bool = False
    ) -> RespostaADN:
        """Transmite a DPS assinada para o endpoint POST /nfse do ADN."""
        if simulacao:
            # Em modo simulação/QA, devolve mock autorizado determinístico
            import hashlib
            h = hashlib.sha256(dps_xml_assinado).hexdigest()[:16].upper()
            chave_mock = f"3526111122233300018155001{h:0>26}"[:50]
            return RespostaADN(
                sucesso=True,
                status_http=200,
                chave_acesso=chave_mock,
                protocolo=f"ADN-HOM-{h}",
                mensagem="DPS recebida e autorizada com sucesso no Ambiente de Dados Nacional.",
                xml_retorno=dps_xml_assinado,
                erros=[]
            )

        if not getattr(settings, 'EXTERNAL_FISCAL_ENABLED', True):
            raise ValidationError('Transmissões fiscais externas desabilitadas neste ambiente.')

        url = f"{self.base_url}/nfse"
        payload_b64 = self.preparar_payload_dps(dps_xml_assinado)
        corpo = json.dumps({"dpsXmlGZipB64": payload_b64}).encode('utf-8')

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "VERTYX-ERP/1.0"
        }

        ssl_ctx = self.criar_contexto_ssl(cert_pem_path, key_pem_path)
        req = urllib.request.Request(url, data=corpo, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=self.timeout) as resp:
                status = resp.getcode()
                dados = json.loads(resp.read().decode('utf-8'))
                chave = dados.get('chaveAcesso', '')
                proto = dados.get('protocolo', '')
                return RespostaADN(
                    sucesso=True,
                    status_http=status,
                    chave_acesso=chave,
                    protocolo=proto,
                    mensagem=dados.get('mensagem', 'NFS-e autorizada com sucesso.'),
                    erros=[]
                )
        except urllib.error.HTTPError as exc:
            corpo_erro = exc.read().decode('utf-8', errors='ignore')
            try:
                detalhes = json.loads(corpo_erro)
                msgs = detalhes.get('erros', [detalhes.get('mensagem', corpo_erro)])
            except Exception:
                msgs = [corpo_erro]
            return RespostaADN(
                sucesso=False,
                status_http=exc.code,
                mensagem="Rejeição no Ambiente de Dados Nacional (ADN).",
                erros=msgs
            )
        except Exception as exc:
            return RespostaADN(
                sucesso=False,
                status_http=500,
                mensagem=f"Falha de transporte/conexão mTLS com o ADN: {str(exc)}",
                erros=[str(exc)]
            )
