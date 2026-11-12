"""Gerador de DANFE (Modelo 55 · Retrato A4) com resumo da Reforma Tributária (CBS / IBS)."""
from __future__ import annotations

import io
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from fiscal.nfse.danfse import gerar_qrcode_bytes


def formatar_chave_44(chave: str) -> str:
    limpa = ''.join(ch for ch in str(chave or '') if ch.isalnum())
    return ' '.join(limpa[i:i + 4] for i in range(0, len(limpa), 4))


def gerar_danfe_nfe_pdf(dados: dict) -> bytes:
    """Renderiza DANFE Modelo 55 A4 com quadro completo de tributos (ICMS + CBS/IBS RTC)."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    st_title = ParagraphStyle('NFeTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=11, leading=13, alignment=1, textColor=colors.HexColor('#0f172a'))
    st_sub = ParagraphStyle('NFeSub', parent=styles['Normal'], fontName='Helvetica', fontSize=8, leading=10, alignment=1, textColor=colors.HexColor('#475569'))
    st_lbl = ParagraphStyle('NFeLbl', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7, leading=9, textColor=colors.HexColor('#475569'))
    st_val = ParagraphStyle('NFeVal', parent=styles['Normal'], fontName='Helvetica', fontSize=8.5, leading=11, textColor=colors.HexColor('#0f172a'))
    st_key = ParagraphStyle('NFeKey', parent=styles['Normal'], fontName='Courier-Bold', fontSize=8, leading=10, alignment=1, textColor=colors.HexColor('#0f172a'))

    emit = dados.get('emitente') or {}
    dest = dados.get('destinatario') or {}
    tot = dados.get('totais') or {}
    rtc = dados.get('nota_json') or {}
    chave = str(dados.get('chave') or '0' * 44)
    qr_url = dados.get('consulta_url') or f'https://www.nfe.fazenda.gov.br/portal/consultaRecaptcha.aspx?chNFe={chave}'
    qr_img = Image(gerar_qrcode_bytes(qr_url), width=23 * mm, height=23 * mm)

    elements = []
    raw_end = emit.get('endereco')
    if isinstance(raw_end, dict):
        end_txt = f"{raw_end.get('logradouro', '-')}, {raw_end.get('numero', 'S/N')} · {raw_end.get('bairro', '')}".strip(' ·')
    else:
        end_txt = str(raw_end or '-')
    hdr = [
        [
            Paragraph(
                f"<b>{emit.get('razao_social', 'VERTYX ERP')}</b><br/>"
                f"CNPJ: {emit.get('cnpj', '-')} · IE: {emit.get('ie', '-')}<br/>"
                f"{end_txt}",
                st_val,
            ),
            Paragraph(
                f"<b>DANFE · NF-e (Mod. 55)</b><br/>"
                f"Nº <b>{dados.get('numero', '1')}</b> · Série {dados.get('serie', '1')}<br/>"
                f"{dados.get('ambiente_label', 'HOMOLOGAÇÃO')}",
                st_title,
            ),
            qr_img,
        ]
    ]
    tbl_hdr = Table(hdr, colWidths=[82 * mm, 76 * mm, 30 * mm])
    tbl_hdr.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.9, colors.HexColor('#1e293b')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (2, 0), (2, 0), 'CENTER'),
        ('PADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(tbl_hdr)
    elements.append(Spacer(1, 2.5 * mm))

    tbl_key = Table([[
        Paragraph('CHAVE DE ACESSO NF-e (MODELO 55)', st_lbl),
        Paragraph('PROTOCOLO DE AUTORIZAÇÃO', st_lbl),
    ], [
        Paragraph(formatar_chave_44(chave), st_key),
        Paragraph(str(dados.get('protocolo') or 'Aguardando retorno SEFAZ'), st_val),
    ]], colWidths=[122 * mm, 66 * mm])
    tbl_key.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.7, colors.HexColor('#94a3b8')),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
        ('PADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(tbl_key)
    elements.append(Spacer(1, 2.5 * mm))

    tbl_dest = Table([[
        Paragraph('DESTINATÁRIO / REMETENTE', st_lbl),
        Paragraph('CPF / CNPJ', st_lbl),
        Paragraph('EMISSÃO', st_lbl),
    ], [
        Paragraph(str(dest.get('nome') or 'Consumidor não identificado'), st_val),
        Paragraph(str(dest.get('documento') or '-'), st_val),
        Paragraph(str(dados.get('data_emissao') or '-'), st_val),
    ]], colWidths=[96 * mm, 52 * mm, 40 * mm])
    tbl_dest.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.7, colors.HexColor('#94a3b8')),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
        ('PADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(tbl_dest)
    elements.append(Spacer(1, 2.5 * mm))

    rows_itens = [[
        Paragraph('COD', st_lbl),
        Paragraph('DESCRIÇÃO DO PRODUTO', st_lbl),
        Paragraph('NCM', st_lbl),
        Paragraph('CFOP', st_lbl),
        Paragraph('QTD', st_lbl),
        Paragraph('VL UNIT', st_lbl),
        Paragraph('TOTAL', st_lbl),
    ]]
    for item in dados.get('itens') or []:
        rows_itens.append([
            Paragraph(str(item.get('sku', '-'))[:14], st_val),
            Paragraph(str(item.get('descricao', '-'))[:60], st_val),
            Paragraph(str(item.get('ncm', '-')), st_val),
            Paragraph(str(item.get('cfop', '-')), st_val),
            Paragraph(str(item.get('quantidade', '1')), st_val),
            Paragraph(str(item.get('valor_unitario', '0.00')), st_val),
            Paragraph(str(item.get('liquido', '0.00')), st_val),
        ])
    tbl_itens = Table(rows_itens, colWidths=[22 * mm, 74 * mm, 20 * mm, 14 * mm, 16 * mm, 21 * mm, 21 * mm])
    tbl_itens.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.7, colors.HexColor('#94a3b8')),
        ('INNERGRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#e2e8f0')),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
        ('PADDING', (0, 0), (-1, -1), 3.5),
    ]))
    elements.append(tbl_itens)
    elements.append(Spacer(1, 2.5 * mm))

    tot_rows = [[
        Paragraph('VALOR PRODUTOS', st_lbl),
        Paragraph('BASE ICMS / ICMS', st_lbl),
        Paragraph('PIS / COFINS', st_lbl),
        Paragraph('VALOR TOTAL NF-e', st_lbl),
    ], [
        Paragraph(f"R$ {tot.get('vProd', '0.00')}", st_val),
        Paragraph(f"R$ {tot.get('vBC', '0.00')} / R$ {tot.get('vICMS', '0.00')}", st_val),
        Paragraph(f"R$ {tot.get('vPIS', '0.00')} / R$ {tot.get('vCOFINS', '0.00')}", st_val),
        Paragraph(f"<b>R$ {tot.get('vNF', '0.00')}</b>", st_val),
    ]]
    tbl_tot = Table(tot_rows, colWidths=[44 * mm, 52 * mm, 46 * mm, 46 * mm])
    tbl_tot.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.7, colors.HexColor('#94a3b8')),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
        ('PADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(tbl_tot)

    if rtc and rtc.get('reforma_tributaria'):
        elements.append(Spacer(1, 2.5 * mm))
        cbs = rtc.get('cbs') or {}
        ibs = rtc.get('ibs') or {}
        tbl_rtc = Table([[
            Paragraph('REFORMA TRIBUTÁRIA (EC 132/2023 · NT 2025.002)', st_lbl),
            Paragraph('BASE CBS / IBS', st_lbl),
            Paragraph('CBS CALCULADA', st_lbl),
            Paragraph('IBS (UF + MUN)', st_lbl),
        ], [
            Paragraph(str(rtc.get('regime_transicao') or 'RTC'), st_val),
            Paragraph(f"R$ {rtc.get('vBCIBSCBS', '0.00')}", st_val),
            Paragraph(f"R$ {cbs.get('vCBS', '0.00')} ({cbs.get('aliquota', '0.00')}%)", st_val),
            Paragraph(f"R$ {ibs.get('vIBS', '0.00')} (UF R$ {ibs.get('vIBSUF', '0.00')})", st_val),
        ]], colWidths=[64 * mm, 36 * mm, 44 * mm, 44 * mm])
        tbl_rtc.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 0.8, colors.HexColor('#2563eb')),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eff6ff')),
            ('PADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(tbl_rtc)

    elements.append(Spacer(1, 3 * mm))
    elements.append(Paragraph('Documento gerado eletronicamente pelo sistema VERTYX ERP · Nota Técnica Fiscal v4.00 / RTC.', st_sub))
    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
