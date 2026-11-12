"""Fiscal foundation in the existing tenant model graph; no secret material."""
import re
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from gestao.models import Validado, RegistroImutavel, SemExclusao
from platform_core.tenancy import TenantManager

TIPOS = [('NFSE', 'NFS-e'), ('NFE', 'NF-e'), ('NFCE', 'NFC-e')]
AMBIENTES = [('SIMULACAO', 'Simulação sem valor fiscal'), ('HOMOLOGACAO', 'Homologação oficial (não habilitada)'), ('PRODUCAO', 'Produção (não habilitada)')]

def dados_publicos(value):
    """Only a small declared schema; reject arbitrary payloads/secrets recursively."""
    allowed = {'codigo', 'descricao', 'regime', 'ncm', 'cfop', 'cst_csosn', 'codigo_servico', 'aliquota', 'retencao', 'vigencia', 'classificacao'}
    if not isinstance(value, dict) or set(value)-allowed:
        raise ValidationError('Use somente parâmetros públicos declarados; segredos não são aceitos.')
    for key, item in value.items():
        if not isinstance(item, (str, int, bool, type(None))) or len(str(item)) > 200:
            raise ValidationError('Parâmetro fiscal público inválido.')
        if isinstance(item, str) and re.search(r'PRIVATE KEY|BEGIN CERTIFICATE|password\s*=|token\s*=|senha\s*=|secret\s*=', item, re.I):
            raise ValidationError('Material sensível não é permitido.')

def endereco_publico(value):
    if not isinstance(value, dict) or set(value)-{'logradouro', 'numero', 'complemento', 'bairro', 'cep', 'pais'} or any(not isinstance(v, str) or len(v) > 200 for v in value.values()):
        raise ValidationError('Endereço estruturado inválido.')

def proposta_documental_publica(value):
    """Pending registration only: declared public fields, never tax rules/secrets."""
    if value == {}:
        return
    fields = {'cnpj', 'razao_social', 'nome_fantasia', 'ie', 'im', 'logradouro',
              'numero', 'complemento', 'bairro', 'cep', 'municipio', 'uf',
              'cnae_principal', 'natureza_juridica', 'porte', 'simples_nacional'}
    if not isinstance(value, dict) or set(value) != {'dados', 'fontes', 'divergencias', 'cadastro_anterior'}:
        raise ValidationError('Proposta documental deve conter somente o esquema público declarado.')
    data = value['dados']
    if not isinstance(data, dict) or set(data) != fields or type(data['simples_nacional']) is not bool:
        raise ValidationError('Dados documentais públicos incompletos ou não permitidos.')
    strings = []
    for key, item in data.items():
        if key != 'simples_nacional':
            if not isinstance(item, str) or not item or len(item) > 200:
                raise ValidationError('Dado documental inválido.')
            strings.append(item)
    if not isinstance(value['fontes'], list) or not 1 <= len(value['fontes']) <= 10:
        raise ValidationError('Informe as fontes documentais públicas.')
    for item in value['fontes']:
        if not isinstance(item, str) or not item or len(item) > 200:
            raise ValidationError('Fonte documental inválida.')
        strings.append(item)
    if not isinstance(value['divergencias'], list) or len(value['divergencias']) > 20:
        raise ValidationError('Divergências inválidas.')
    for item in value['divergencias']:
        if not isinstance(item, dict) or set(item) != {'campo', 'anterior', 'proposta'} or item['campo'] not in fields:
            raise ValidationError('Divergência fora do cadastro documental.')
        for text in item.values():
            if not isinstance(text, str) or len(text) > 200:
                raise ValidationError('Divergência inválida.')
            strings.append(text)
        if item['proposta'] != str(data[item['campo']]):
            raise ValidationError('Divergência não corresponde à proposta documental.')
    previous = value['cadastro_anterior']
    if not isinstance(previous, dict) or set(previous) - {'razao_social', 'nome_fantasia', 'cnpj', 'ie', 'im'}:
        raise ValidationError('Cadastro anterior fora do esquema público.')
    for item in previous.values():
        if not isinstance(item, str) or len(item) > 200:
            raise ValidationError('Cadastro anterior inválido.')
        strings.append(item)
    if any(re.search(r'PRIVATE KEY|BEGIN CERTIFICATE|password\s*=|token\s*=|senha\s*=|secret\s*=', text, re.I) for text in strings):
        raise ValidationError('Material sensível não é permitido na proposta.')

PREPARACAO_TEXTOS = {'logradouro', 'numero', 'complemento', 'bairro', 'cep', 'municipio', 'uf', 'municipio_ibge', 'cnae', 'tipo_estabelecimento', 'regime', 'crt', 'enquadramento', 'natureza_operacao', 'qr_url', 'consulta_url', 'fonte_urls', 'schema_release'}
PREPARACAO_BOOLEANOS = {'dados_confirmados', 'endereco_confirmado', 'municipio_confirmado', 'regime_confirmado', 'divergencias_confirmadas', 'credenciamento_confirmado'}
PREPARACAO_INTEIROS = {'serie', 'proximo_numero', 'produto_id', 'revisao'}


def preparacao_fiscal_publica(value):
    """Public preparation only: no credentials, certificate or issuance switches."""
    if not isinstance(value, dict) or set(value) - (PREPARACAO_TEXTOS | PREPARACAO_BOOLEANOS | PREPARACAO_INTEIROS):
        raise ValidationError('Somente campos públicos da preparação fiscal são aceitos.')
    for key, item in value.items():
        if key in PREPARACAO_TEXTOS:
            if not isinstance(item, str) or len(item) > 250 or re.search(r'PRIVATE KEY|CERTIFICATE|senha|password|token|secret|cofre://', item, re.I):
                raise ValidationError('Campo público inválido ou material sensível não permitido.')
        elif key in PREPARACAO_BOOLEANOS and type(item) is not bool:
            raise ValidationError('Confirmação fiscal inválida.')
        elif key in PREPARACAO_INTEIROS and (item is not None and (type(item) is not int or item < 0)):
            raise ValidationError('Identificador ou número fiscal inválido.')


class CadastroFiscal(Validado):
    """Versioned configuration: no silent edits/deletes through ordinary saves."""
    objects = TenantManager.from_queryset(SemExclusao)()
    class Meta:
        abstract = True
    def save(self, *args, **kwargs):
        if self.pk and not getattr(self, '_service_write', False):
            raise ValidationError('Alteração fiscal exige serviço auditado ou nova versão.')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError('Cadastro fiscal deve ser preservado no histórico.')

class PerfilFiscalEstabelecimento(CadastroFiscal):
    estabelecimento = models.OneToOneField('platform_core.FilialEmpresa', on_delete=models.PROTECT, related_name='perfil_fiscal')
    municipio_ibge = models.CharField(max_length=7)
    uf = models.CharField(max_length=2)
    endereco_estruturado = models.JSONField(default=dict, blank=True, validators=[endereco_publico])
    regime = models.CharField(max_length=40, blank=True)
    atividade = models.CharField(max_length=100, blank=True)
    def clean(self):
        super().clean()
        if not re.fullmatch(r'\d{7}', self.municipio_ibge) or not re.fullmatch(r'[A-Z]{2}', self.uf):
            raise ValidationError('Informe município IBGE e UF válidos.')
    def __str__(self):
        return str(self.estabelecimento)

class CredencialFiscal(CadastroFiscal):
    referencia = models.CharField(max_length=80, unique=True)
    mecanismo = models.CharField(max_length=20, choices=[('REFERENCIA', 'Referência de cofre (sem material)'), ('SIMULACAO', 'Sem certificado — simulação')])
    ambiente = models.CharField(max_length=12, choices=AMBIENTES, default='SIMULACAO')
    versao = models.PositiveIntegerField(default=1)
    ativa = models.BooleanField(default=True)
    expira_em = models.DateTimeField(null=True, blank=True)
    def clean(self):
        super().clean()
        prefix = 'simulacao' if self.mecanismo == 'SIMULACAO' else 'cofre'
        if not re.fullmatch(prefix+r'://[0-9a-f-]{36}', self.referencia):
            raise ValidationError('Informe apenas referência opaca; nunca certificado, senha ou token.')
        try:
            uuid.UUID(self.referencia.split('://')[1])
        except (ValueError, IndexError):
            raise ValidationError('Referência opaca inválida.')
        if self.mecanismo == 'SIMULACAO' and self.ambiente != 'SIMULACAO':
            raise ValidationError('Referência simulada não habilita ambiente oficial.')
    def __str__(self):
        return f'Referência fiscal #{self.pk} · versão {self.versao}'

class ConfiguracaoFiscal(CadastroFiscal):
    estabelecimento = models.ForeignKey('platform_core.FilialEmpresa', on_delete=models.PROTECT)
    tipo = models.CharField(max_length=5, choices=TIPOS)
    ambiente = models.CharField(max_length=12, choices=AMBIENTES, default='SIMULACAO')
    versao = models.PositiveIntegerField(default=1)
    motor = models.CharField(
        max_length=25,
        choices=[
            ('SIMULADOR', 'Simulador local sem emissão'),
            ('NFCE_SVRS_HOM', 'NFC-e SVRS — somente homologação'),
            ('NFSE_ADN_HOM', 'NFS-e ADN Nacional — homologação'),
            ('NFSE_RPS_CONTINGENCIA', 'NFS-e Contingência RPS Offline'),
            ('NFE_SEFAZ_HOM', 'NF-e SEFAZ v4.00 — homologação'),
        ],
        default='SIMULADOR',
    )
    habilitada = models.BooleanField(default=False)
    proposta_documental = models.JSONField(default=dict, blank=True, validators=[proposta_documental_publica])
    preparacao_fiscal = models.JSONField(default=dict, blank=True, validators=[preparacao_fiscal_publica])
    configuracao_confirmada = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='origens_documentais')
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'estabelecimento', 'tipo', 'ambiente', 'versao'], name='fiscal_config_versao_unica')]
    def __str__(self):
        return f'{self.get_tipo_display()} · {self.estabelecimento} · {self.get_ambiente_display()} v{self.versao}'
    def clean(self):
        super().clean()
        if self.preparacao_fiscal and (self.habilitada or self.ambiente != 'HOMOLOGACAO' or self.tipo != 'NFCE'):
            raise ValidationError('Preparação fiscal não habilita emissão; produção bloqueada nesta etapa.')
        if self.habilitada and self.estabelecimento_id and ConfiguracaoFiscal.objects.filter(estabelecimento_id=self.estabelecimento_id).exclude(proposta_documental={}, preparacao_fiscal={}).exists():
            raise ValidationError('Estabelecimento em preparação documental; habilitação prematura bloqueada.')
        if self.configuracao_confirmada_id:
            other = self.configuracao_confirmada
            if other.organization_id != self.organization_id or other.pk == self.pk or other.estabelecimento.cnpj != self.estabelecimento.cnpj:
                raise ValidationError('Confirmação deve pertencer ao mesmo tenant e emitente.')
        if self.proposta_documental and (self.habilitada or self.ambiente != 'HOMOLOGACAO' or self.tipo != 'NFCE'):
            raise ValidationError('Proposta documental pendente não pode habilitar emissão nem outro ambiente.')
        if self.motor == 'NFCE_SVRS_HOM' and (self.tipo != 'NFCE' or self.ambiente != 'HOMOLOGACAO'):
            raise ValidationError('Motor SVRS restrito à NFC-e em homologação.')
        if self.motor == 'NFSE_ADN_HOM' and (self.tipo != 'NFSE' or self.ambiente != 'HOMOLOGACAO'):
            raise ValidationError('Motor ADN restrito à NFS-e em homologação.')
        if self.motor == 'NFE_SEFAZ_HOM' and (self.tipo != 'NFE' or self.ambiente != 'HOMOLOGACAO'):
            raise ValidationError('Motor NF-e SEFAZ restrito à NF-e em homologação.')
        if self.motor == 'NFSE_RPS_CONTINGENCIA' and (self.tipo != 'NFSE' or self.ambiente == 'PRODUCAO'):
            raise ValidationError('Contingência RPS é permitida apenas para NFS-e em simulação ou homologação.')

class VinculoCredencialFiscal(CadastroFiscal):
    configuracao = models.ForeignKey(ConfiguracaoFiscal, on_delete=models.PROTECT, related_name='vinculos')
    credencial = models.ForeignKey(CredencialFiscal, on_delete=models.PROTECT)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['configuracao', 'credencial'], name='fiscal_vinculo_unico')]
    def clean(self):
        super().clean()
        if self.configuracao_id and self.credencial_id and self.configuracao.ambiente != self.credencial.ambiente:
            raise ValidationError('Credencial e configuração devem usar o mesmo ambiente.')

class SerieFiscal(CadastroFiscal):
    estabelecimento = models.ForeignKey('platform_core.FilialEmpresa', on_delete=models.PROTECT)
    tipo = models.CharField(max_length=5, choices=TIPOS)
    ambiente = models.CharField(max_length=12, choices=AMBIENTES, default='SIMULACAO')
    serie = models.CharField(max_length=10)
    proximo_numero = models.PositiveBigIntegerField(default=1, editable=False)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'estabelecimento', 'tipo', 'ambiente', 'serie'], name='fiscal_serie_contexto_unico'), models.CheckConstraint(condition=models.Q(proximo_numero__gte=1), name='fiscal_numero_positivo')]
    def __str__(self):
        return f'{self.tipo} · {self.serie} · {self.ambiente}'
    def clean(self):
        super().clean()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,10}', self.serie):
            raise ValidationError('Série inválida.')

class RegraTributariaFiscal(CadastroFiscal):
    configuracao = models.ForeignKey(ConfiguracaoFiscal, on_delete=models.PROTECT)
    versao = models.PositiveIntegerField(default=1)
    inicio = models.DateField()
    fim = models.DateField(null=True, blank=True)
    parametros = models.JSONField(default=dict, blank=True, validators=[dados_publicos])
    class Meta:
        constraints = [models.UniqueConstraint(fields=['configuracao', 'versao'], name='fiscal_regra_versao_unica')]
    def clean(self):
        super().clean()
        if self.fim and self.fim < self.inicio:
            raise ValidationError('Fim da vigência anterior ao início.')

class DetalheFiscal(Validado):
    documento = models.OneToOneField('operacao.DocumentoFiscal', on_delete=models.PROTECT)
    class Meta:
        abstract = True
    def clean(self):
        super().clean()
        if self.documento_id and self.documento.tipo != self.tipo_fiscal:
            raise ValidationError('Detalhes incompatíveis com o tipo de documento.')
    def save(self, *args, **kwargs):
        if not getattr(self, '_service_write', False):
            raise ValidationError('Detalhes exigem serviço fiscal.')
        return super().save(*args, **kwargs)

class DetalhesNFSe(DetalheFiscal):
    tipo_fiscal = 'NFSE'
    competencia = models.DateField()
    codigo_servico = models.CharField(max_length=30, blank=True)
    municipio_incidencia = models.CharField(max_length=7)
    identificador_dps_rps = models.CharField(max_length=80, blank=True)
    modo_emissao = models.CharField(
        max_length=20,
        choices=[('NORMAL', 'Normal ADN'), ('CONTINGENCIA_RPS', 'Contingência offline (RPS/DPS)')],
        default='NORMAL',
    )
    motivo_contingencia = models.CharField(max_length=200, blank=True)
    numero_rps_contingencia = models.CharField(max_length=40, blank=True)
    sincronizada_em = models.DateTimeField(null=True, blank=True)
    tentativas_sincronizacao = models.PositiveIntegerField(default=0)
    ultimo_erro_sincronizacao = models.CharField(max_length=250, blank=True)
    nota_json = models.JSONField(default=dict, blank=True)

class DetalhesNFe(DetalheFiscal):
    tipo_fiscal = 'NFE'
    finalidade = models.CharField(max_length=20, default='NORMAL')
    itens_snapshot = models.JSONField(default=list, editable=False)
    modo_emissao = models.CharField(
        max_length=15,
        choices=[('NORMAL', 'Normal'), ('SVC_AN', 'Contingência SVC-AN'), ('SVC_RS', 'Contingência SVC-RS'), ('FS_DA', 'Contingência FS-DA'), ('EPEC', 'Contingência EPEC')],
        default='NORMAL',
    )
    motivo_contingencia = models.CharField(max_length=200, blank=True)
    nota_json = models.JSONField(default=dict, blank=True)

class DetalhesNFCe(DetalheFiscal):
    tipo_fiscal = 'NFCE'
    pagamentos_snapshot = models.JSONField(default=list, editable=False)
    modo_emissao = models.CharField(
        max_length=25,
        choices=[('NORMAL', 'Normal; sem contingência fiscal'), ('CONTINGENCIA_OFFLINE', 'Contingência Offline PDV (tpEmis=9)')],
        default='NORMAL',
    )
    contingencia_justificativa = models.CharField(max_length=200, blank=True)
    contingencia_ativada_em = models.DateTimeField(null=True, blank=True)
    nota_json = models.JSONField(default=dict, blank=True)

class SolicitacaoFiscal(Validado):
    objects = TenantManager.from_queryset(SemExclusao)()
    documento = models.ForeignKey('operacao.DocumentoFiscal', on_delete=models.PROTECT, related_name='solicitacoes_fiscais')
    request_id = models.UUIDField()
    operacao = models.CharField(max_length=10, choices=[('EMITIR', 'Emitir em simulação'), ('CANCELAR', 'Cancelar em simulação')])
    solicitante = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    payload_hash = models.CharField(max_length=64, editable=False)
    estado = models.CharField(max_length=25, default='PENDENTE', editable=False)
    motivo = models.CharField(max_length=500, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    resultado_simulado = models.JSONField(default=dict, blank=True, editable=False)
    cenario_simulacao = models.CharField(max_length=15, choices=[('AUTORIZAR', 'Autorizar em simulação'), ('REJEITAR', 'Rejeitar em simulação'), ('TIMEOUT_ANTES', 'Timeout antes da resposta'), ('TIMEOUT_APOS', 'Timeout após resposta')], default='AUTORIZAR')
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_ate = models.DateTimeField(null=True, blank=True, editable=False)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'request_id'], name='fiscal_solicitacao_idempotente'), models.UniqueConstraint(fields=['documento', 'operacao'], name='fiscal_solicitacao_operacao_unica')]
    def save(self, *args, **kwargs):
        if not getattr(self, '_service_write', False):
            raise ValidationError('Solicitação exige serviço fiscal auditado.')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError('Solicitação fiscal não pode ser apagada.')

class ReservaNumeracaoFiscal(RegistroImutavel):
    serie = models.ForeignKey(SerieFiscal, on_delete=models.PROTECT)
    documento = models.OneToOneField('operacao.DocumentoFiscal', on_delete=models.PROTECT)
    numero = models.PositiveBigIntegerField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=['serie', 'numero'], name='fiscal_numero_reservado_unico')]
    def clean(self):
        super().clean()
        if self.documento.serie_fiscal_id != self.serie_id:
            raise ValidationError('Reserva deve usar a série do documento.')

class TentativaFiscal(RegistroImutavel):
    solicitacao = models.ForeignKey(SolicitacaoFiscal, on_delete=models.PROTECT, related_name='tentativas')
    resultado = models.CharField(max_length=25)
    criado_em = models.DateTimeField(auto_now_add=True)
    # Intentionally no exception text, request body, certificate or token field.

class EventoFiscal(RegistroImutavel):
    documento = models.ForeignKey('operacao.DocumentoFiscal', on_delete=models.PROTECT, related_name='eventos_fiscais')
    solicitacao = models.ForeignKey(SolicitacaoFiscal, on_delete=models.PROTECT, null=True, blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    evento = models.CharField(max_length=30)
    anterior = models.CharField(max_length=25, blank=True)
    posterior = models.CharField(max_length=25, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['pk']
    def clean(self):
        super().clean()
        if self.solicitacao_id and self.solicitacao.documento_id != self.documento_id:
            raise ValidationError('Evento deve coincidir com o documento da solicitação.')
