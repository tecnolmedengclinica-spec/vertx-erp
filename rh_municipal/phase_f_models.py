"""Phase F foundations: territorial cadastre, configurable taxes and service invoices.

This layer intentionally stores municipal rule snapshots and does not implement
legal calculations, bank collection integrations, or official NFS-e transport.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from .models import MunicipalRecord, Secretaria, DepartamentoMunicipal, SetorMunicipal, UnidadeMunicipal


PHASE_F_MODULES = {
    'tributos': 'tributos_municipais',
    'territorio': 'gestao_territorial',
    'nfse': 'nfse_municipal',
}


class PhaseFRecord(MunicipalRecord):
    secretaria = models.ForeignKey(Secretaria, null=True, blank=True, on_delete=models.PROTECT)
    departamento = models.ForeignKey(DepartamentoMunicipal, null=True, blank=True, on_delete=models.PROTECT)
    setor = models.ForeignKey(SetorMunicipal, null=True, blank=True, on_delete=models.PROTECT)
    unidade = models.ForeignKey(UnidadeMunicipal, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        for field in ('secretaria', 'departamento', 'setor', 'unidade'):
            related = getattr(self, field, None)
            if related and related.organization_id != self.organization_id:
                raise ValidationError({field: 'O vínculo pertence a outra Prefeitura.'})
        if self.departamento_id and self.secretaria_id and self.departamento.secretaria_id != self.secretaria_id:
            raise ValidationError({'departamento': 'O departamento não pertence à secretaria informada.'})
        if self.departamento_id and not self.secretaria_id:
            self.secretaria_id = self.departamento.secretaria_id
        if self.setor_id and self.departamento_id and self.setor.departamento_id != self.departamento_id:
            raise ValidationError({'setor': 'O setor não pertence ao departamento informado.'})
        if self.setor_id and not self.departamento_id:
            self.departamento_id = self.setor.departamento_id
            self.secretaria_id = self.setor.departamento.secretaria_id
        if self.unidade_id and self.setor_id and self.unidade.setor_id != self.setor_id:
            raise ValidationError({'unidade': 'A unidade não pertence ao setor informado.'})
        if self.unidade_id and not self.setor_id:
            self.setor_id = self.unidade.setor_id
            self.departamento_id = self.unidade.setor.departamento_id
            self.secretaria_id = self.unidade.setor.departamento.secretaria_id
        if self.setor_id and self.secretaria_id and self.setor.departamento.secretaria_id != self.secretaria_id:
            raise ValidationError({'setor': 'O setor não pertence à secretaria informada.'})
        if self.unidade_id and self.departamento_id and self.unidade.setor.departamento_id != self.departamento_id:
            raise ValidationError({'unidade': 'A unidade não pertence ao departamento informado.'})


class ContribuinteMunicipal(PhaseFRecord):
    class Tipo(models.TextChoices):
        PESSOA_FISICA = 'PF', 'Pessoa física'
        PESSOA_JURIDICA = 'PJ', 'Pessoa jurídica'

    class Situacao(models.TextChoices):
        ATIVO = 'ATIVO', 'Ativo'
        INATIVO = 'INATIVO', 'Inativo'
        SUSPENSO = 'SUSPENSO', 'Suspenso'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    codigo = models.CharField(max_length=32)
    tipo = models.CharField(max_length=2, choices=Tipo.choices)
    documento = models.CharField(max_length=14, help_text='CPF/CNPJ normalizado; acesso sujeito a permissão.')
    nome = models.CharField(max_length=180)
    email = models.EmailField(blank=True)
    telefone = models.CharField(max_length=30, blank=True)
    cep = models.CharField(max_length=9, blank=True)
    logradouro = models.CharField(max_length=180, blank=True)
    numero_endereco = models.CharField(max_length=24, blank=True)
    complemento = models.CharField(max_length=100, blank=True)
    bairro = models.CharField(max_length=100, blank=True)
    municipio = models.CharField(max_length=100, blank=True)
    uf = models.CharField(max_length=2, blank=True)
    inscricao_municipal = models.CharField(max_length=32, blank=True)
    cnae_principal = models.CharField(max_length=16, blank=True)
    regime_iss = models.CharField(
        max_length=20,
        choices=[
            ('HOMOLOGADO', 'ISS Homologado (Variável)'),
            ('FIXO', 'ISS Fixo Anual'),
            ('SIMPLES', 'Simples Nacional / DAS'),
            ('MEI', 'MEI'),
            ('ISENTO', 'Isento / Imune'),
        ],
        blank=True,
    )
    atividades = models.JSONField(default=list, blank=True)
    situacao = models.CharField(max_length=10, choices=Situacao.choices, default=Situacao.ATIVO)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['nome', 'pk']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_contrib_codigo_org'),
            models.UniqueConstraint(fields=['organization', 'documento'], name='rhmun_contrib_doc_org'),
        ]

    def clean(self):
        super().clean()
        expected = 11 if self.tipo == self.Tipo.PESSOA_FISICA else 14
        if not self.documento.isdigit() or len(self.documento) != expected:
            raise ValidationError({'documento': f'Informe documento normalizado de {expected} dígitos.'})
        if self.uf and len(self.uf) != 2:
            raise ValidationError({'uf': 'Informe a UF com duas letras.'})


class ImovelMunicipal(PhaseFRecord):
    class Uso(models.TextChoices):
        RESIDENCIAL = 'RESIDENCIAL', 'Residencial'
        COMERCIAL = 'COMERCIAL', 'Comercial'
        INDUSTRIAL = 'INDUSTRIAL', 'Industrial'
        RURAL = 'RURAL', 'Rural'
        MISTO = 'MISTO', 'Misto'
        OUTRO = 'OUTRO', 'Outro'

    class Situacao(models.TextChoices):
        ATIVO = 'ATIVO', 'Ativo'
        INATIVO = 'INATIVO', 'Inativo'
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    inscricao = models.CharField(max_length=40)
    matricula = models.CharField(max_length=50, blank=True)
    proprietario = models.ForeignKey(ContribuinteMunicipal, on_delete=models.PROTECT, related_name='imoveis_proprietario')
    possuidor = models.ForeignKey(ContribuinteMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='imoveis_posse')
    logradouro = models.CharField(max_length=180)
    numero_endereco = models.CharField(max_length=24, blank=True)
    complemento = models.CharField(max_length=100, blank=True)
    bairro = models.CharField(max_length=100, blank=True)
    cep = models.CharField(max_length=9, blank=True)
    area_terreno = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    area_edificada = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    utilizacao = models.CharField(max_length=14, choices=Uso.choices, default=Uso.OUTRO)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    situacao = models.CharField(max_length=8, choices=Situacao.choices, default=Situacao.ATIVO)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['inscricao']
        constraints = [models.UniqueConstraint(fields=['organization', 'inscricao'], name='rhmun_imovel_inscricao_org')]

    def clean(self):
        super().clean()
        for field in ('proprietario', 'possuidor'):
            item = getattr(self, field, None)
            if item and item.organization_id != self.organization_id:
                raise ValidationError({field: 'O contribuinte pertence a outra Prefeitura.'})
        if (self.latitude is None) != (self.longitude is None):
            raise ValidationError('Latitude e longitude devem ser informadas juntas.')
        if self.latitude is not None and not (Decimal('-90') <= self.latitude <= Decimal('90')):
            raise ValidationError({'latitude': 'Coordenada fora do intervalo válido.'})
        if self.longitude is not None and not (Decimal('-180') <= self.longitude <= Decimal('180')):
            raise ValidationError({'longitude': 'Coordenada fora do intervalo válido.'})


class RegraTributariaMunicipal(PhaseFRecord):
    class Tributo(models.TextChoices):
        IPTU = 'IPTU', 'IPTU'
        ISS = 'ISS', 'ISS'
        ITBI = 'ITBI', 'ITBI'
        ALVARA = 'ALVARA', 'Alvará e Licença'
        TAXA = 'TAXA', 'Taxa municipal'
        CONTRIB = 'CONTRIB', 'Contribuição / CIP'
    codigo = models.CharField(max_length=40)
    tributo = models.CharField(max_length=8, choices=Tributo.choices)
    versao = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    vigencia_inicio = models.DateField()
    vigencia_fim = models.DateField(null=True, blank=True)
    parametros = models.JSONField(default=dict)
    ativa = models.BooleanField(default=False)
    registrada_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    registrada_em = models.DateTimeField(null=True, blank=True)
    criada_em = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['tributo', 'codigo', '-versao']
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo', 'versao'], name='rhmun_regra_tributo_versao_org')]

    def clean(self):
        super().clean()
        if self.vigencia_fim and self.vigencia_fim < self.vigencia_inicio:
            raise ValidationError({'vigencia_fim': 'A vigência final deve ser posterior ao início.'})
        if not isinstance(self.parametros, dict):
            raise ValidationError({'parametros': 'Parâmetros devem ser um objeto JSON.'})
        rate = self.parametros.get('aliquota_percentual')
        if rate is not None:
            try:
                parsed = Decimal(str(rate))
            except Exception as exc:
                raise ValidationError({'parametros': 'Alíquota parametrizada inválida.'}) from exc
            if parsed < 0 or parsed > 100:
                raise ValidationError({'parametros': 'Alíquota deve estar entre 0 e 100.'})


class LancamentoTributarioMunicipal(PhaseFRecord):
    class Status(models.TextChoices):
        ABERTO = 'ABERTO', 'Em aberto'
        PARCIAL = 'PARCIAL', 'Parcialmente pago'
        PAGO = 'PAGO', 'Pago'
        CANCELADO = 'CANCELADO', 'Cancelado'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    numero = models.CharField(max_length=40)
    tributo = models.CharField(max_length=8, choices=RegraTributariaMunicipal.Tributo.choices)
    exercicio = models.PositiveSmallIntegerField()
    competencia = models.CharField(max_length=7, blank=True)
    contribuinte = models.ForeignKey(ContribuinteMunicipal, on_delete=models.PROTECT, related_name='lancamentos_tributarios')
    imovel = models.ForeignKey(ImovelMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='lancamentos_tributarios')
    regra = models.ForeignKey(RegraTributariaMunicipal, on_delete=models.PROTECT, related_name='lancamentos')
    regra_snapshot = models.JSONField(default=dict)
    base_calculo = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(0)])
    aliquota_percentual = models.DecimalField(max_digits=7, decimal_places=4, validators=[MinValueValidator(0)])
    retencao = models.DecimalField(max_digits=15, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    valor_total = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(0)])
    vencimento = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ABERTO)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['-exercicio', '-pk']
        constraints = [models.UniqueConstraint(fields=['organization', 'numero'], name='rhmun_lanc_tributo_num_org')]

    def clean(self):
        super().clean()
        for field in ('contribuinte', 'imovel', 'regra'):
            item = getattr(self, field, None)
            if item and item.organization_id != self.organization_id:
                raise ValidationError({field: 'O registro pertence a outra Prefeitura.'})
        if self.imovel_id and self.imovel.proprietario_id != self.contribuinte_id and self.imovel.possuidor_id != self.contribuinte_id:
            raise ValidationError({'contribuinte': 'O contribuinte não é proprietário nem possuidor do imóvel.'})
        if self.regra_id and self.tributo != self.regra.tributo:
            raise ValidationError({'regra': 'A regra pertence a outro tipo de tributo.'})


class ParcelaTributariaMunicipal(PhaseFRecord):
    lancamento = models.ForeignKey(LancamentoTributarioMunicipal, on_delete=models.PROTECT, related_name='parcelas')
    numero = models.PositiveSmallIntegerField()
    vencimento = models.DateField()
    valor = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(0)])
    criada_em = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['lancamento_id', 'numero']
        constraints = [models.UniqueConstraint(fields=['organization', 'lancamento', 'numero'], name='rhmun_parcela_tributo_num_org')]

    def clean(self):
        super().clean()
        if self.lancamento_id and self.lancamento.organization_id != self.organization_id:
            raise ValidationError({'lancamento': 'O lançamento pertence a outra Prefeitura.'})


class PagamentoArrecadacaoMunicipal(PhaseFRecord):
    class Status(models.TextChoices):
        REGISTRADO = 'REGISTRADO', 'Registrado manualmente'
        ESTORNADO = 'ESTORNADO', 'Estornado'
    request_id = models.UUIDField(default=uuid.uuid4)
    parcela = models.ForeignKey(ParcelaTributariaMunicipal, on_delete=models.PROTECT, related_name='pagamentos')
    valor = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    recebido_em = models.DateTimeField(default=timezone.now)
    canal = models.CharField(max_length=30, default='MANUAL')
    referencia_externa = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REGISTRADO)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'request_id'], name='rhmun_pgto_arrec_req_org')]

    def clean(self):
        super().clean()
        if self.parcela_id and self.parcela.organization_id != self.organization_id:
            raise ValidationError({'parcela': 'A parcela pertence a outra Prefeitura.'})


class InscricaoDividaAtivaMunicipal(PhaseFRecord):
    class Status(models.TextChoices):
        INSCRITA = 'INSCRITA', 'Inscrita'
        PARCELADA = 'PARCELADA', 'Parcelada'
        PAGA = 'PAGA', 'Paga'
        CANCELADA = 'CANCELADA', 'Cancelada'
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    numero = models.CharField(max_length=40)
    lancamento = models.ForeignKey(LancamentoTributarioMunicipal, on_delete=models.PROTECT, related_name='divida_ativa')
    contribuinte = models.ForeignKey(ContribuinteMunicipal, on_delete=models.PROTECT, related_name='inscricoes_divida')
    valor_inscrito = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(0)])
    inscrita_em = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INSCRITA)
    motivo_cancelamento = models.TextField(blank=True)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'numero'], name='rhmun_divida_numero_org')]

    def clean(self):
        super().clean()
        if self.lancamento_id and self.lancamento.organization_id != self.organization_id:
            raise ValidationError({'lancamento': 'O lançamento pertence a outra Prefeitura.'})
        if self.contribuinte_id and self.contribuinte.organization_id != self.organization_id:
            raise ValidationError({'contribuinte': 'O contribuinte pertence a outra Prefeitura.'})
        if self.status == self.Status.CANCELADA and not self.motivo_cancelamento.strip():
            raise ValidationError({'motivo_cancelamento': 'Informe o motivo da baixa/cancelamento.'})


class NotaServicoMunicipal(PhaseFRecord):
    """Draft for a future official NFS-e integration; never transmitted by this phase."""
    class Status(models.TextChoices):
        RASCUNHO = 'RASCUNHO', 'Rascunho'
        AGUARDANDO_CONFIGURACAO = 'AGUARDANDO_CONFIGURACAO', 'Aguardando integração oficial'
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    numero_interno = models.CharField(max_length=40)
    prestador = models.ForeignKey(ContribuinteMunicipal, on_delete=models.PROTECT, related_name='nfse_prestadas')
    tomador = models.ForeignKey(ContribuinteMunicipal, on_delete=models.PROTECT, related_name='nfse_tomadas')
    codigo_servico = models.CharField(max_length=30)
    descricao = models.TextField()
    competencia = models.CharField(max_length=7)
    valor_servico = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(0)])
    parametros_snapshot = models.JSONField(default=dict)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.RASCUNHO)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'numero_interno'], name='rhmun_nfse_num_interno_org')]

    def clean(self):
        super().clean()
        for field in ('prestador', 'tomador'):
            party = getattr(self, field, None)
            if party and party.organization_id != self.organization_id:
                raise ValidationError({field: 'O contribuinte pertence a outra Prefeitura.'})


class AuditoriaTributariaMunicipal(PhaseFRecord):
    acao = models.CharField(max_length=50)
    entidade = models.CharField(max_length=80)
    entidade_id = models.PositiveBigIntegerField(null=True, blank=True)
    alteracoes = models.JSONField(default=dict, blank=True)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['-criado_em', '-pk']


class GuiaArrecadacaoDAM(PhaseFRecord):
    """Documento de Arrecadação Municipal (DAM) com código de barras FEBRABAN Segmento 8 e Pix."""
    class Status(models.TextChoices):
        EMITIDA = 'EMITIDA', 'Emitida / Aberta'
        PAGA = 'PAGA', 'Paga / Baixada'
        CANCELADA = 'CANCELADA', 'Cancelada'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    numero_dam = models.CharField(max_length=40)
    contribuinte = models.ForeignKey(ContribuinteMunicipal, on_delete=models.PROTECT, related_name='guias_dam')
    lancamento = models.ForeignKey(LancamentoTributarioMunicipal, on_delete=models.PROTECT, related_name='guias_dam')
    parcela = models.ForeignKey(ParcelaTributariaMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='guias_dam')
    vencimento = models.DateField()
    valor_principal = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(0)])
    valor_acrescimos = models.DecimalField(max_digits=15, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    valor_desconto = models.DecimalField(max_digits=15, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    valor_total = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(0)])
    codigo_barras = models.CharField(max_length=48)
    linha_digitavel = models.CharField(max_length=60)
    pix_copia_cola = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.EMITIDA)
    emitida_em = models.DateTimeField(default=timezone.now, editable=False)
    emitida_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ['-emitida_em', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'numero_dam'], name='rhmun_dam_num_org'),
        ]


class CertidaoTributariaMunicipal(PhaseFRecord):
    """Certidão Negativa (CND), Positiva com Efeito de Negativa (CPEN) ou Positiva Municipal."""
    class Tipo(models.TextChoices):
        CND = 'CND', 'Certidão Negativa de Débitos (CND)'
        CPEN = 'CPEN', 'Certidão Positiva com Efeitos de Negativa (CPEN)'
        POSITIVA = 'POSITIVA', 'Certidão Positiva de Débitos'

    class Status(models.TextChoices):
        VALIDA = 'VALIDA', 'Válida'
        REVOGADA = 'REVOGADA', 'Revogada'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    numero = models.CharField(max_length=40)
    tipo = models.CharField(max_length=12, choices=Tipo.choices)
    contribuinte = models.ForeignKey(ContribuinteMunicipal, on_delete=models.PROTECT, related_name='certidoes')
    imovel = models.ForeignKey(ImovelMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='certidoes')
    codigo_autenticacao = models.CharField(max_length=32)
    finalidade = models.CharField(max_length=140, blank=True)
    situacao_snapshot = models.JSONField(default=dict, blank=True)
    emitida_em = models.DateTimeField(default=timezone.now, editable=False)
    valida_ate = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.VALIDA)
    emitida_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ['-emitida_em', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'numero'], name='rhmun_certidao_num_org'),
            models.UniqueConstraint(fields=['organization', 'codigo_autenticacao'], name='rhmun_certidao_auth_org'),
        ]


class SolicitacaoITBIMunicipal(PhaseFRecord):
    """Avaliação, cálculo parametrizado e guia de ITBI para transmissão imobiliária."""
    class Status(models.TextChoices):
        AVALIADO = 'AVALIADO', 'Avaliado / Guia Emitida'
        QUITADO = 'QUITADO', 'Quitado / Liberado p/ Cartório'
        CANCELADO = 'CANCELADO', 'Cancelado'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    numero = models.CharField(max_length=40)
    imovel = models.ForeignKey(ImovelMunicipal, on_delete=models.PROTECT, related_name='solicitacoes_itbi')
    transmitente = models.ForeignKey(ContribuinteMunicipal, on_delete=models.PROTECT, related_name='itbi_transmitidos')
    adquirente = models.ForeignKey(ContribuinteMunicipal, on_delete=models.PROTECT, related_name='itbi_adquiridos')
    valor_venal = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(0)])
    valor_transacao = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(0)])
    regra = models.ForeignKey(RegraTributariaMunicipal, on_delete=models.PROTECT, related_name='avaliacoes_itbi')
    valor_itbi = models.DecimalField(max_digits=15, decimal_places=2, validators=[MinValueValidator(0)])
    cartorio_matricula = models.CharField(max_length=100, blank=True)
    lancamento = models.OneToOneField(LancamentoTributarioMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='itbi_origem')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AVALIADO)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['-criado_em', '-pk']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'numero'], name='rhmun_itbi_num_org'),
        ]
