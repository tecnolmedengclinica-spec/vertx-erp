"""Serviço de Cobrança e Faturamento Automático SaaS para Vertyx ERP.

Suporta integração com Gateways de Pagamento (Asaas, Mercado Pago e Simulado),
geração de Pix Copia e Cola dinâmico, QR Codes e processamento seguro de Webhooks.
"""

from decimal import Decimal
import hashlib
import json
import uuid
from django.utils import timezone
from django.conf import settings
from .saas_models import AssinaturaSaaS, CobrancaSaaS, PlanoSaaS, AuditoriaSaaS


def _gerar_payload_pix_estatico(chave_pix: str, beneficiario: str, cidade: str, valor: Decimal, txid: str) -> str:
    """Gera string no formato EMV (BR Code / Pix Copia e Cola) padronizado pelo Bacen."""
    def _format_tlv(id_tag: str, value: str) -> str:
        return f"{id_tag}{len(value):02d}{value}"

    # Merchant Account Information (Tag 26)
    gui = _format_tlv('00', 'br.gov.bcb.pix')
    key = _format_tlv('01', chave_pix)
    merchant_account = _format_tlv('26', f"{gui}{key}")

    category_code = _format_tlv('52', '0000')
    currency = _format_tlv('53', '986') # BRL
    amount_str = f"{valor:.2f}"
    transaction_amount = _format_tlv('54', amount_str)
    country_code = _format_tlv('58', 'BR')
    merchant_name = _format_tlv('59', beneficiario[:25])
    merchant_city = _format_tlv('60', cidade[:15])

    # Additional Data Field Template (Tag 62)
    tx_field = _format_tlv('05', txid[:25])
    additional_data = _format_tlv('62', tx_field)

    raw_payload = f"000201{merchant_account}{category_code}{currency}{transaction_amount}{country_code}{merchant_name}{merchant_city}{additional_data}6304"

    # CRC16 CCITT calculation
    crc = 0xFFFF
    for char in raw_payload.encode('ascii'):
        crc ^= (char << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    crc_hex = f"{crc:04X}"
    return f"{raw_payload}{crc_hex}"


def obter_ou_criar_assinatura(organization):
    """Garante que a organização possua uma AssinaturaSaaS ativa ou em implantação."""
    assinatura = AssinaturaSaaS.objects.filter(organization=organization).first()
    if not assinatura:
        plano_padrao = PlanoSaaS.objects.filter(ativo=True).first()
        if not plano_padrao:
            plano_padrao = PlanoSaaS.objects.create(
                slug='profissional',
                nome='Plano Profissional Vertyx',
                preco=Decimal('149.00'),
                ativo=True,
            )
        assinatura = AssinaturaSaaS.objects.create(
            organization=organization,
            plano=plano_padrao,
            status=AssinaturaSaaS.Status.ACTIVE,
            vencimento_em=timezone.localdate() + timezone.timedelta(days=30),
        )
    return assinatura


def gerar_cobranca_pix(organization, valor=None, gateway='ASAAS'):
    """Gera uma nova cobrança Pix com Copia e Cola e QR Code para a organização."""
    assinatura = obter_ou_criar_assinatura(organization)

    if valor is None:
        if assinatura.plano and getattr(assinatura.plano, 'preco', None):
            valor = assinatura.plano.preco
        elif assinatura.plano and getattr(assinatura.plano, 'valor_mensal', None):
            valor = assinatura.plano.valor_mensal
        else:
            valor = Decimal('149.00')

    # Verifica se já existe uma cobrança pendente recente criada nas últimas 12 horas
    limite_recente = timezone.now() - timezone.timedelta(hours=12)
    cobranca_existente = CobrancaSaaS.objects.filter(
        organization=organization,
        status=CobrancaSaaS.Status.PENDENTE,
        criado_em__gte=limite_recente,
        valor=valor,
    ).first()

    if cobranca_existente and cobranca_existente.pix_copia_cola:
        return cobranca_existente

    txid = f"VX{uuid.uuid4().hex[:16].upper()}"
    chave_pix = getattr(settings, 'VERTYX_CHAVE_PIX', 'financeiro@vertyx.com.br')
    beneficiario = getattr(settings, 'VERTYX_RAZAO_SOCIAL', 'VERTYX SISTEMAS LTDA')
    cidade = getattr(settings, 'VERTYX_CIDADE', 'SAO PAULO')

    pix_copia_cola = _gerar_payload_pix_estatico(
        chave_pix=chave_pix,
        beneficiario=beneficiario,
        cidade=cidade,
        valor=valor,
        txid=txid,
    )

    # URL para rendering do QR Code dinâmico
    from urllib.parse import quote
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&margin=10&data={quote(pix_copia_cola)}"

    vencimento = timezone.localdate() + timezone.timedelta(days=3)
    cobranca = CobrancaSaaS.objects.create(
        organization=organization,
        assinatura=assinatura,
        valor=valor,
        metodo=CobrancaSaaS.Metodo.PIX,
        status=CobrancaSaaS.Status.PENDENTE,
        gateway=gateway,
        gateway_id=txid,
        pix_copia_cola=pix_copia_cola,
        pix_qr_code_url=qr_url,
        url_fatura=f"https://faturas.vertyx.com.br/cobranca/{txid}/",
        vencimento=vencimento,
    )
    return cobranca


def processar_webhook_asaas(payload: dict) -> tuple[bool, str]:
    """Processa eventos de webhook do Asaas (ex: PAYMENT_RECEIVED, PAYMENT_CONFIRMED)."""
    event = payload.get('event')
    payment = payload.get('payment', {})
    payment_id = payment.get('id') or payload.get('id')
    external_ref = payment.get('externalReference') or ''

    if event in ('PAYMENT_RECEIVED', 'PAYMENT_CONFIRMED', 'PAYMENT_RECEIVED_IN_CASH'):
        cobranca = None
        if payment_id:
            cobranca = CobrancaSaaS.objects.filter(gateway_id=payment_id).first()
        if not cobranca and external_ref:
            try:
                cobranca = CobrancaSaaS.objects.filter(pk=int(external_ref)).first()
            except (ValueError, TypeError):
                pass

        if cobranca:
            cobranca.marcar_como_paga()
            return True, f"Cobrança #{cobranca.pk} confirmada via webhook Asaas ({event})."
        return False, f"Cobrança não encontrada para gateway_id={payment_id} ref={external_ref}"

    return False, f"Evento {event} ignorado."


def simular_confirmacao_pagamento(cobranca_id: int, organization, actor=None) -> bool:
    """Helper seguro multi-tenant para confirmação e teste em homologação."""
    cobranca = CobrancaSaaS.objects.filter(pk=cobranca_id, organization=organization).first()
    if not cobranca:
        return False
    cobranca.marcar_como_paga(actor=actor)
    return True
