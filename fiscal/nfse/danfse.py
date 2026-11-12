"""Gerador do DANFSE (Documento Auxiliar da NFS-e Nacional) em PDF com QR Code Oficial."""
import io
import qrcode
from decimal import Decimal
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


def gerar_qrcode_bytes(url_consulta: str) -> io.BytesIO:
    """Gera o QR Code em memória para inclusão no PDF."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=4,
        border=1,
    )
    qr.add_data(url_consulta)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def formatar_chave_50(chave: str) -> str:
    """Formata a chave nacional de 50 dígitos em blocos legíveis."""
    limpa = chave.replace(" ", "")
    return " ".join([limpa[i:i+4] for i in range(0, len(limpa), 4)])


def gerar_danfse_pdf(dados_nfse: dict) -> bytes:
    """Renderiza o documento auxiliar DANFSE em PDF segundo o leiaute do padrão nacional."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm
    )

    styles = getSampleStyleSheet()

    style_title = ParagraphStyle(
        'DanfseTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=14,
        alignment=1,
        textColor=colors.HexColor('#0f172a')
    )

    style_subtitle = ParagraphStyle(
        'DanfseSubTitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        alignment=1,
        textColor=colors.HexColor('#64748b')
    )

    style_label = ParagraphStyle(
        'DanfseLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=7,
        leading=9,
        textColor=colors.HexColor('#475569')
    )

    style_val = ParagraphStyle(
        'DanfseValue',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#0f172a')
    )

    style_chave = ParagraphStyle(
        'DanfseChave',
        parent=styles['Normal'],
        fontName='Courier-Bold',
        fontSize=8,
        leading=10,
        alignment=1,
        textColor=colors.HexColor('#1e293b')
    )

    elements = []

    # 1. Cabeçalho
    header_text = [
        Paragraph("<b>DANFSE — Documento Auxiliar da Nota Fiscal de Serviço Eletrônica Nacional</b>", style_title),
        Paragraph("Sistema Nacional da NFS-e · Receita Federal do Brasil / SEFIN", style_subtitle)
    ]
    elements.extend(header_text)
    elements.append(Spacer(1, 4 * mm))

    # Chave e QR Code
    chave_acesso = dados_nfse.get('chave_acesso', '0' * 50)
    url_consulta = f"https://www.nfse.gov.br/consultapublica?chave={chave_acesso}"
    qr_buf = gerar_qrcode_bytes(url_consulta)
    qr_img = Image(qr_buf, width=28 * mm, height=28 * mm)

    info_box = [
        [
            Paragraph("<b>CHAVE DE ACESSO NACIONAL</b>", style_label),
            Paragraph(f"<b>Nº DA NFS-E:</b> {dados_nfse.get('numero_nfse', '1')}", style_label)
        ],
        [
            Paragraph(formatar_chave_50(chave_acesso), style_chave),
            Paragraph(f"<b>EMISSÃO:</b> {dados_nfse.get('data_emissao', '-')}", style_val)
        ],
        [
            Paragraph("Consulte a autenticidade deste documento no portal nacional apontando a câmera para o QR Code ao lado.", style_subtitle),
            Paragraph(f"<b>COMPETÊNCIA:</b> {dados_nfse.get('competencia', '-')}", style_val)
        ]
    ]

    t_info = Table(info_box, colWidths=[120 * mm, 38 * mm])
    t_info.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#94a3b8')),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 4),
    ]))

    t_top = Table([[t_info, qr_img]], colWidths=[158 * mm, 30 * mm])
    t_top.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, 0), 'CENTER'),
    ]))
    elements.append(t_top)
    elements.append(Spacer(1, 3 * mm))

    # 2. Dados do Prestador
    prestador = dados_nfse.get('prestador', {})
    t_prest = Table([
        [Paragraph("<b>DADOS DO PRESTADOR DE SERVIÇOS</b>", style_label), ""],
        [Paragraph(f"<b>Razão Social:</b> {prestador.get('razao_social', '-')}", style_val),
         Paragraph(f"<b>CNPJ:</b> {prestador.get('cnpj', '-')}", style_val)],
        [Paragraph(f"<b>Município:</b> {prestador.get('municipio', '-')}", style_val),
         Paragraph(f"<b>Inscrição Municipal:</b> {prestador.get('im', '-')}", style_val)],
    ], colWidths=[110 * mm, 78 * mm])
    t_prest.setStyle(TableStyle([
        ('SPAN', (0, 0), (1, 0)),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e2e8f0')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#94a3b8')),
        ('PADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(t_prest)
    elements.append(Spacer(1, 2.5 * mm))

    # 3. Dados do Tomador
    tomador = dados_nfse.get('tomador', {})
    t_toma = Table([
        [Paragraph("<b>DADOS DO TOMADOR DE SERVIÇOS</b>", style_label), ""],
        [Paragraph(f"<b>Razão Social / Nome:</b> {tomador.get('razao_social_nome', '-')}", style_val),
         Paragraph(f"<b>CNPJ / CPF:</b> {tomador.get('cnpj_cpf', '-')}", style_val)],
        [Paragraph(f"<b>Endereço:</b> {tomador.get('endereco', '-')}", style_val),
         Paragraph(f"<b>E-mail:</b> {tomador.get('email', '-')}", style_val)],
    ], colWidths=[110 * mm, 78 * mm])
    t_toma.setStyle(TableStyle([
        ('SPAN', (0, 0), (1, 0)),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e2e8f0')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#94a3b8')),
        ('PADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(t_toma)
    elements.append(Spacer(1, 2.5 * mm))

    # 4. Discriminação dos Serviços
    servico = dados_nfse.get('servico', {})
    t_serv = Table([
        [Paragraph("<b>DISCRIMINAÇÃO DOS SERVIÇOS</b>", style_label)],
        [Paragraph(servico.get('discriminacao', '-').replace('\n', '<br/>'), style_val)],
        [Paragraph(f"<b>Código Tributação Nacional / LC 116:</b> {servico.get('codigo_tributacao', '14.01')}", style_label)]
    ], colWidths=[188 * mm])
    t_serv.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#e2e8f0')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#94a3b8')),
        ('PADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(t_serv)
    elements.append(Spacer(1, 2.5 * mm))

    # 5. Quadro de Valores e Tributos
    valores = dados_nfse.get('valores', {})
    t_vals = Table([
        [
            Paragraph("<b>VALOR TOTAL DO SERVIÇO</b>", style_label),
            Paragraph("<b>DEDUÇÕES</b>", style_label),
            Paragraph("<b>BASE DE CÁLCULO ISS</b>", style_label),
            Paragraph("<b>ALÍQUOTA (%)</b>", style_label),
            Paragraph("<b>VALOR DO ISS</b>", style_label),
            Paragraph("<b>VALOR LÍQUIDO</b>", style_label),
        ],
        [
            Paragraph(f"R$ {valores.get('valor_servicos', '0,00')}", style_val),
            Paragraph(f"R$ {valores.get('valor_deducoes', '0,00')}", style_val),
            Paragraph(f"R$ {valores.get('base_calculo', '0,00')}", style_val),
            Paragraph(f"{valores.get('aliquota', '0,00')}%", style_val),
            Paragraph(f"R$ {valores.get('valor_iss', '0,00')}", style_val),
            Paragraph(f"<b>R$ {valores.get('valor_liquido', '0,00')}</b>", style_val),
        ]
    ], colWidths=[31 * mm, 31 * mm, 32 * mm, 30 * mm, 32 * mm, 32 * mm])
    t_vals.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e2e8f0')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#94a3b8')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('PADDING', (0, 0), (-1, -1), 4),
        ('ALIGN', (0, 1), (-1, 1), 'CENTER'),
    ]))
    elements.append(t_vals)

    doc.build(elements)
    return buffer.getvalue()
