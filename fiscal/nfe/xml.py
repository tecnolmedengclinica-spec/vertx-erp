"""Construtor oficial NF-e modelo 55 v4.00 com suporte opt-in à Reforma Tributária (CBS/IBS/IS) e eventos."""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from lxml import etree
from django.core.exceptions import ValidationError

from fiscal.nfce.schema import NFE
from fiscal.nfce.xml import CENT, decimal, digito, money
from fiscal.tributaria import (
    ParametrosReformaTributaria,
    anexar_ibscbs_item_xml,
    anexar_ibscbs_total_xml,
    gerar_nota_json_rtc,
)

NS = '{' + NFE + '}'

UF_CODES = {
    'RO': '11', 'AC': '12', 'AM': '13', 'RR': '14', 'PA': '15', 'AP': '16', 'TO': '17',
    'MA': '21', 'PI': '22', 'CE': '23', 'RN': '24', 'PB': '25', 'PE': '26', 'AL': '27',
    'SE': '28', 'BA': '29', 'MG': '31', 'ES': '32', 'RJ': '33', 'SP': '35', 'PR': '41',
    'SC': '42', 'RS': '43', 'MS': '50', 'MT': '51', 'GO': '52', 'DF': '53',
}

MODOS_EMISSAO_TPEMIS = {
    'NORMAL': '1',
    'FS_IA': '2',
    'FS_DA': '5',
    'SVC_AN': '6',
    'SVC_RS': '7',
    'EPEC': '4',
}


def _tag(parent: etree._Element, name: str, value: Any) -> etree._Element:
    node = etree.SubElement(parent, NS + name)
    node.text = str(value)
    return node


def _only_digits(val: Any) -> str:
    return re.sub(r'\D', '', str(val or ''))


def chave_nfe(
    uf: str,
    cnpj: str,
    serie: int | str,
    numero: int | str,
    emissao: datetime,
    cnf: str,
    *,
    tp_emis: str = '1',
) -> str:
    """Constrói chave NF-e (modelo 55, 44 dígitos + DV módulo 11)."""
    cuf = UF_CODES.get(str(uf).upper(), str(uf))
    cnpj_num = _only_digits(cnpj)
    if not re.fullmatch(r'\d{2}', cuf):
        raise ValidationError('UF/cUF inválida para chave NF-e.')
    if not re.fullmatch(r'\d{14}', cnpj_num):
        raise ValidationError('CNPJ do emitente com 14 dígitos é obrigatório para NF-e.')
    if not str(serie).isdigit() or not 0 <= int(serie) <= 999:
        raise ValidationError('Série NF-e deve estar entre 0 e 999.')
    if not str(numero).isdigit() or not 1 <= int(numero) <= 999999999:
        raise ValidationError('Número NF-e deve estar entre 1 e 999999999.')
    if not re.fullmatch(r'\d{8}', str(cnf)):
        raise ValidationError('cNF deve conter exatamente 8 dígitos numéricos.')
    if str(tp_emis) not in ('1', '2', '3', '4', '5', '6', '7'):
        raise ValidationError('tpEmis inválido para modelo 55.')
    base = f'{cuf}{emissao:%y%m}{cnpj_num}55{int(serie):03}{int(numero):09}{tp_emis}{cnf}'
    return base + digito(base)


def _resolver_rtc_nota(
    snapshot: dict[str, Any],
    items: list[dict[str, Any]],
    key: str,
    numero: int,
    emissao: datetime,
) -> dict[str, Any] | None:
    rtc_cfg = snapshot.get('reforma_tributaria') or snapshot.get('rtc')
    has_item_rtc = any(item.get('rtc') or item.get('reforma_tributaria') for item in items)
    if not rtc_cfg and not has_item_rtc:
        return None
    if isinstance(rtc_cfg, dict) and rtc_cfg.get('habilitada') is False and not has_item_rtc:
        return None
    if isinstance(rtc_cfg, dict):
        if 'regime_transicao' in rtc_cfg and not any(k in rtc_cfg for k in ('aliquota_cbs', 'aliquota_ibs_uf', 'aliquota_ibs_mun')):
            default_params = ParametrosReformaTributaria.from_preset(rtc_cfg['regime_transicao'])
        else:
            default_params = ParametrosReformaTributaria.from_dict(rtc_cfg)
    else:
        default_params = ParametrosReformaTributaria.from_preset('2026_TESTE')
    return gerar_nota_json_rtc(
        items,
        numero=numero,
        chave=key,
        modelo='55',
        serie=str(snapshot.get('serie', '1')),
        data_emissao=emissao.isoformat(timespec='seconds'),
        default_params=default_params,
    )


def gerar_nfe(
    snapshot: dict[str, Any],
    numero: int,
    emissao: datetime,
    cnf: str,
    *,
    modo_emissao: str = 'NORMAL',
    motivo_contingencia: str = '',
    contingencia_em: datetime | None = None,
) -> tuple[bytes, str, dict[str, Any] | None]:
    """Gera XML NF-e v4.00 (modelo 55), retorna `(xml_bytes, chave, nota_json)`."""
    emit = snapshot['emitente']
    dest = snapshot.get('destinatario') or {}
    items = snapshot['itens']
    if not items:
        raise ValidationError('NF-e exige ao menos 1 item.')
    tp_emis = MODOS_EMISSAO_TPEMIS.get(modo_emissao, '1')
    ambiente_cod = '2' if snapshot.get('ambiente', 'HOMOLOGACAO') in ('HOMOLOGACAO', 'SIMULACAO') else '1'
    cuf = UF_CODES.get(emit['uf'].upper(), '35')
    key = chave_nfe(cuf, emit['cnpj'], snapshot['serie'], numero, emissao, cnf, tp_emis=tp_emis)

    dest_uf = (dest.get('uf') or emit['uf']).upper()
    id_dest = '1' if dest_uf == emit['uf'].upper() else '2'

    root = etree.Element(NS + 'NFe', nsmap={None: NFE})
    inf = etree.SubElement(root, NS + 'infNFe', Id='NFe' + key, versao='4.00')
    ide = etree.SubElement(inf, NS + 'ide')

    ide_fields: list[tuple[str, Any]] = [
        ('cUF', cuf),
        ('cNF', cnf),
        ('natOp', (snapshot.get('natureza') or 'VENDA DE MERCADORIA')[:60]),
        ('mod', '55'),
        ('serie', int(snapshot.get('serie', 1))),
        ('nNF', int(numero)),
        ('dhEmi', emissao.isoformat(timespec='seconds')),
        ('tpNF', str(snapshot.get('tp_nf', '1'))),
        ('idDest', id_dest),
        ('cMunFG', _only_digits(emit.get('municipio_ibge') or '3550308').zfill(7)[:7]),
        ('tpImp', '1'),
        ('tpEmis', tp_emis),
        ('cDV', key[-1]),
        ('tpAmb', ambiente_cod),
        ('finNFe', str(snapshot.get('finalidade', '1'))),
        ('indFinal', str(snapshot.get('ind_final', '1'))),
        ('indPres', str(snapshot.get('ind_pres', '1'))),
        ('procEmi', '0'),
        ('verProc', 'VERTYX-NFE-4.00'),
    ]
    if tp_emis != '1':
        dh_cont = (contingencia_em or emissao).isoformat(timespec='seconds')
        x_just = (motivo_contingencia or 'Contingência operacional por indisponibilidade SEFAZ').strip()
        if len(x_just) < 15:
            x_just = f'{x_just} - Contingência SEFAZ'.ljust(15, '.')
        ide_fields.extend([('dhCont', dh_cont), ('xJust', x_just[:256])])
    for k, v in ide_fields:
        _tag(ide, k, v)

    issuer = etree.SubElement(inf, NS + 'emit')
    _tag(issuer, 'CNPJ', _only_digits(emit['cnpj']).zfill(14)[:14])
    _tag(issuer, 'xNome', (emit['razao_social'] or 'EMITENTE')[:60])
    ender_emit = etree.SubElement(issuer, NS + 'enderEmit')
    raw_addr = emit.get('endereco')
    emit_addr = raw_addr if isinstance(raw_addr, dict) else {'logradouro': str(raw_addr or emit.get('logradouro') or 'Rua não informada')}
    _tag(ender_emit, 'xLgr', (emit_addr.get('logradouro') or 'Rua não informada')[:60])
    _tag(ender_emit, 'nro', (emit_addr.get('numero') or 'S/N')[:60])
    if emit_addr.get('complemento'):
        _tag(ender_emit, 'xCpl', emit_addr['complemento'][:60])
    _tag(ender_emit, 'xBairro', (emit_addr.get('bairro') or 'Centro')[:60])
    _tag(ender_emit, 'cMun', _only_digits(emit.get('municipio_ibge') or '3550308').zfill(7)[:7])
    _tag(ender_emit, 'xMun', (emit.get('municipio_nome') or 'Município')[:60])
    _tag(ender_emit, 'UF', emit['uf'].upper()[:2])
    _tag(ender_emit, 'CEP', _only_digits(emit_addr.get('cep') or '01001000').zfill(8)[:8])
    _tag(ender_emit, 'cPais', '1058')
    _tag(ender_emit, 'xPais', 'BRASIL')
    _tag(issuer, 'IE', _only_digits(emit.get('ie')) or 'ISENTO')
    _tag(issuer, 'CRT', str(emit.get('crt') or '1'))

    if dest and _only_digits(dest.get('documento')):
        dest_el = etree.SubElement(inf, NS + 'dest')
        doc_dest = _only_digits(dest['documento'])
        _tag(dest_el, 'CPF' if len(doc_dest) <= 11 else 'CNPJ', doc_dest.zfill(11 if len(doc_dest) <= 11 else 14))
        _tag(dest_el, 'xNome', ('NF-E EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL' if ambiente_cod == '2' else (dest.get('nome') or 'CONSUMIDOR'))[:60])
        if dest.get('logradouro'):
            ed = etree.SubElement(dest_el, NS + 'enderDest')
            _tag(ed, 'xLgr', dest['logradouro'][:60])
            _tag(ed, 'nro', (dest.get('numero') or 'S/N')[:60])
            _tag(ed, 'xBairro', (dest.get('bairro') or 'Centro')[:60])
            _tag(ed, 'cMun', _only_digits(dest.get('municipio_ibge') or emit.get('municipio_ibge') or '3550308').zfill(7)[:7])
            _tag(ed, 'xMun', (dest.get('municipio_nome') or 'Cidade')[:60])
            _tag(ed, 'UF', dest_uf[:2])
            _tag(ed, 'CEP', _only_digits(dest.get('cep') or '01001000').zfill(8)[:8])
            _tag(ed, 'cPais', '1058')
            _tag(ed, 'xPais', 'BRASIL')
        _tag(dest_el, 'indIEDest', '1' if _only_digits(dest.get('ie')) else '9')
        if _only_digits(dest.get('ie')):
            _tag(dest_el, 'IE', _only_digits(dest['ie']))

    nota_json = _resolver_rtc_nota(snapshot, items, key, int(numero), emissao)
    rtc_itens = {item['item']: item for item in (nota_json['itens_rtc'] if nota_json else [])}

    sums = {name: Decimal('0.00') for name in ('vProd', 'vDesc', 'vOutro', 'vBC', 'vICMS', 'vPIS', 'vCOFINS')}
    for index, row in enumerate(items, start=1):
        det = etree.SubElement(inf, NS + 'det', nItem=str(index))
        prod = etree.SubElement(det, NS + 'prod')
        qty = decimal(row.get('quantidade', 1), 3)
        unit = decimal(row.get('valor_unitario', row.get('liquido', 0)), 4)
        gross = (qty * unit).quantize(CENT, rounding=ROUND_HALF_UP)
        net = decimal(row.get('liquido', gross), 2)
        discount = max(gross - net, Decimal('0.00'))
        addition = max(net - gross, Decimal('0.00'))

        gtin = _only_digits(row.get('gtin')) or 'SEM GTIN'
        x_prod = 'NOTA FISCAL EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL' if (ambiente_cod == '2' and index == 1) else str(row.get('descricao') or 'PRODUTO')
        _tag(prod, 'cProd', str(row.get('sku') or f'ITEM-{index}')[:60])
        _tag(prod, 'cEAN', gtin)
        _tag(prod, 'xProd', x_prod[:120])
        _tag(prod, 'NCM', _only_digits(row.get('ncm') or '90189099').zfill(8)[:8])
        if _only_digits(row.get('cest')):
            _tag(prod, 'CEST', _only_digits(row['cest'])[:7])
        _tag(prod, 'CFOP', _only_digits(row.get('cfop') or '5102')[:4])
        u_com = str(row.get('unidade') or 'UN')[:6]
        _tag(prod, 'uCom', u_com)
        _tag(prod, 'qCom', format(qty, '.4f'))
        _tag(prod, 'vUnCom', format(unit, '.4f'))
        _tag(prod, 'vProd', money(gross))
        _tag(prod, 'cEANTrib', gtin)
        _tag(prod, 'uTrib', u_com)
        _tag(prod, 'qTrib', format(qty, '.4f'))
        _tag(prod, 'vUnTrib', format(unit, '.4f'))
        if discount > 0:
            _tag(prod, 'vDesc', money(discount))
        if addition > 0:
            _tag(prod, 'vOutro', money(addition))
        _tag(prod, 'indTot', '1')

        sums['vProd'] += gross
        sums['vDesc'] += discount
        sums['vOutro'] += addition

        tax = row.get('tributacao') or {}
        imposto = etree.SubElement(det, NS + 'imposto')
        icms = etree.SubElement(imposto, NS + 'ICMS')
        icms_code = str(tax.get('icms_codigo') or ('102' if str(emit.get('crt') or '1') in ('1', '4') else '00'))
        orig = str(row.get('origem') or '0')
        if icms_code == '102':
            grp = etree.SubElement(icms, NS + 'ICMSSN102')
            _tag(grp, 'orig', orig)
            _tag(grp, 'CSOSN', '102')
        else:
            grp = etree.SubElement(icms, NS + 'ICMS00')
            _tag(grp, 'orig', orig)
            _tag(grp, 'CST', icms_code[:2])
            _tag(grp, 'modBC', '3')
            p_icms = decimal(tax.get('icms_aliquota', '0.00'), 4)
            v_icms = (net * p_icms / Decimal('100')).quantize(CENT, rounding=ROUND_HALF_UP)
            _tag(grp, 'vBC', money(net))
            _tag(grp, 'pICMS', format(p_icms, '.2f'))
            _tag(grp, 'vICMS', money(v_icms))
            sums['vBC'] += net
            sums['vICMS'] += v_icms

        for kind, node_name in (('pis', 'PIS'), ('cofins', 'COFINS')):
            cst_pc = str(tax.get(f'{kind}_codigo') or '07')
            pc_parent = etree.SubElement(imposto, NS + node_name)
            if cst_pc in ('01', '02'):
                pc_grp = etree.SubElement(pc_parent, NS + node_name + 'Aliq')
                _tag(pc_grp, 'CST', cst_pc)
                rate = decimal(tax.get(f'{kind}_aliquota', '0.00'), 4)
                val_pc = (net * rate / Decimal('100')).quantize(CENT, rounding=ROUND_HALF_UP)
                _tag(pc_grp, 'vBC', money(net))
                _tag(pc_grp, f'p{node_name}', format(rate, '.2f'))
                _tag(pc_grp, f'v{node_name}', money(val_pc))
                sums[f'v{node_name}'] += val_pc
            else:
                pc_grp = etree.SubElement(pc_parent, NS + node_name + 'NT')
                _tag(pc_grp, 'CST', cst_pc)

        if str(index) in rtc_itens:
            anexar_ibscbs_item_xml(imposto, NS, rtc_itens[str(index)])

    total_nf = decimal(snapshot.get('total', sums['vProd'] - sums['vDesc'] + sums['vOutro']), 2)
    totals = etree.SubElement(inf, NS + 'total')
    icmstot = etree.SubElement(totals, NS + 'ICMSTot')
    for name in ['vBC', 'vICMS', 'vICMSDeson', 'vFCP', 'vBCST', 'vST', 'vFCPST', 'vFCPSTRet', 'vProd', 'vFrete', 'vSeg', 'vDesc', 'vII', 'vIPI', 'vIPIDevol', 'vPIS', 'vCOFINS', 'vOutro', 'vNF']:
        _tag(icmstot, name, money(total_nf if name == 'vNF' else sums.get(name, Decimal('0.00'))))
    if nota_json:
        anexar_ibscbs_total_xml(totals, NS, nota_json)

    transp = etree.SubElement(inf, NS + 'transp')
    _tag(transp, 'modFrete', str(snapshot.get('mod_frete', '9')))

    pag = etree.SubElement(inf, NS + 'pag')
    payments = snapshot.get('pagamentos') or [{'codigo': '15', 'valor': str(total_nf)}]
    for p in payments:
        det_pag = etree.SubElement(pag, NS + 'detPag')
        _tag(det_pag, 'tPag', str(p.get('codigo') or '15'))
        _tag(det_pag, 'vPag', money(decimal(p.get('valor', total_nf), 2)))

    inf_adic = etree.SubElement(inf, NS + 'infAdic')
    cpl_parts = []
    if ambiente_cod == '2':
        cpl_parts.append('Documento emitido em homologacao - sem valor fiscal.')
    if nota_json:
        cpl_parts.append(
            f"RTC: vBC={nota_json['vBCIBSCBS']} | CBS={nota_json['cbs']['vCBS']} | IBS={nota_json['ibs']['vIBS']}"
        )
    if snapshot.get('observacoes'):
        cpl_parts.append(str(snapshot['observacoes'])[:400])
    _tag(inf_adic, 'infCpl', ' '.join(cpl_parts) or 'VERTYX ERP')

    xml_bytes = etree.tostring(root, encoding='utf-8', xml_declaration=True, pretty_print=True)
    return xml_bytes, key, nota_json


def construir_evento_nfe(
    *,
    chave: str,
    cnpj: str,
    tp_evento: str,
    n_seq: int = 1,
    ambiente: str = 'HOMOLOGACAO',
    protocolo: str = '',
    justificativa: str = '',
    correcao: str = '',
    dh_evento: datetime | None = None,
) -> bytes:
    """Constrói XML de evento NF-e (`110111` Cancelamento ou `110110` CC-e)."""
    if len(_only_digits(chave)) != 44:
        raise ValidationError('Chave de acesso deve possuir 44 dígitos.')
    cnpj_num = _only_digits(cnpj).zfill(14)[:14]
    c_orgao = chave[:2]
    tp_amb = '2' if ambiente in ('HOMOLOGACAO', 'SIMULACAO') else '1'
    dt_str = (dh_evento or datetime.now().astimezone()).isoformat(timespec='seconds')

    root = etree.Element(NS + 'envEvento', versao='1.00', nsmap={None: NFE})
    _tag(root, 'idLote', '1')
    evento = etree.SubElement(root, NS + 'evento', versao='1.00')
    info = etree.SubElement(evento, NS + 'infEvento', Id=f'ID{tp_evento}{chave}{int(n_seq):02d}')
    _tag(info, 'cOrgao', c_orgao)
    _tag(info, 'tpAmb', tp_amb)
    _tag(info, 'CNPJ', cnpj_num)
    _tag(info, 'chNFe', chave)
    _tag(info, 'dhEvento', dt_str)
    _tag(info, 'tpEvento', tp_evento)
    _tag(info, 'nSeqEvento', str(int(n_seq)))
    _tag(info, 'verEvento', '1.00')
    det = etree.SubElement(info, NS + 'detEvento', versao='1.00')
    if tp_evento == '110111':
        if len(justificativa.strip()) < 15:
            raise ValidationError('Justificativa de cancelamento exige ao menos 15 caracteres.')
        _tag(det, 'descEvento', 'Cancelamento')
        _tag(det, 'nProt', protocolo)
        _tag(det, 'xJust', justificativa.strip()[:255])
    elif tp_evento == '110110':
        if len(correcao.strip()) < 15:
            raise ValidationError('Texto da Carta de Correção (CC-e) exige ao menos 15 caracteres.')
        _tag(det, 'descEvento', 'Carta de Correcao')
        _tag(det, 'xCorrecao', correcao.strip()[:1000])
        _tag(
            det,
            'xCondUso',
            'A Carta de Correcao e disciplinada pelo paragrafo 1o-A do art. 7o do Convenio S/N, de 15 de dezembro de 1970 e pode ser utilizada para regularizacao de erro ocorrido na emissao de documento fiscal, desde que o erro nao esteja relacionado com: I - as variaveis que determinam o valor do imposto tais como: base de calculo, aliquota, diferenca de preco, quantidade, valor da operacao ou da prestacao; II - a correcao de dados cadastrais que implique mudanca do remetente ou do destinatario; III - a data de emissao ou de saida.',
        )
    else:
        raise ValidationError(f'Tipo de evento NF-e não suportado: {tp_evento}.')
    return etree.tostring(root, encoding='utf-8', xml_declaration=True, pretty_print=True)
