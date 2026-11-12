"""Modelos para Notas Recebidas Automáticas (SEFAZ DF-e / Manifestação do Destinatário) com Reforma Tributária."""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from gestao.models import RegistroImutavel, Validado, ZERO


class ConfiguracaoDFeRecebimento(Validado):
    AMBIENTES = [
        ('SIMULACAO', 'Simulação local / Testes'),
        ('HOMOLOGACAO', 'Homologação SEFAZ AN'),
        ('PRODUCAO', 'Produção SEFAZ AN'),
    ]

    estabelecimento = models.OneToOneField(
        'platform_core.FilialEmpresa',
        on_delete=models.PROTECT,
        related_name='configuracao_dfe',
    )
    ambiente = models.CharField(max_length=15, choices=AMBIENTES, default='HOMOLOGACAO')
    habilitada = models.BooleanField(default=True)
    credencial_referencia = models.CharField(
        'Referência A1 (cofre:// ou simulacao://)',
        max_length=180,
        default='simulacao://dfe-local',
    )
    ultimo_nsu = models.CharField(max_length=15, default='000000000000000')
    max_nsu = models.CharField(max_length=15, default='000000000000000')
    auto_ciencia = models.BooleanField('Manifestar Ciência da Operação (210210) automaticamente', default=True)
    ultima_sincronizacao_em = models.DateTimeField(null=True, blank=True, editable=False)

    def __str__(self) -> str:
        return f'DF-e Recebimento · {self.estabelecimento} (NSU {self.ultimo_nsu})'

    def clean(self) -> None:
        super().clean()
        if self.estabelecimento_id and self.organization_id and self.estabelecimento.organization_id != self.organization_id:
            raise ValidationError('Estabelecimento fora da organização.')
        ref = str(self.credencial_referencia or '').strip()
        if not ref.startswith(('cofre://', 'env://', 'simulacao://')):
            raise ValidationError('Referência A1 DF-e deve iniciar com cofre://, env:// ou simulacao://.')


class NotaFiscalRecebida(Validado):
    SITUACOES = [
        ('AUTORIZADA', 'Autorizada na SEFAZ'),
        ('CANCELADA', 'Cancelada pelo emitente'),
        ('DENEGADA', 'Uso denegado'),
    ]
    MANIFESTACOES = [
        ('PENDENTE', 'Sem manifestação'),
        ('CIENCIA', '210210 · Ciência da Operação'),
        ('CONFIRMADA', '210200 · Confirmação da Operação'),
        ('DESCONHECIDA', '210220 · Desconhecimento da Operação'),
        ('NAO_REALIZADA', '210240 · Operação Não Realizada'),
    ]
    ORIGENS = [
        ('DFE_AUTOMATICA', 'Captura automática DF-e (SEFAZ)'),
        ('XML_MANUAL', 'Importação XML manual'),
    ]

    estabelecimento = models.ForeignKey(
        'platform_core.FilialEmpresa',
        on_delete=models.PROTECT,
        related_name='notas_recebidas',
    )
    chave_acesso = models.CharField('Chave de acesso (44 dígitos)', max_length=44)
    nsu = models.CharField('NSU DF-e', max_length=15, blank=True)
    modelo = models.CharField(max_length=10, default='55')
    serie = models.CharField(max_length=6, default='1')
    numero = models.CharField(max_length=20)
    data_emissao = models.DateTimeField(default=timezone.now)
    cnpj_emitente = models.CharField('CNPJ do emitente', max_length=18)
    nome_emitente = models.CharField('Razão social emitente', max_length=180)
    fornecedor = models.ForeignKey(
        'gestao.Fornecedor',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='notas_recebidas',
    )
    compra = models.ForeignKey(
        'operacao.Compra',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='notas_recebidas',
    )
    valor_total = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(ZERO)])
    valor_cbs = models.DecimalField('CBS (Reforma Tributária)', max_digits=14, decimal_places=2, default=ZERO)
    valor_ibs = models.DecimalField('IBS (Reforma Tributária)', max_digits=14, decimal_places=2, default=ZERO)
    valor_is = models.DecimalField('IS (Imposto Seletivo)', max_digits=14, decimal_places=2, default=ZERO)
    reforma_tributaria = models.BooleanField(default=False)
    nota_json = models.JSONField(default=dict)
    itens_resumo = models.JSONField(default=list)
    situacao_sefaz = models.CharField(max_length=15, choices=SITUACOES, default='AUTORIZADA')
    manifestacao_status = models.CharField(max_length=20, choices=MANIFESTACOES, default='PENDENTE')
    protocolo_manifestacao = models.CharField(max_length=40, blank=True)
    manifestada_em = models.DateTimeField(null=True, blank=True)
    origem_captura = models.CharField(max_length=20, choices=ORIGENS, default='DFE_AUTOMATICA')
    resumo_apenas = models.BooleanField('Apenas resumo (aguardando Ciência/XML completo)', default=False)
    xml_armazenado = models.ForeignKey(
        'operacao.Documento',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='notas_recebidas_xml',
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-data_emissao', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'chave_acesso'], name='nota_recebida_org_chave_unica'),
        ]

    def __str__(self) -> str:
        return f'NF-e {self.numero} · {self.nome_emitente} (R$ {self.valor_total})'

    def clean(self) -> None:
        super().clean()
        if len(''.join(ch for ch in str(self.chave_acesso) if ch.isdigit())) != 44:
            raise ValidationError({'chave_acesso': 'Chave de acesso deve conter exatamente 44 dígitos.'})
        if self.fornecedor_id and self.organization_id and self.fornecedor.organization_id != self.organization_id:
            raise ValidationError('Fornecedor pertence a outra organização.')
        if self.compra_id and self.organization_id and self.compra.organization_id != self.organization_id:
            raise ValidationError('Compra pertence a outra organização.')


class ManifestacaoDFeEvento(RegistroImutavel):
    nota = models.ForeignKey(NotaFiscalRecebida, on_delete=models.PROTECT, related_name='manifestacoes')
    codigo_evento = models.CharField(max_length=10)
    descricao_evento = models.CharField(max_length=80)
    justificativa = models.TextField(blank=True)
    protocolo = models.CharField(max_length=40, blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em', '-pk']
