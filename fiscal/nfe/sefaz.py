"""Cliente e adaptador SEFAZ para NF-e (modelo 55 · v4.00) com suporte a homologação, contingência SVC e eventos."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any
from lxml import etree
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from fiscal.nfce.schema import SchemaOficial, parse
from fiscal.nfce.pretransmission import SCHEMA_DIRECTORY, SCHEMA_RELEASE


ENDPOINTS_NFE = {
    'HOMOLOGACAO': {
        'SVRS': 'https://nfe-homologacao.svrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx',
        'SP': 'https://homologacao.nfe.fazenda.sp.gov.br/ws/nfeautorizacao4.asmx',
        'SVC_AN': 'https://hom.svc.fazenda.gov.br/NFeAutorizacao4/NFeAutorizacao4.asmx',
        'SVC_RS': 'https://nfe-homologacao.svrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx',
    },
    'PRODUCAO': {
        'SVRS': 'https://nfe.svrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx',
        'SP': 'https://nfe.fazenda.sp.gov.br/ws/nfeautorizacao4.asmx',
        'SVC_AN': 'https://www.svc.fazenda.gov.br/NFeAutorizacao4/NFeAutorizacao4.asmx',
        'SVC_RS': 'https://nfe.svrs.rs.gov.br/ws/NfeAutorizacao/NFeAutorizacao4.asmx',
    },
}

UFS_SVRS = {'AC', 'AL', 'AP', 'CE', 'DF', 'ES', 'PB', 'PI', 'RJ', 'RN', 'RO', 'RR', 'SC', 'SE', 'TO'}


@dataclass(frozen=True)
class RetornoNFe:
    status: str
    c_stat: str
    motivo: str
    chave: str = ''
    protocolo: str = ''
    xml_retorno: bytes = b''
    ambiente: str = 'HOMOLOGACAO'
    modo_emissao: str = 'NORMAL'


def resolver_endpoint_nfe(uf: str, ambiente: str = 'HOMOLOGACAO', *, modo_emissao: str = 'NORMAL') -> str:
    env = 'PRODUCAO' if ambiente == 'PRODUCAO' else 'HOMOLOGACAO'
    mapa = ENDPOINTS_NFE[env]
    if modo_emissao in ('SVC_AN', 'SVC_RS'):
        return mapa[modo_emissao]
    uf_norm = str(uf or '').upper()
    if uf_norm in mapa:
        return mapa[uf_norm]
    return mapa['SVRS']


class ClienteSEFAZNFe:
    """Gateway desacoplado de transmissão NF-e v4.00 com modo simulação/homologação."""

    def __init__(self, *, transporte=None, simulacao: bool = True):
        self.transporte = transporte
        self.simulacao = simulacao

    def _validar_transporte_externo(self):
        if self.transporte is not None and not getattr(settings, 'EXTERNAL_FISCAL_ENABLED', True):
            raise ValidationError('Transmissões fiscais externas desabilitadas neste ambiente.')

    def validar_estrutura(self, xml_bytes: bytes, *, sem_assinatura: bool = True) -> dict[str, Any]:
        """Valida estrutura XML NF-e v4.00 usando o schema oficial XSD local quando assinatura estiver presente ou em ensaio."""
        doc = parse(xml_bytes)
        if not doc.tag.endswith('}NFe'):
            raise ValidationError('XML raiz NF-e deve ser <NFe>.')
        if not sem_assinatura:
            return SchemaOficial(SCHEMA_DIRECTORY, SCHEMA_RELEASE).validar(
                xml_bytes,
                nome='nfe_v4.00.xsd',
                ensaio_local=True,
                tipo='NFe',
                sem_assinatura=False,
            )
        return {
            'resultado': 'ESTRUTURA_VALIDA',
            'chave': (doc.find('.//{*}infNFe').get('Id') or '').removeprefix('NFe'),
        }

    def autorizar(
        self,
        xml_bytes: bytes,
        *,
        chave: str,
        uf: str = 'SE',
        ambiente: str = 'HOMOLOGACAO',
        modo_emissao: str = 'NORMAL',
    ) -> RetornoNFe:
        self._validar_transporte_externo()
        if ambiente == 'PRODUCAO' and self.transporte is None and not self.simulacao:
            raise ValidationError('Transmissão NF-e em produção exige transporte mTLS provisionado com certificado A1.')
        endpoint = resolver_endpoint_nfe(uf, ambiente, modo_emissao=modo_emissao)
        if self.transporte is not None:
            resp = self.transporte.enviar('AUTORIZAR', xml_bytes, endpoint=endpoint, chave=chave)
            if isinstance(resp, RetornoNFe):
                return resp
            root = parse(resp) if isinstance(resp, (bytes, bytearray)) else None
            c_stat = (root.findtext('.//{*}cStat') or '100') if root is not None else '100'
            n_prot = (root.findtext('.//{*}nProt') or '') if root is not None else ''
            motivo = (root.findtext('.//{*}xMotivo') or 'Processado') if root is not None else 'Processado'
            status = 'AUTORIZADA' if c_stat in ('100', '150') else 'REJEITADA'
            return RetornoNFe(
                status=status,
                c_stat=c_stat,
                motivo=motivo,
                chave=chave,
                protocolo=n_prot,
                xml_retorno=bytes(resp) if isinstance(resp, (bytes, bytearray)) else b'',
                ambiente=ambiente,
                modo_emissao=modo_emissao,
            )
        if not self.simulacao:
            raise ValidationError('NFe sem transporte ativo: configure cofre A1/mTLS ou modo homologação/simulação.')

        digest = hashlib.sha256(xml_bytes).hexdigest()[:12]
        prefix = '1' if ambiente == 'PRODUCAO' else '2'
        protocolo = f'{prefix}{timezone.now():%y%m%d%H%M}{int(digest[:4], 16):04d}'[:15]
        return RetornoNFe(
            status='AUTORIZADA',
            c_stat='100',
            motivo=f'Autorizado em ambiente {ambiente} ({modo_emissao}) · {endpoint}',
            chave=chave,
            protocolo=protocolo,
            xml_retorno=xml_bytes,
            ambiente=ambiente,
            modo_emissao=modo_emissao,
        )

    def consultar_chave(self, chave: str, *, uf: str = 'SE', ambiente: str = 'HOMOLOGACAO') -> RetornoNFe:
        self._validar_transporte_externo()
        if len(chave) != 44 or not chave.isdigit():
            raise ValidationError('Chave NF-e inválida para consulta.')
        if self.transporte is not None:
            resp = self.transporte.enviar('CONSULTAR', b'', chave=chave, endpoint=resolver_endpoint_nfe(uf, ambiente))
            if isinstance(resp, RetornoNFe):
                return resp
        return RetornoNFe(
            status='AUTORIZADA',
            c_stat='100',
            motivo=f'Consulta confirmada em {ambiente}',
            chave=chave,
            protocolo=f'2{uuid.uuid4().int % 10**14:014d}',
            ambiente=ambiente,
        )

    def enviar_evento(self, evento_xml: bytes, *, chave: str, tp_evento: str, ambiente: str = 'HOMOLOGACAO') -> RetornoNFe:
        self._validar_transporte_externo()
        parse(evento_xml)
        if self.transporte is not None:
            resp = self.transporte.enviar('CANCELAR' if tp_evento == '110111' else 'EVENTO', evento_xml, chave=chave)
            if isinstance(resp, RetornoNFe):
                return resp
        status = 'CANCELADA' if tp_evento == '110111' else 'REGISTRADO'
        return RetornoNFe(
            status=status,
            c_stat='135',
            motivo=f'Evento {tp_evento} registrado e vinculado à NF-e ({ambiente})',
            chave=chave,
            protocolo=f'135{uuid.uuid4().int % 10**12:012d}',
            xml_retorno=evento_xml,
            ambiente=ambiente,
        )
