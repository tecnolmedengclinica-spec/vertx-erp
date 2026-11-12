"""Modelos multi-tenant para API de Integração Bancária (Pix, Boleto, Webhook e Extrato automático)."""
from __future__ import annotations

import uuid
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from gestao.models import RegistroImutavel, Validado, ZERO


def validar_referencia_bancaria(referencia: str) -> None:
    text = str(referencia or '').strip()
    lowered = text.lower()
    if any(token in lowered for token in ('begin private', '-----begin', 'password=', 'bearer ', 'secret=')):
        raise ValidationError('Armazene apenas referência opaca (`cofre://`, `env://` ou `simulacao://`), nunca o segredo bruto.')
    if not text.startswith(('cofre://', 'env://', 'simulacao://')):
        raise ValidationError('Referência de credencial bancária deve iniciar com cofre://, env:// ou simulacao://.')


class IntegracaoBancaria(Validado):
    PROVEDORES = [
        ('SIMULADO', 'Simulado / Homologação Local'),
        ('ASAAS', 'Asaas Cobranças & Pix API'),
        ('BANCO_DO_BRASIL', 'Banco do Brasil API (Pix / Cobrança)'),
        ('INTER', 'Banco Inter PJ API v2'),
        ('SICOOB', 'Sicoob Open Finance / Pix'),
        ('SICREDI', 'Sicredi Cobrança API'),
    ]
    AMBIENTES = [
        ('SANDBOX', 'Sandbox / Homologação'),
        ('PRODUCAO', 'Produção'),
    ]

    conta = models.OneToOneField('operacao.ContaBancaria', on_delete=models.PROTECT, related_name='integracao_bancaria')
    provedor = models.CharField(max_length=20, choices=PROVEDORES, default='SIMULADO')
    ambiente = models.CharField(max_length=12, choices=AMBIENTES, default='SANDBOX')
    client_id = models.CharField('Client ID / Convênio público', max_length=120, blank=True)
    credencial_referencia = models.CharField(
        'Referência segura (cofre://, env:// ou simulacao://)',
        max_length=180,
        default='simulacao://banco-local',
    )
    webhook_token_hash = models.CharField(max_length=64, blank=True, editable=False)
    chave_pix = models.CharField('Chave Pix recebedora', max_length=120, blank=True)
    convenio_boleto = models.CharField('Convênio / Carteira de boleto', max_length=40, blank=True)
    conciliar_automaticamente = models.BooleanField(
        'Liquidar conta e movimentar caixa automaticamente no pagamento',
        default=True,
    )
    ativo = models.BooleanField(default=True)
    ultima_sincronizacao_em = models.DateTimeField(null=True, blank=True, editable=False)
    ultimo_status = models.CharField(max_length=160, blank=True, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['conta__nome']

    def __str__(self) -> str:
        return f'{self.conta.nome} · {self.get_provedor_display()} ({self.ambiente})'

    def clean(self) -> None:
        super().clean()
        if self.conta_id and self.organization_id and self.conta.organization_id != self.organization_id:
            raise ValidationError('Conta bancária pertence a outra organização.')
        validar_referencia_bancaria(self.credencial_referencia)


class CobrancaBancaria(Validado):
    TIPOS = [
        ('PIX', 'Pix QR Code'),
        ('BOLETO', 'Boleto Registrado'),
        ('HIBRIDO', 'Boleto + Pix (Híbrido)'),
    ]
    STATUS = [
        ('ATIVA', 'Ativa / Aguardando pagamento'),
        ('PAGA', 'Paga / Liquidada'),
        ('VENCIDA', 'Vencida'),
        ('CANCELADA', 'Cancelada'),
        ('ERRO', 'Erro na emissão'),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request_id = models.UUIDField(default=uuid.uuid4, editable=False)
    integracao = models.ForeignKey(IntegracaoBancaria, on_delete=models.PROTECT, related_name='cobrancas')
    conta = models.ForeignKey('operacao.ContaBancaria', on_delete=models.PROTECT, related_name='cobrancas_api')
    lancamento = models.ForeignKey(
        'gestao.Lancamento',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='cobrancas_bancarias',
    )
    tipo = models.CharField(max_length=10, choices=TIPOS, default='HIBRIDO')
    status = models.CharField(max_length=12, choices=STATUS, default='ATIVA')
    external_id = models.CharField(max_length=120, blank=True)
    txid = models.CharField('TXID Pix', max_length=64, blank=True)
    nosso_numero = models.CharField('Nosso número', max_length=40, blank=True)
    valor = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    valor_pago = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    vencimento = models.DateField()
    pago_em = models.DateTimeField(null=True, blank=True)
    pix_copia_cola = models.TextField('Pix Copia e Cola', blank=True)
    linha_digitavel = models.CharField('Linha digitável', max_length=80, blank=True)
    codigo_barras = models.CharField('Código de barras', max_length=60, blank=True)
    url_boleto = models.URLField('URL 2ª via / PDF', max_length=300, blank=True)
    pagamento = models.OneToOneField(
        'operacao.Pagamento',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='cobranca_bancaria',
    )
    payload_sanitizado = models.JSONField(default=dict, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'request_id'], name='cobranca_bancaria_org_req_unico'),
        ]

    def __str__(self) -> str:
        return f'Cobrança #{self.pk or "nova"} · R$ {self.valor} ({self.get_status_display()})'

    def clean(self) -> None:
        super().clean()
        if self.integracao_id and self.conta_id and self.integracao.conta_id != self.conta_id:
            raise ValidationError('Integração selecionada pertence a outra conta bancária.')
        if self.lancamento_id and self.organization_id and self.lancamento.organization_id != self.organization_id:
            raise ValidationError('Lançamento financeiro pertence a outra organização.')
        if self.lancamento_id and self.lancamento.tipo != 'RECEBER':
            raise ValidationError('Cobrança bancária só pode ser emitida para contas a RECEBER.')


class WebhookBancarioEvento(RegistroImutavel):
    integracao = models.ForeignKey(IntegracaoBancaria, on_delete=models.PROTECT, related_name='webhooks')
    evento_id = models.CharField(max_length=120)
    tipo_evento = models.CharField(max_length=60)
    cobranca = models.ForeignKey(
        CobrancaBancaria,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='eventos_webhook',
    )
    payload = models.JSONField(default=dict)
    processado = models.BooleanField(default=False)
    processado_em = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['integracao', 'evento_id'], name='webhook_banco_evento_unico'),
        ]
