"""Gerador do XML oficial da DPS (Declaração de Prestação de Serviços) da NFS-e Nacional."""
import re
from decimal import Decimal
from lxml import etree
from .contracts import DadosDPS, TipoAmbiente

NAMESPACE_NFSE = "http://www.sped.fazenda.gov.br/nfse"


def formatar_decimal(valor: Decimal) -> str:
    """Formata valor decimal com duas casas decimais."""
    return f"{valor:.2f}"


def gerar_chave_dps(dados: DadosDPS) -> str:
    """Gera o identificador único da DPS no padrão nacional (42 dígitos)."""
    cnpj_limpo = re.sub(r'\D', '', dados.prestador.cnpj)
    serie_formatada = f"{dados.serie[:5]:0>5}"
    n_dps_formatado = f"{dados.numero_dps:0>14}"
    tipo_insc = '1' if len(cnpj_limpo) == 14 else '2'
    chave = (
        f"{dados.servico.municipio_prestacao_ibge:0>7}"
        f"{dados.ambiente.value}"
        f"{tipo_insc}"
        f"{cnpj_limpo:0>14}"
        f"{serie_formatada}"
        f"{n_dps_formatado}"
    )
    return chave


def construir_xml_dps(dados: DadosDPS) -> etree._Element:
    """Monta a árvore de elementos XML da DPS em conformidade com o schema v1.00 do ADN."""
    chave_dps = gerar_chave_dps(dados)
    id_dps = f"DPS{chave_dps}"

    nsmap = {None: NAMESPACE_NFSE}
    root = etree.Element("DPS", versao="1.00", nsmap=nsmap)
    inf_dps = etree.SubElement(root, "infDPS", Id=id_dps)

    # Identificação da DPS
    etree.SubElement(inf_dps, "tpAmb").text = dados.ambiente.value
    etree.SubElement(inf_dps, "dhEmi").text = dados.data_hora_emissao
    etree.SubElement(inf_dps, "verAplic").text = dados.versao_aplicacao
    etree.SubElement(inf_dps, "serie").text = str(dados.serie)
    etree.SubElement(inf_dps, "nDPS").text = str(dados.numero_dps)
    etree.SubElement(inf_dps, "dCompet").text = dados.data_competencia
    etree.SubElement(inf_dps, "tpEmit").text = "1"  # Prestador
    etree.SubElement(inf_dps, "cLocEmi").text = dados.servico.municipio_prestacao_ibge

    # Prestador do Serviço
    prest = etree.SubElement(inf_dps, "prest")
    cnpj_prest = re.sub(r'\D', '', dados.prestador.cnpj)
    etree.SubElement(prest, "CNPJ").text = cnpj_prest
    if dados.prestador.inscricao_municipal:
        etree.SubElement(prest, "IM").text = dados.prestador.inscricao_municipal
    etree.SubElement(prest, "xNome").text = dados.prestador.razao_social[:150]
    reg_trib = etree.SubElement(prest, "regTrib")
    etree.SubElement(reg_trib, "opSimpNac").text = dados.prestador.opcao_simples.value

    # Tomador do Serviço
    toma = etree.SubElement(inf_dps, "toma")
    doc_toma = re.sub(r'\D', '', dados.tomador.cnpj_cpf)
    if len(doc_toma) == 14:
        etree.SubElement(toma, "CNPJ").text = doc_toma
    else:
        etree.SubElement(toma, "CPF").text = doc_toma
    etree.SubElement(toma, "xNome").text = dados.tomador.razao_social_nome[:150]

    if dados.tomador.endereco:
        end = etree.SubElement(toma, "end")
        end_nac = etree.SubElement(end, "endNac")
        etree.SubElement(end_nac, "cMun").text = dados.tomador.endereco.municipio_ibge
        etree.SubElement(end_nac, "CEP").text = re.sub(r'\D', '', dados.tomador.endereco.cep)
        etree.SubElement(end, "xLgr").text = dados.tomador.endereco.logradouro[:125]
        etree.SubElement(end, "nro").text = dados.tomador.endereco.numero[:10]
        if dados.tomador.endereco.complemento:
            etree.SubElement(end, "xCpl").text = dados.tomador.endereco.complemento[:60]
        etree.SubElement(end, "xBairro").text = dados.tomador.endereco.bairro[:60]
        etree.SubElement(end, "UF").text = dados.tomador.endereco.uf

    if dados.tomador.telefone:
        etree.SubElement(toma, "fone").text = re.sub(r'\D', '', dados.tomador.telefone)[:11]
    if dados.tomador.email:
        etree.SubElement(toma, "email").text = dados.tomador.email[:80]

    # Detalhes do Serviço
    serv = etree.SubElement(inf_dps, "serv")
    loc_prest = etree.SubElement(serv, "locPrest")
    etree.SubElement(loc_prest, "cLocPrestacao").text = dados.servico.municipio_prestacao_ibge

    c_serv = etree.SubElement(serv, "cServ")
    codigo_trib = re.sub(r'\D', '', dados.servico.codigo_tributacao_nacional)
    etree.SubElement(c_serv, "cTribNac").text = codigo_trib
    etree.SubElement(c_serv, "xDescServ").text = dados.servico.discriminacao[:2000]

    # Valores e Tributos
    valores = etree.SubElement(inf_dps, "valores")
    v_serv_prest = etree.SubElement(valores, "vServPrest")
    etree.SubElement(v_serv_prest, "vServ").text = formatar_decimal(dados.valores.valor_servicos)

    if dados.valores.valor_desconto_incondicionado > 0:
        v_desc = etree.SubElement(valores, "vDescCondIncond")
        etree.SubElement(v_desc, "vDescIncond").text = formatar_decimal(dados.valores.valor_desconto_incondicionado)

    trib = etree.SubElement(valores, "trib")
    trib_mun = etree.SubElement(trib, "tribMun")
    etree.SubElement(trib_mun, "tribISSQN").text = dados.servico.exigibilidade_iss.value
    etree.SubElement(trib_mun, "vBC").text = formatar_decimal(dados.valores.base_calculo_iss)
    etree.SubElement(trib_mun, "pAliq").text = formatar_decimal(dados.servico.aliquota_iss)
    iss_calculado = dados.valores.calcular_iss(dados.servico.aliquota_iss)
    etree.SubElement(trib_mun, "vISSQN").text = formatar_decimal(iss_calculado)
    etree.SubElement(trib_mun, "tpRetISSQN").text = dados.servico.tipo_retencao_iss.value

    return root


def dps_para_xml_bytes(dados: DadosDPS) -> bytes:
    """Gera os bytes XML brutos prontos para assinatura e validação."""
    element = construir_xml_dps(dados)
    return etree.tostring(element, encoding="utf-8", xml_declaration=True, pretty_print=True)
