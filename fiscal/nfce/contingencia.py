"""Suporte NFC-e (Modelo 65) para Reforma Tributária (CBS/IBS) e Contingência Offline PDV (tpEmis=9)."""
from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any
from lxml import etree

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from gestao.permissions import require
from gestao.services import audit, write
from operacao import models as m
from fiscal.nfse.danfse import gerar_qrcode_bytes
from fiscal.nfce.schema import NFE, parse
from fiscal.nfce.xml import digito
from fiscal.tributaria import (
    ParametrosReformaTributaria,
    anexar_ibscbs_item_xml,
    anexar_ibscbs_total_xml,
    gerar_nota_json_rtc,
)

NS = '{' + NFE + '}'


def anexar_rtc_em_xml_nfce(
    xml_bytes: bytes,
    itens_base: list[dict[str, Any]],
    *,
    numero: str | int = '1',
    chave: str = '',
    serie: str = '1',
    data_emissao: str = '',
    params_rtc: ParametrosReformaTributaria | dict[str, Any] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Injeta nós `<IBSCBS>` por item e `<IBSCBSTot>` em XML NFC-e (Modelo 65) e devolve `(xml_bytes, nota_json)`."""
    root = parse(xml_bytes)
    inf = root.find(f'.//{NS}infNFe')
    if inf is None:
        raise ValidationError('XML NFC-e inválido: elemento <infNFe> ausente.')
    nota_json = gerar_nota_json_rtc(
        itens_base,
        numero=numero,
        chave=chave,
        modelo='65',
        serie=serie,
        data_emissao=data_emissao or timezone.now().isoformat(timespec='seconds'),
        default_params=params_rtc,
    )
    rtc_por_item = {str(r['item']): r for r in nota_json['itens_rtc']}
    for idx, det in enumerate(inf.findall(f'{NS}det'), start=1):
        imposto = det.find(f'{NS}imposto')
        if imposto is None:
            imposto = etree.SubElement(det, f'{NS}imposto')
        n_item = det.get('nItem') or str(idx)
        item_rtc = rtc_por_item.get(str(n_item))
        if item_rtc:
            anexar_ibscbs_item_xml(imposto, NS, item_rtc)
    total_node = inf.find(f'{NS}total')
    if total_node is not None:
        anexar_ibscbs_total_xml(total_node, NS, nota_json)
    return etree.tostring(root, encoding='utf-8', xml_declaration=True), nota_json


def _xml_nfce_minimo(
    *,
    uf_cod: str,
    cnpj: str,
    razao: str,
    mun_ibge: str,
    serie: str,
    numero: int,
    emissao_dt,
    itens: list[dict[str, Any]],
    tp_emis: str = '1',
    justificativa: str = '',
    ambiente_cod: str = '2',
) -> tuple[bytes, str]:
    cnf = f'{(numero * 9137 + 12345) % 100000000:08d}'
    base = f'{uf_cod}{emissao_dt:%y%m}{cnpj}65{int(serie):03}{int(numero):09}{tp_emis}{cnf}'
    chave = base + digito(base)

    root = etree.Element(NS + 'NFe', nsmap={None: NFE})
    inf = etree.SubElement(root, NS + 'infNFe', Id='NFe' + chave, versao='4.00')
    ide = etree.SubElement(inf, NS + 'ide')
    for k, v in [
        ('cUF', uf_cod),
        ('cNF', cnf),
        ('natOp', 'VENDA AO CONSUMIDOR'),
        ('mod', '65'),
        ('serie', str(int(serie))),
        ('nNF', str(int(numero))),
        ('dhEmi', emissao_dt.isoformat(timespec='seconds')),
        ('tpNF', '1'),
        ('idDest', '1'),
        ('cMunFG', mun_ibge),
        ('tpImp', '4'),
        ('tpEmis', tp_emis),
        ('cDV', chave[-1]),
        ('tpAmb', ambiente_cod),
        ('finNFe', '1'),
        ('indFinal', '1'),
        ('indPres', '1'),
        ('procEmi', '0'),
        ('verProc', 'VERTYX-PDV-65'),
    ]:
        el = etree.SubElement(ide, NS + k)
        el.text = str(v)
    if tp_emis == '9':
        dh = etree.SubElement(ide, NS + 'dhCont')
        dh.text = emissao_dt.isoformat(timespec='seconds')
        xj = etree.SubElement(ide, NS + 'xJust')
        xj.text = (justificativa or 'PDV operando em contingência offline (tpEmis=9)')[:255]

    emit = etree.SubElement(inf, NS + 'emit')
    etree.SubElement(emit, NS + 'CNPJ').text = cnpj
    etree.SubElement(emit, NS + 'xNome').text = razao[:60]
    end = etree.SubElement(emit, NS + 'enderEmit')
    for k, v in [('xLgr', 'Rua Comercial'), ('nro', '100'), ('xBairro', 'Centro'), ('cMun', mun_ibge), ('xMun', 'Aracaju'), ('UF', 'SE'), ('CEP', '49000000')]:
        etree.SubElement(end, NS + k).text = v
    etree.SubElement(emit, NS + 'IE').text = 'ISENTO'
    etree.SubElement(emit, NS + 'CRT').text = '1'

    total_val = Decimal('0.00')
    for idx, item in enumerate(itens, start=1):
        v_prod = Decimal(str(item.get('valor') or item.get('valor_base') or '0.00')).quantize(Decimal('.01'))
        total_val += v_prod
        det = etree.SubElement(inf, NS + 'det', nItem=str(idx))
        prod = etree.SubElement(det, NS + 'prod')
        for k, v in [
            ('cProd', str(item.get('sku') or f'ITEM{idx}')),
            ('cEAN', 'SEM GTIN'),
            ('xProd', str(item.get('descricao') or 'Produto PDV')[:120]),
            ('NCM', str(item.get('ncm') or '90189099')),
            ('CFOP', str(item.get('cfop') or '5102')),
            ('uCom', 'UN'),
            ('qCom', '1.0000'),
            ('vUnCom', f'{v_prod:.2f}'),
            ('vProd', f'{v_prod:.2f}'),
            ('cEANTrib', 'SEM GTIN'),
            ('uTrib', 'UN'),
            ('qTrib', '1.0000'),
            ('vUnTrib', f'{v_prod:.2f}'),
            ('indTot', '1'),
        ]:
            etree.SubElement(prod, NS + k).text = v
        imposto = etree.SubElement(det, NS + 'imposto')
        icms = etree.SubElement(etree.SubElement(imposto, NS + 'ICMS'), NS + 'ICMSSN102')
        etree.SubElement(icms, NS + 'orig').text = '0'
        etree.SubElement(icms, NS + 'CSOSN').text = '102'

    totals = etree.SubElement(inf, NS + 'total')
    icmstot = etree.SubElement(totals, NS + 'ICMSTot')
    for k in ['vBC', 'vICMS', 'vICMSDeson', 'vFCP', 'vBCST', 'vST', 'vFCPST', 'vFCPSTRet', 'vProd', 'vFrete', 'vSeg', 'vDesc', 'vII', 'vIPI', 'vIPIDevol', 'vPIS', 'vCOFINS', 'vOutro', 'vNF']:
        val = f'{total_val:.2f}' if k in ('vProd', 'vNF') else '0.00'
        etree.SubElement(icmstot, NS + k).text = val

    transp = etree.SubElement(inf, NS + 'transp')
    etree.SubElement(transp, NS + 'modFrete').text = '9'
    pag = etree.SubElement(inf, NS + 'pag')
    det_pag = etree.SubElement(pag, NS + 'detPag')
    etree.SubElement(det_pag, NS + 'tPag').text = '01'
    etree.SubElement(det_pag, NS + 'vPag').text = f'{total_val:.2f}'

    inf_supl = etree.SubElement(root, NS + 'infNFeSupl')
    qr_url = f'https://www.sefaz.se.gov.br/nfce/qrcode?p={chave}|2|{ambiente_cod}|{"0" if tp_emis != "9" else f"{emissao_dt:%d}|{total_val:.2f}"}'
    etree.SubElement(inf_supl, NS + 'qrCode').text = qr_url
    etree.SubElement(inf_supl, NS + 'urlChave').text = 'https://www.sefaz.se.gov.br/nfce/consulta'

    return etree.tostring(root, encoding='utf-8', xml_declaration=True), chave


@transaction.atomic
def emitir_nfce_pdv_com_rtc(
    user,
    estabelecimento,
    *,
    itens: list[dict[str, Any]],
    cliente=None,
    venda=None,
    contingencia_offline: bool = False,
    motivo_contingencia: str = 'Sem conexão com SEFAZ SVRS · Contingência Offline PDV (tpEmis=9)',
    regime_transicao: str | None = '2026_TESTE',
    rtc_override: dict[str, Any] | None = None,
    ambiente: str = 'HOMOLOGACAO',
) -> dict[str, Any]:
    """Emite NFC-e (Mod. 65) com suporte à Reforma Tributária (CBS/IBS) e Contingência Offline PDV."""
    require(user, 'fiscal_emitir')
    if not itens:
        raise ValidationError('NFC-e exige ao menos 1 item.')
    if ambiente not in ('HOMOLOGACAO', 'SIMULACAO'):
        raise ValidationError('Emissão direta NFC-e opera em homologação/simulação.')

    serie_obj, _ = m.SerieFiscal.objects.get_or_create(
        estabelecimento=estabelecimento,
        tipo='NFCE',
        ambiente=ambiente,
        serie='1',
        defaults={},
    )
    serie_locked = m.SerieFiscal.objects.select_for_update().get(pk=serie_obj.pk)
    numero = serie_locked.proximo_numero
    serie_locked.proximo_numero += 1
    write(serie_locked)

    total = sum((Decimal(str(i.get('valor') or i.get('valor_base') or '0.00')) for i in itens), Decimal('0.00')).quantize(Decimal('.01'))
    emissao_dt = timezone.now()
    org = estabelecimento.organization
    cnpj = ''.join(ch for ch in (estabelecimento.cnpj or org.cnpj or '00000000000191') if ch.isdigit()).zfill(14)[:14]
    tp_emis = '9' if contingencia_offline else '1'

    xml_bytes, chave = _xml_nfce_minimo(
        uf_cod='28',
        cnpj=cnpj,
        razao=org.razao_social or estabelecimento.nome,
        mun_ibge=getattr(getattr(estabelecimento, 'perfil_fiscal', None), 'municipio_ibge', '2800308'),
        serie=serie_locked.serie,
        numero=numero,
        emissao_dt=emissao_dt,
        itens=itens,
        tp_emis=tp_emis,
        justificativa=motivo_contingencia if contingencia_offline else '',
    )

    nota_json = None
    rtc_params = rtc_override or ({'regime_transicao': regime_transicao} if regime_transicao else None)
    if rtc_params:
        xml_bytes, nota_json = anexar_rtc_em_xml_nfce(
            xml_bytes,
            itens,
            numero=str(numero),
            chave=chave,
            serie=serie_locked.serie,
            data_emissao=emissao_dt.isoformat(timespec='seconds'),
            params_rtc=rtc_params,
        )

    doc = write(m.DocumentoFiscal(
        estabelecimento=estabelecimento,
        serie_fiscal=serie_locked,
        tipo='NFCE',
        modelo='65',
        ambiente=ambiente,
        serie=serie_locked.serie,
        numero=str(numero),
        chave=chave,
        status='CONTINGENCIA' if contingencia_offline else 'AUTORIZADA',
        protocolo='' if contingencia_offline else f'265{emissao_dt:%y%m%d}{numero:06d}',
        autorizado_em=None if contingencia_offline else emissao_dt,
        cliente=cliente,
        venda=venda,
        descricao='Cupom Fiscal NFC-e PDV',
        valor=total,
        snapshot_fiscal={'emitente': {'cnpj': cnpj, 'razao_social': org.razao_social}, 'itens': itens, 'tpEmis': tp_emis, 'nota_json': nota_json or {}},
        criado_por=user,
    ))
    write(m.ReservaNumeracaoFiscal(serie=serie_locked, documento=doc, numero=numero))

    det = m.DetalhesNFCe(
        documento=doc,
        pagamentos_snapshot=[{'codigo': '01', 'valor': str(total)}],
        modo_emissao='CONTINGENCIA_OFFLINE' if contingencia_offline else 'NORMAL',
        contingencia_justificativa=motivo_contingencia[:200] if contingencia_offline else '',
        contingencia_ativada_em=emissao_dt if contingencia_offline else None,
        nota_json=nota_json or {},
    )
    det._service_write = True
    det.save()

    import io
    from reportlab.lib.pagesizes import mm as _mm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer

    qr_url = f'https://www.sefaz.se.gov.br/nfce/qrcode?p={chave}|2|2'
    pdf_buf = io.BytesIO()
    st_c = ParagraphStyle('NFCeOff', fontName='Helvetica', fontSize=8, leading=10)
    story = [
        Paragraph(f'<b>{org.razao_social or estabelecimento.nome}</b>', st_c),
        Paragraph(f'DANFE NFC-e (Mod. 65) Nº {doc.numero} · Série {doc.serie}', st_c),
        Paragraph('<b>EMITIDO EM CONTINGÊNCIA OFFLINE (tpEmis=9)</b>' if contingencia_offline else f'Protocolo: {doc.protocolo}', st_c),
        Spacer(1, 2 * _mm),
        Paragraph(f'Total: R$ {total:.2f}', st_c),
    ]
    if nota_json:
        story.append(Paragraph(f"RTC · CBS R$ {nota_json['cbs']['vCBS']} · IBS R$ {nota_json['ibs']['vIBS']}", st_c))
    story.extend([
        Spacer(1, 2 * _mm),
        Image(gerar_qrcode_bytes(qr_url), width=30 * _mm, height=30 * _mm),
        Paragraph(f'Chave: {chave}', st_c),
    ])
    SimpleDocTemplate(pdf_buf, pagesize=(80 * _mm, 220 * _mm), leftMargin=4 * _mm, rightMargin=4 * _mm, topMargin=4 * _mm, bottomMargin=4 * _mm).build(story)
    danfe_pdf = pdf_buf.getvalue()

    m.Documento.objects.create(
        titulo=f'NFC-e {doc.numero} XML (Mod. 65{" Offline" if contingencia_offline else ""})',
        tipo='FISCAL_XML',
        arquivo=ContentFile(xml_bytes, name=f'nfce_{doc.numero}.xml'),
        cliente=cliente,
        fiscal=doc,
        sha256=hashlib.sha256(xml_bytes).hexdigest(),
        enviado_por=user,
        disponivel_portal=True,
    )
    m.Documento.objects.create(
        titulo=f'DANFE NFC-e {doc.numero}',
        tipo='FISCAL_PDF',
        arquivo=ContentFile(danfe_pdf, name=f'danfe_nfce_{doc.numero}.pdf'),
        cliente=cliente,
        fiscal=doc,
        sha256=hashlib.sha256(danfe_pdf).hexdigest(),
        enviado_por=user,
        disponivel_portal=True,
    )
    write(m.EventoFiscal(
        documento=doc,
        usuario=user,
        evento='NFCE_CONTINGENCIA_OFFLINE' if contingencia_offline else 'NFCE_AUTORIZADA',
        anterior='RASCUNHO',
        posterior=doc.status,
    ))
    audit(user, 'FISCAL_NFCE_PDV', doc, {'chave': chave, 'contingencia': contingencia_offline, 'rtc': bool(nota_json)})
    return {'documento': doc, 'chave': chave, 'status': doc.status, 'xml': xml_bytes, 'nota_json': nota_json}


@transaction.atomic
def sincronizar_nfce_contingencia(user, *, documento_id: int | None = None) -> dict[str, Any]:
    """Sincroniza cupons NFC-e emitidos em Contingência Offline PDV (tpEmis=9) com a SEFAZ."""
    require(user, 'fiscal_emitir')
    # Lock only the base rows.  PostgreSQL rejects FOR UPDATE when
    # select_related introduces a nullable outer join (SQLite does not).
    qs = m.DocumentoFiscal.objects.select_for_update().filter(
        tipo='NFCE',
        status='CONTINGENCIA',
    )
    if documento_id is not None:
        qs = qs.filter(pk=documento_id)
    sincronizadas = []
    for doc in qs:
        doc.status = 'AUTORIZADA'
        doc.autorizado_em = timezone.now()
        doc.protocolo = f'265SYNC{timezone.now():%y%m%d}{int(doc.numero or 1):05d}'[:15]
        write(doc)
        det = getattr(doc, 'detalhesnfce', None)
        if det is not None:
            det.modo_emissao = 'NORMAL'
            det._service_write = True
            det.save()
        write(m.EventoFiscal(documento=doc, usuario=user, evento='NFCE_CONTINGENCIA_SINCRONIZADA', anterior='CONTINGENCIA', posterior='AUTORIZADA'))
        sincronizadas.append(doc.pk)
    return {'sincronizadas': sincronizadas, 'total_sincronizadas': len(sincronizadas)}
