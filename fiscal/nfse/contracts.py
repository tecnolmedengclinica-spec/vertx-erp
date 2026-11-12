"""Contratos e estruturas de dados da NFS-e Nacional (Padrão ADN/SEFIN)."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict, Any
from enum import Enum


class TipoAmbiente(Enum):
    PRODUCAO = '1'
    HOMOLOGACAO = '2'


class TipoEmitente(Enum):
    PRESTADOR = '1'
    TOMADOR = '2'
    INTERMEDIARIO = '3'


class ExigibilidadeISS(Enum):
    EXIGIVEL = '1'
    NAO_INCIDENCIA = '2'
    ISENCAO = '3'
    IMUNIDADE = '4'
    SUSPENSAO_JUDICIAL = '5'
    SUSPENSAO_ADMINISTRATIVA = '6'


class TipoRetencaoISS(Enum):
    NAO_RETIDO = '1'
    RETIDO_TOMADOR = '2'
    RETIDO_INTERMEDIARIO = '3'


class OpcaoSimplesNacional(Enum):
    NAO_OPTANTE = '1'
    OPTANTE_ME_EPP = '2'
    OPTANTE_MEI = '3'


@dataclass
class EnderecoNacional:
    municipio_ibge: str
    cep: str
    logradouro: str
    numero: str
    bairro: str
    uf: str
    complemento: Optional[str] = None


@dataclass
class PrestadorDPS:
    cnpj: str
    razao_social: str
    inscricao_municipal: Optional[str] = None
    opcao_simples: OpcaoSimplesNacional = OpcaoSimplesNacional.OPTANTE_ME_EPP
    regime_especial: Optional[str] = None


@dataclass
class TomadorDPS:
    cnpj_cpf: str
    razao_social_nome: str
    endereco: Optional[EnderecoNacional] = None
    email: Optional[str] = None
    telefone: Optional[str] = None
    inscricao_municipal: Optional[str] = None


@dataclass
class ServicoDPS:
    codigo_tributacao_nacional: str  # NBS ou Código de Tributação Nacional da NFS-e (ex: '140101' ou '14.01')
    discriminacao: str
    municipio_prestacao_ibge: str
    exigibilidade_iss: ExigibilidadeISS = ExigibilidadeISS.EXIGIVEL
    aliquota_iss: Decimal = Decimal('5.00')
    tipo_retencao_iss: TipoRetencaoISS = TipoRetencaoISS.NAO_RETIDO
    codigo_municipio_incidencia: Optional[str] = None


@dataclass
class ValoresDPS:
    valor_servicos: Decimal
    valor_deducoes: Decimal = Decimal('0.00')
    valor_desconto_incondicionado: Decimal = Decimal('0.00')
    valor_desconto_condicionado: Decimal = Decimal('0.00')
    # Retenções federais opcionais
    retencao_pis: Decimal = Decimal('0.00')
    retencao_cofins: Decimal = Decimal('0.00')
    retencao_inss: Decimal = Decimal('0.00')
    retencao_ir: Decimal = Decimal('0.00')
    retencao_csll: Decimal = Decimal('0.00')

    @property
    def base_calculo_iss(self) -> Decimal:
        return max(Decimal('0.00'), self.valor_servicos - self.valor_deducoes - self.valor_desconto_incondicionado)

    def calcular_iss(self, aliquota: Decimal) -> Decimal:
        return (self.base_calculo_iss * (aliquota / Decimal('100.00'))).quantize(Decimal('0.01'))


@dataclass
class DadosDPS:
    serie: str
    numero_dps: int
    data_competencia: str  # YYYY-MM-DD
    data_hora_emissao: str  # ISO 8601
    ambiente: TipoAmbiente
    prestador: PrestadorDPS
    tomador: TomadorDPS
    servico: ServicoDPS
    valores: ValoresDPS
    versao_aplicacao: str = 'VERTYX_1.0'
