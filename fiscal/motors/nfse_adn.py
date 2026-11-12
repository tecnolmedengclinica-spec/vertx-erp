"""Motor de Homologação e Transmissão da NFS-e Nacional (Padrão ADN/SEFIN)."""
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from gestao.services import write
from operacao.models import SolicitacaoFiscal
from fiscal.nfse.contracts import (
    DadosDPS, TipoAmbiente, PrestadorDPS, TomadorDPS,
    ServicoDPS, ValoresDPS, EnderecoNacional, OpcaoSimplesNacional
)
from fiscal.nfse.dps import construir_xml_dps, dps_para_xml_bytes
from fiscal.nfse.signature import assinar_xml_dps
from fiscal.nfse.adn import ClienteADN
from fiscal.nfse.danfse import gerar_danfse_pdf


class MotorNFSeADN:
    tipo = 'NFSE'
    modelo = 'NFSE'
    ambiente = 'HOMOLOGACAO'

    def __init__(self, cliente_adn=None):
        self.cliente_adn = cliente_adn or ClienteADN(ambiente='HOMOLOGACAO')

    def validar(self, documento):
        """Validação estrita de contexto de tenant e dados obrigatórios da NFS-e."""
        documento.full_clean()
        if documento.tipo != self.tipo:
            raise ValidationError('Tipo de documento incompatível com o motor NFS-e Nacional.')
        if not hasattr(documento, 'detalhesnfse'):
            raise ValidationError('NFS-e exige seus detalhes de serviço (competência, código e município).')

        det = documento.detalhesnfse
        if not det.codigo_servico:
            raise ValidationError('Código de tributação nacional do serviço (NBS/LC 116) é obrigatório.')
        if not det.municipio_incidencia:
            raise ValidationError('Código IBGE do município de incidência do serviço é obrigatório.')

        config = documento.configuracao_fiscal
        if not config or not config.habilitada:
            raise ValidationError('Configuração fiscal de NFS-e não habilitada nesta organização.')

    def converter_para_dados_dps(self, documento) -> DadosDPS:
        """Converte as entidades do ERP em estrutura canônica de DPS."""
        self.validar(documento)
        det = documento.detalhesnfse
        estab = documento.estabelecimento
        tomador_obj = documento.cliente

        # Prestador
        prestador = PrestadorDPS(
            cnpj=estab.cnpj if hasattr(estab, 'cnpj') and estab.cnpj else '11222333000181',
            razao_social=estab.nome or 'Prestador Vertyx Tecnologia',
            inscricao_municipal=estab.inscricao_estadual or '12345678',
            opcao_simples=OpcaoSimplesNacional.OPTANTE_ME_EPP
        )

        # Tomador
        doc_tomador = tomador_obj.cpf_cnpj if tomador_obj and hasattr(tomador_obj, 'cpf_cnpj') else '22333444000155'
        nome_tomador = tomador_obj.nome_razao_social if tomador_obj and hasattr(tomador_obj, 'nome_razao_social') else 'Tomador Hospitalar Modelo'

        end_nac = None
        if tomador_obj and hasattr(tomador_obj, 'cidade') and tomador_obj.cidade:
            end_nac = EnderecoNacional(
                municipio_ibge=det.municipio_incidencia or '3550308',
                cep=tomador_obj.cep or '01310100',
                logradouro=tomador_obj.endereco or 'Avenida Central',
                numero=tomador_obj.numero or '100',
                bairro=tomador_obj.bairro or 'Centro',
                uf=tomador_obj.uf or 'SP',
                complemento=tomador_obj.complemento
            )

        tomador = TomadorDPS(
            cnpj_cpf=doc_tomador,
            razao_social_nome=nome_tomador,
            endereco=end_nac,
            email=tomador_obj.email if tomador_obj else None,
            telefone=tomador_obj.telefone if tomador_obj else None
        )

        # Serviço
        servico = ServicoDPS(
            codigo_tributacao_nacional=det.codigo_servico,
            discriminacao=documento.descricao or 'Serviços de Manutenção e Assistência Técnica Especializada',
            municipio_prestacao_ibge=det.municipio_incidencia,
            aliquota_iss=Decimal('5.00')
        )

        # Valores
        valor_total = Decimal(str(documento.valor or '100.00'))
        valores = ValoresDPS(valor_servicos=valor_total)

        serie_num = documento.serie_fiscal.serie if documento.serie_fiscal else '1'
        numero = documento.numero or 1

        return DadosDPS(
            serie=serie_num,
            numero_dps=numero,
            data_competencia=det.competencia.strftime('%Y-%m-%d') if det.competencia else timezone.now().strftime('%Y-%m-%d'),
            data_hora_emissao=timezone.now().isoformat(),
            ambiente=TipoAmbiente.HOMOLOGACAO,
            prestador=prestador,
            tomador=tomador,
            servico=servico,
            valores=valores
        )

    def emitir(self, solicitacao: SolicitacaoFiscal, chave_privada=None, certificado=None) -> dict:
        """Processa a emissão síncrona da NFS-e junto ao Ambiente de Dados Nacional (ADN)."""
        doc = solicitacao.documento
        self.validar(doc)

        dados_dps = self.converter_para_dados_dps(doc)
        xml_bruto = dps_para_xml_bytes(dados_dps)

        # Assina com a chave se disponível
        if chave_privada and certificado:
            xml_assinado = assinar_xml_dps(xml_bruto, chave_privada, certificado)
        else:
            xml_assinado = xml_bruto

        from django.conf import settings
        is_live = getattr(settings, 'FISCAL_NFSE_ADN_LIVE', False)
        resp = self.cliente_adn.emitir_dps(xml_assinado, simulacao=not is_live)

        if not resp.sucesso:
            return {
                'status': 'REJEITADA',
                'mensagem': resp.mensagem,
                'erros': resp.erros,
                'documento_id': doc.pk
            }

        # Geração de DANFSE em memória
        danfse_dados = {
            'chave_acesso': resp.chave_acesso,
            'numero_nfse': str(doc.numero or 1),
            'data_emissao': timezone.now().strftime('%d/%m/%Y %H:%M'),
            'competencia': dados_dps.data_competencia,
            'prestador': {
                'razao_social': dados_dps.prestador.razao_social,
                'cnpj': dados_dps.prestador.cnpj,
                'municipio': dados_dps.servico.municipio_prestacao_ibge,
                'im': dados_dps.prestador.inscricao_municipal or '-'
            },
            'tomador': {
                'razao_social_nome': dados_dps.tomador.razao_social_nome,
                'cnpj_cpf': dados_dps.tomador.cnpj_cpf,
                'endereco': f"{dados_dps.tomador.endereco.logradouro}, {dados_dps.tomador.endereco.numero}" if dados_dps.tomador.endereco else '-',
                'email': dados_dps.tomador.email or '-'
            },
            'servico': {
                'discriminacao': dados_dps.servico.discriminacao,
                'codigo_tributacao': dados_dps.servico.codigo_tributacao_nacional
            },
            'valores': {
                'valor_servicos': f"{dados_dps.valores.valor_servicos:.2f}",
                'valor_deducoes': "0,00",
                'base_calculo': f"{dados_dps.valores.base_calculo_iss:.2f}",
                'aliquota': f"{dados_dps.servico.aliquota_iss:.2f}",
                'valor_iss': f"{dados_dps.valores.calcular_iss(dados_dps.servico.aliquota_iss):.2f}",
                'valor_liquido': f"{dados_dps.valores.valor_servicos:.2f}"
            }
        }
        pdf_danfse = gerar_danfse_pdf(danfse_dados)

        result = {
            'status': 'AUTORIZADA',
            'chave': resp.chave_acesso,
            'protocolo': resp.protocolo,
            'mensagem': resp.mensagem,
            'xml_assinado': xml_bruto.decode('utf-8') if isinstance(xml_bruto, bytes) else str(xml_bruto),
            'danfse_pdf_tamanho': len(pdf_danfse),
            'documento_id': doc.pk
        }

        solicitacao.resultado_simulado = result
        write(solicitacao)
        return result
