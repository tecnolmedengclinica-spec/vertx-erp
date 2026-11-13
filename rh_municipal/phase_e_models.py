"""Phase E: tenant-isolated protocol, digital processes and public information.

Public transparency is an explicit, curated snapshot. These models never expose
internal source tables directly, and uploaded process documents use private
storage with immutable version records.
"""
import hashlib
import uuid
from pathlib import PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from ordens.storage import private_storage
from .models import MunicipalRecord, RegistroMunicipalImutavel


PHASE_E_MODULES = {
    'processos': 'processo_digital',
    'transparencia': 'transparencia',
    'ouvidoria': 'ouvidoria',
    'esic': 'esic',
}


def _validate_links(record):
    if not record.organization_id:
        return
    for field in record._meta.fields:
        if not field.is_relation or not (field.many_to_one or field.one_to_one):
            continue
        if field.name == 'organization' or not getattr(record, field.attname, None):
            continue
        related = getattr(record, field.name)
        related_org = getattr(related, 'organization_id', None)
        if related_org is not None and related_org != record.organization_id:
            raise ValidationError({field.name: 'O registro relacionado pertence a outra Prefeitura.'})
        if hasattr(related, 'memberships') and not related.memberships.filter(
            organization_id=record.organization_id, status='ATIVO',
        ).exists():
            raise ValidationError({field.name: 'O usuário precisa ter membership ativo nesta Prefeitura.'})


class PhaseERecord(MunicipalRecord):
    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        _validate_links(self)
        if getattr(self, 'setor_id', None) and getattr(self, 'departamento_id', None) and self.setor.departamento_id != self.departamento_id:
            raise ValidationError({'setor': 'O setor não pertence ao departamento selecionado.'})
        if getattr(self, 'departamento_id', None) and getattr(self, 'secretaria_id', None) and self.departamento.secretaria_id != self.secretaria_id:
            raise ValidationError({'departamento': 'O departamento não pertence à secretaria selecionada.'})
        if getattr(self, 'unidade_id', None) and getattr(self, 'setor_id', None) and self.unidade.setor_id != self.setor_id:
            raise ValidationError({'unidade': 'A unidade não pertence ao setor selecionado.'})
        if getattr(self, 'unidade_id', None) and getattr(self, 'secretaria_id', None) and self.unidade.setor.departamento.secretaria_id != self.secretaria_id:
            raise ValidationError({'unidade': 'A unidade não pertence à secretaria selecionada.'})


class PhaseEImmutable(RegistroMunicipalImutavel):
    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        _validate_links(self)


class ContadorExpedienteMunicipal(PhaseERecord):
    class Familia(models.TextChoices):
        PROTOCOLO = 'PROTOCOLO', 'Protocolo'
        PROCESSO = 'PROCESSO', 'Processo digital'
        OUVIDORIA = 'OUVIDORIA', 'Ouvidoria'
        ESIC = 'ESIC', 'E-SIC'

    familia = models.CharField(max_length=12, choices=Familia.choices)
    exercicio = models.PositiveSmallIntegerField()
    proximo_numero = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'familia', 'exercicio'], name='rhmun_contador_exp_org_fam_ano')]


class ProtocoloMunicipal(PhaseERecord):
    class Origem(models.TextChoices):
        INTERNA = 'INTERNA', 'Interna'
        PRESENCIAL = 'PRESENCIAL', 'Presencial'
        PORTAL = 'PORTAL', 'Portal público'
        EMAIL = 'EMAIL', 'E-mail'
        OUTRA = 'OUTRA', 'Outra origem'

    class Prioridade(models.TextChoices):
        BAIXA = 'BAIXA', 'Baixa'
        NORMAL = 'NORMAL', 'Normal'
        ALTA = 'ALTA', 'Alta'
        URGENTE = 'URGENTE', 'Urgente'

    class Status(models.TextChoices):
        RECEBIDO = 'RECEBIDO', 'Recebido'
        DISTRIBUIDO = 'DISTRIBUIDO', 'Distribuído'
        EM_ANALISE = 'EM_ANALISE', 'Em análise'
        PENDENTE_INTERESSADO = 'PENDENTE_INTERESSADO', 'Pendente do interessado'
        CONCLUIDO = 'CONCLUIDO', 'Concluído'
        ARQUIVADO = 'ARQUIVADO', 'Arquivado'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    numero = models.CharField(max_length=32)
    exercicio = models.PositiveSmallIntegerField()
    origem = models.CharField(max_length=12, choices=Origem.choices, default=Origem.INTERNA)
    interessado_nome = models.CharField(max_length=180, blank=True)
    interessado_documento = models.CharField(max_length=20, blank=True)
    interessado_email = models.EmailField(blank=True)
    interessado_telefone = models.CharField(max_length=30, blank=True)
    assunto = models.CharField(max_length=180)
    resumo = models.TextField()
    prioridade = models.CharField(max_length=10, choices=Prioridade.choices, default=Prioridade.NORMAL)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.RECEBIDO)
    recebido_em = models.DateTimeField(default=timezone.now)
    prazo_em = models.DateField(null=True, blank=True)
    secretaria = models.ForeignKey('rh_municipal.Secretaria', null=True, blank=True, on_delete=models.PROTECT, related_name='protocolos_municipais')
    departamento = models.ForeignKey('rh_municipal.DepartamentoMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='protocolos_municipais')
    setor = models.ForeignKey('rh_municipal.SetorMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='protocolos_municipais')
    unidade = models.ForeignKey('rh_municipal.UnidadeMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='protocolos_municipais')
    responsavel = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='protocolos_municipais_responsavel')
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='protocolos_municipais_criados')
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-recebido_em', '-pk']
        constraints = [models.UniqueConstraint(fields=['organization', 'exercicio', 'numero'], name='rhmun_protocolo_num_org')]


class ProcessoDigitalMunicipal(PhaseERecord):
    class Status(models.TextChoices):
        AUTUADO = 'AUTUADO', 'Autuado'
        DISTRIBUIDO = 'DISTRIBUIDO', 'Distribuído'
        EM_ANALISE = 'EM_ANALISE', 'Em análise'
        AGUARDANDO_PARECER = 'AGUARDANDO_PARECER', 'Aguardando parecer'
        AGUARDANDO_ASSINATURA = 'AGUARDANDO_ASSINATURA', 'Aguardando assinatura'
        PENDENTE_INTERESSADO = 'PENDENTE_INTERESSADO', 'Pendente do interessado'
        CONCLUIDO = 'CONCLUIDO', 'Concluído'
        ARQUIVADO = 'ARQUIVADO', 'Arquivado'

    class Prioridade(models.TextChoices):
        BAIXA = 'BAIXA', 'Baixa'
        NORMAL = 'NORMAL', 'Normal'
        ALTA = 'ALTA', 'Alta'
        URGENTE = 'URGENTE', 'Urgente'

    class Sigilo(models.TextChoices):
        PUBLICO = 'PUBLICO', 'Público após revisão de publicação'
        INTERNO = 'INTERNO', 'Interno'
        RESTRITO = 'RESTRITO', 'Restrito'
        SENSIVEL = 'SENSIVEL', 'Sensível'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    numero = models.CharField(max_length=32)
    exercicio = models.PositiveSmallIntegerField()
    titulo = models.CharField(max_length=200)
    assunto = models.CharField(max_length=180)
    descricao = models.TextField(blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.AUTUADO)
    prioridade = models.CharField(max_length=10, choices=Prioridade.choices, default=Prioridade.NORMAL)
    sigilo = models.CharField(max_length=12, choices=Sigilo.choices, default=Sigilo.INTERNO)
    prazo_em = models.DateField(null=True, blank=True)
    protocolo = models.OneToOneField(ProtocoloMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='processo_digital')
    secretaria = models.ForeignKey('rh_municipal.Secretaria', null=True, blank=True, on_delete=models.PROTECT, related_name='processos_digitais')
    departamento = models.ForeignKey('rh_municipal.DepartamentoMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='processos_digitais')
    setor = models.ForeignKey('rh_municipal.SetorMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='processos_digitais')
    unidade = models.ForeignKey('rh_municipal.UnidadeMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='processos_digitais')
    responsavel = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='processos_digitais_responsavel')
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='processos_digitais_criados')
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)
    concluido_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-criado_em', '-pk']
        constraints = [models.UniqueConstraint(fields=['organization', 'exercicio', 'numero'], name='rhmun_processo_digital_num_org')]


class ParteProcessoMunicipal(PhaseERecord):
    class Tipo(models.TextChoices):
        INTERESSADO = 'INTERESSADO', 'Interessado'
        REPRESENTANTE = 'REPRESENTANTE', 'Representante'
        ORGAO = 'ORGAO', 'Órgão público'
        FORNECEDOR = 'FORNECEDOR', 'Fornecedor'
        OUTRO = 'OUTRO', 'Outro'

    processo = models.ForeignKey(ProcessoDigitalMunicipal, on_delete=models.PROTECT, related_name='partes')
    tipo = models.CharField(max_length=16, choices=Tipo.choices, default=Tipo.INTERESSADO)
    nome = models.CharField(max_length=180)
    documento = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    telefone = models.CharField(max_length=30, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)


class TramiteProcessoMunicipal(PhaseEImmutable):
    class Acao(models.TextChoices):
        AUTUACAO = 'AUTUACAO', 'Autuação'
        DISTRIBUICAO = 'DISTRIBUICAO', 'Distribuição'
        RECEBIMENTO = 'RECEBIMENTO', 'Recebimento'
        DESPACHO = 'DESPACHO', 'Despacho'
        PARECER = 'PARECER', 'Parecer'
        ASSINATURA = 'ASSINATURA', 'Assinatura solicitada'
        CONCLUSAO = 'CONCLUSAO', 'Conclusão'
        ARQUIVAMENTO = 'ARQUIVAMENTO', 'Arquivamento'
        REABERTURA = 'REABERTURA', 'Reabertura'

    processo = models.ForeignKey(ProcessoDigitalMunicipal, on_delete=models.PROTECT, related_name='tramitacoes')
    acao = models.CharField(max_length=16, choices=Acao.choices)
    status_anterior = models.CharField(max_length=24, blank=True)
    status_novo = models.CharField(max_length=24, blank=True)
    secretaria_origem = models.ForeignKey('rh_municipal.Secretaria', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    secretaria_destino = models.ForeignKey('rh_municipal.Secretaria', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    departamento_origem = models.ForeignKey('rh_municipal.DepartamentoMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    departamento_destino = models.ForeignKey('rh_municipal.DepartamentoMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    setor_origem = models.ForeignKey('rh_municipal.SetorMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    setor_destino = models.ForeignKey('rh_municipal.SetorMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    unidade_origem = models.ForeignKey('rh_municipal.UnidadeMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    unidade_destino = models.ForeignKey('rh_municipal.UnidadeMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    responsavel_origem = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    responsavel_destino = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    descricao = models.TextField(blank=True)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    ocorrido_em = models.DateTimeField(default=timezone.now, editable=False)


class DespachoProcessoMunicipal(PhaseEImmutable):
    processo = models.ForeignKey(ProcessoDigitalMunicipal, on_delete=models.PROTECT, related_name='despachos')
    tipo = models.CharField(max_length=24, default='DESPACHO')
    texto = models.TextField()
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    registrado_em = models.DateTimeField(default=timezone.now, editable=False)


class ParecerProcessoMunicipal(PhaseEImmutable):
    class Resultado(models.TextChoices):
        FAVORAVEL = 'FAVORAVEL', 'Favorável'
        DESFAVORAVEL = 'DESFAVORAVEL', 'Desfavorável'
        DILIGENCIA = 'DILIGENCIA', 'Solicita diligência'
        INFORMATIVO = 'INFORMATIVO', 'Informativo'

    processo = models.ForeignKey(ProcessoDigitalMunicipal, on_delete=models.PROTECT, related_name='pareceres')
    resultado = models.CharField(max_length=14, choices=Resultado.choices, default=Resultado.INFORMATIVO)
    texto = models.TextField()
    referencia = models.CharField(max_length=180, blank=True)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    registrado_em = models.DateTimeField(default=timezone.now, editable=False)


class TarefaProcessoMunicipal(PhaseERecord):
    class Status(models.TextChoices):
        ABERTA = 'ABERTA', 'Aberta'
        EM_ANDAMENTO = 'EM_ANDAMENTO', 'Em andamento'
        CONCLUIDA = 'CONCLUIDA', 'Concluída'
        CANCELADA = 'CANCELADA', 'Cancelada'

    processo = models.ForeignKey(ProcessoDigitalMunicipal, on_delete=models.PROTECT, related_name='tarefas')
    titulo = models.CharField(max_length=180)
    descricao = models.TextField(blank=True)
    atribuida_a = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='tarefas_processos_municipais')
    prazo_em = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.ABERTA)
    criada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='tarefas_processos_municipais_criadas')
    criada_em = models.DateTimeField(auto_now_add=True)
    concluida_em = models.DateTimeField(null=True, blank=True)


def processo_document_upload(instance, filename):
    suffix = PurePosixPath(filename).suffix.lower()[:12]
    return f'organizations/{instance.organization.public_id}/gestao-publica/processos/{instance.documento.processo.public_id}/{uuid.uuid4().hex}{suffix}'


class DocumentoProcessoMunicipal(PhaseERecord):
    class Categoria(models.TextChoices):
        ENTRADA = 'ENTRADA', 'Documento recebido'
        DESPACHO = 'DESPACHO', 'Despacho'
        PARECER = 'PARECER', 'Parecer'
        ANEXO = 'ANEXO', 'Anexo'
        FINAL = 'FINAL', 'Documento final'
        OUTRO = 'OUTRO', 'Outro'

    processo = models.ForeignKey(ProcessoDigitalMunicipal, on_delete=models.PROTECT, related_name='documentos')
    titulo = models.CharField(max_length=180)
    categoria = models.CharField(max_length=12, choices=Categoria.choices, default=Categoria.ANEXO)
    atual = models.ForeignKey('rh_municipal.VersaoDocumentoProcessoMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='documentos_processos_municipais_criados')
    criado_em = models.DateTimeField(auto_now_add=True)


class VersaoDocumentoProcessoMunicipal(PhaseEImmutable):
    documento = models.ForeignKey(DocumentoProcessoMunicipal, on_delete=models.PROTECT, related_name='versoes')
    numero_versao = models.PositiveSmallIntegerField()
    arquivo = models.FileField(upload_to=processo_document_upload, storage=private_storage, max_length=500)
    nome_original = models.CharField(max_length=180)
    content_type = models.CharField(max_length=100, blank=True)
    tamanho_bytes = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    sha256 = models.CharField(max_length=64)
    enviado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    enviado_em = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['documento_id', '-numero_versao']
        constraints = [models.UniqueConstraint(fields=['organization', 'documento', 'numero_versao'], name='rhmun_doc_proc_versao_org')]

    def clean(self):
        super().clean()
        if self.documento_id and self.documento.organization_id != self.organization_id:
            raise ValidationError({'documento': 'O documento pertence a outra Prefeitura.'})
        if self.sha256 and (len(self.sha256) != 64 or any(c not in '0123456789abcdef' for c in self.sha256.lower())):
            raise ValidationError({'sha256': 'Hash SHA-256 inválido.'})


class SolicitacaoAssinaturaMunicipal(PhaseERecord):
    class Status(models.TextChoices):
        PENDENTE = 'PENDENTE', 'Pendente'
        CONCLUIDA = 'CONCLUIDA', 'Concluída'
        CANCELADA = 'CANCELADA', 'Cancelada'

    processo = models.ForeignKey(ProcessoDigitalMunicipal, on_delete=models.PROTECT, related_name='solicitacoes_assinatura')
    versao_documento = models.ForeignKey(VersaoDocumentoProcessoMunicipal, on_delete=models.PROTECT, related_name='solicitacoes_assinatura')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDENTE)
    solicitado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    solicitado_em = models.DateTimeField(auto_now_add=True)
    concluido_em = models.DateTimeField(null=True, blank=True)
    observacao = models.TextField(blank=True)

    def clean(self):
        super().clean()
        if self.versao_documento_id and self.processo_id and self.versao_documento.documento.processo_id != self.processo_id:
            raise ValidationError({'versao_documento': 'A versão documental não pertence a este processo.'})


class SignatarioMunicipal(PhaseERecord):
    class Status(models.TextChoices):
        AGUARDANDO = 'AGUARDANDO', 'Aguardando'
        CONFIRMADA = 'CONFIRMADA', 'Confirmação registrada'
        RECUSADA = 'RECUSADA', 'Recusada'

    solicitacao = models.ForeignKey(SolicitacaoAssinaturaMunicipal, on_delete=models.PROTECT, related_name='signatarios')
    membership = models.ForeignKey('platform_core.OrganizationMembership', on_delete=models.PROTECT, related_name='assinaturas_municipais')
    ordem = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.AGUARDANDO)
    confirmado_em = models.DateTimeField(null=True, blank=True)
    hash_confirmacao = models.CharField(max_length=64, blank=True)
    observacao = models.TextField(blank=True)

    class Meta:
        ordering = ['ordem', 'pk']
        constraints = [models.UniqueConstraint(fields=['organization', 'solicitacao', 'membership'], name='rhmun_signatario_solic_mem'), models.UniqueConstraint(fields=['organization', 'solicitacao', 'ordem'], name='rhmun_signatario_solic_ordem')]

    def clean(self):
        super().clean()
        if self.membership_id and self.membership.organization_id != self.organization_id:
            raise ValidationError({'membership': 'O signatário não pertence a esta Prefeitura.'})


class EventoAssinaturaMunicipal(PhaseEImmutable):
    solicitacao = models.ForeignKey(SolicitacaoAssinaturaMunicipal, on_delete=models.PROTECT, related_name='eventos')
    signatario = models.ForeignKey(SignatarioMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='eventos')
    evento = models.CharField(max_length=32)
    hash_documento = models.CharField(max_length=64)
    hash_confirmacao = models.CharField(max_length=64, blank=True)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    registrado_em = models.DateTimeField(default=timezone.now, editable=False)
    observacao = models.TextField(blank=True)


class PublicacaoTransparenciaMunicipal(PhaseERecord):
    """Reviewed public snapshot; not a view over internal business tables."""
    class Categoria(models.TextChoices):
        RECEITAS = 'RECEITAS', 'Receitas'
        DESPESAS = 'DESPESAS', 'Despesas'
        EMPENHOS = 'EMPENHOS', 'Empenhos'
        LIQUIDACOES = 'LIQUIDACOES', 'Liquidações'
        PAGAMENTOS = 'PAGAMENTOS', 'Pagamentos'
        LICITACOES = 'LICITACOES', 'Licitações'
        CONTRATOS = 'CONTRATOS', 'Contratos'
        FORNECEDORES = 'FORNECEDORES', 'Fornecedores'
        SERVIDORES = 'SERVIDORES', 'Servidores'
        PATRIMONIO = 'PATRIMONIO', 'Patrimônio'
        CONVENIOS = 'CONVENIOS', 'Convênios'
        COMPRAS = 'COMPRAS', 'Compras'
        ATAS = 'ATAS', 'Atas'
        OBRAS = 'OBRAS', 'Obras'
        VEICULOS = 'VEICULOS', 'Veículos'
        OUTRO = 'OUTRO', 'Outro conteúdo público'

    class Status(models.TextChoices):
        RASCUNHO = 'RASCUNHO', 'Rascunho'
        EM_REVISAO = 'EM_REVISAO', 'Em revisão'
        PUBLICADO = 'PUBLICADO', 'Publicado'
        RETIRADO = 'RETIRADO', 'Retirado'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    categoria = models.CharField(max_length=16, choices=Categoria.choices)
    titulo = models.CharField(max_length=180)
    resumo = models.CharField(max_length=500, blank=True)
    conteudo_publico = models.JSONField(default=dict, blank=True)
    fonte_referencia = models.CharField(max_length=180, blank=True)
    data_referencia = models.DateField(null=True, blank=True)
    secretaria = models.ForeignKey('rh_municipal.Secretaria', null=True, blank=True, on_delete=models.PROTECT, related_name='publicacoes_transparencia')
    departamento = models.ForeignKey('rh_municipal.DepartamentoMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='publicacoes_transparencia')
    setor = models.ForeignKey('rh_municipal.SetorMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='publicacoes_transparencia')
    unidade = models.ForeignKey('rh_municipal.UnidadeMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='publicacoes_transparencia')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RASCUNHO)
    versao = models.PositiveSmallIntegerField(default=1)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='publicacoes_transparencia_criadas')
    revisado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='publicacoes_transparencia_revisadas')
    publicado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='publicacoes_transparencia_publicadas')
    criado_em = models.DateTimeField(auto_now_add=True)
    revisado_em = models.DateTimeField(null=True, blank=True)
    publicado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-publicado_em', '-pk']

    def clean(self):
        super().clean()
        forbidden_keys = {'cpf', 'cnpj', 'cns', 'email', 'telefone', 'celular', 'endereco_residencial', 'senha', 'token', 'documento_pessoal'}
        def inspect(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    if str(key).strip().lower() in forbidden_keys:
                        raise ValidationError({'conteudo_publico': 'O conjunto contém campo pessoal/restrito e não pode ser publicado.'})
                    inspect(nested)
            elif isinstance(value, list):
                for nested in value:
                    inspect(nested)
        if self.conteudo_publico is not None:
            inspect(self.conteudo_publico)
        if self.status == self.Status.PUBLICADO and (not self.revisado_por_id or not self.publicado_por_id or not self.publicado_em):
            raise ValidationError('Publicação exige revisão e autoria de publicação registradas.')


class ManifestacaoOuvidoriaMunicipal(PhaseERecord):
    class Tipo(models.TextChoices):
        DENUNCIA = 'DENUNCIA', 'Denúncia'
        RECLAMACAO = 'RECLAMACAO', 'Reclamação'
        ELOGIO = 'ELOGIO', 'Elogio'
        SUGESTAO = 'SUGESTAO', 'Sugestão'
        SOLICITACAO = 'SOLICITACAO', 'Solicitação'

    class Status(models.TextChoices):
        RECEBIDA = 'RECEBIDA', 'Recebida'
        CLASSIFICACAO = 'CLASSIFICACAO', 'Em classificação'
        ENCAMINHADA = 'ENCAMINHADA', 'Encaminhada'
        EM_ANALISE = 'EM_ANALISE', 'Em análise'
        AGUARDANDO = 'AGUARDANDO', 'Aguardando informação'
        RESPONDIDA = 'RESPONDIDA', 'Respondida'
        ENCERRADA = 'ENCERRADA', 'Encerrada'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    protocolo = models.CharField(max_length=32)
    tipo = models.CharField(max_length=12, choices=Tipo.choices)
    assunto = models.CharField(max_length=180)
    relato = models.TextField()
    anonima = models.BooleanField(default=False)
    sigilosa = models.BooleanField(default=False)
    nome = models.CharField(max_length=180, blank=True)
    documento = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    telefone = models.CharField(max_length=30, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEBIDA)
    prazo_em = models.DateField(null=True, blank=True)
    secretaria = models.ForeignKey('rh_municipal.Secretaria', null=True, blank=True, on_delete=models.PROTECT, related_name='manifestacoes_ouvidoria')
    responsavel = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='manifestacoes_ouvidoria_responsavel')
    chave_acompanhamento_digest = models.CharField(max_length=64)
    recebido_em = models.DateTimeField(default=timezone.now)
    encerrado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-recebido_em']
        constraints = [models.UniqueConstraint(fields=['organization', 'protocolo'], name='rhmun_ouvidoria_protocolo_org')]


class EventoOuvidoriaMunicipal(PhaseEImmutable):
    manifestacao = models.ForeignKey(ManifestacaoOuvidoriaMunicipal, on_delete=models.PROTECT, related_name='eventos')
    evento = models.CharField(max_length=32)
    status_anterior = models.CharField(max_length=16, blank=True)
    status_novo = models.CharField(max_length=16, blank=True)
    observacao = models.TextField(blank=True)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    registrado_em = models.DateTimeField(default=timezone.now, editable=False)


class RespostaOuvidoriaMunicipal(PhaseEImmutable):
    manifestacao = models.ForeignKey(ManifestacaoOuvidoriaMunicipal, on_delete=models.PROTECT, related_name='respostas')
    resposta = models.TextField()
    publicar_no_acompanhamento = models.BooleanField(default=False)
    respondida_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    respondida_em = models.DateTimeField(default=timezone.now, editable=False)


class PedidoEsicMunicipal(PhaseERecord):
    class Status(models.TextChoices):
        RECEBIDO = 'RECEBIDO', 'Recebido'
        EM_ANALISE = 'EM_ANALISE', 'Em análise'
        ENCAMINHADO = 'ENCAMINHADO', 'Encaminhado'
        AGUARDANDO = 'AGUARDANDO', 'Aguardando informação'
        RESPONDIDO = 'RESPONDIDO', 'Respondido'
        EM_RECURSO = 'EM_RECURSO', 'Em recurso'
        ENCERRADO = 'ENCERRADO', 'Encerrado'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    protocolo = models.CharField(max_length=32)
    assunto = models.CharField(max_length=180)
    pedido = models.TextField()
    nome = models.CharField(max_length=180)
    documento = models.CharField(max_length=20, blank=True)
    email = models.EmailField()
    telefone = models.CharField(max_length=30, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEBIDO)
    prazo_em = models.DateField(null=True, blank=True)
    secretaria = models.ForeignKey('rh_municipal.Secretaria', null=True, blank=True, on_delete=models.PROTECT, related_name='pedidos_esic')
    responsavel = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='pedidos_esic_responsavel')
    chave_acompanhamento_digest = models.CharField(max_length=64)
    recebido_em = models.DateTimeField(default=timezone.now)
    encerrado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-recebido_em']
        constraints = [models.UniqueConstraint(fields=['organization', 'protocolo'], name='rhmun_esic_protocolo_org')]


class RespostaEsicMunicipal(PhaseEImmutable):
    pedido = models.ForeignKey(PedidoEsicMunicipal, on_delete=models.PROTECT, related_name='respostas')
    resposta = models.TextField()
    documento_key = models.CharField(max_length=500, blank=True)
    respondida_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    respondida_em = models.DateTimeField(default=timezone.now, editable=False)


class RecursoEsicMunicipal(PhaseERecord):
    class Status(models.TextChoices):
        RECEBIDO = 'RECEBIDO', 'Recebido'
        EM_ANALISE = 'EM_ANALISE', 'Em análise'
        PROVIDO = 'PROVIDO', 'Provido'
        PARCIAL = 'PARCIAL', 'Parcialmente provido'
        NEGADO = 'NEGADO', 'Negado'

    pedido = models.ForeignKey(PedidoEsicMunicipal, on_delete=models.PROTECT, related_name='recursos')
    fundamento = models.TextField()
    resposta = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RECEBIDO)
    apresentado_em = models.DateTimeField(default=timezone.now)
    decidido_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    decidido_em = models.DateTimeField(null=True, blank=True)


class EventoEsicMunicipal(PhaseEImmutable):
    pedido = models.ForeignKey(PedidoEsicMunicipal, on_delete=models.PROTECT, related_name='eventos')
    recurso = models.ForeignKey(RecursoEsicMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='eventos')
    evento = models.CharField(max_length=32)
    status_anterior = models.CharField(max_length=16, blank=True)
    status_novo = models.CharField(max_length=16, blank=True)
    observacao = models.TextField(blank=True)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    registrado_em = models.DateTimeField(default=timezone.now, editable=False)


class AuditoriaProcessoDigitalMunicipal(PhaseEImmutable):
    acao = models.CharField(max_length=50)
    entidade = models.CharField(max_length=80)
    entidade_public_id = models.UUIDField(null=True, blank=True)
    alteracoes = models.JSONField(default=dict, blank=True)
    motivo = models.TextField(blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)


def tracking_digest(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class ConfiguracaoGovBrMunicipal(MunicipalRecord):
    """Parametrização OIDC Login Único gov.br por Prefeitura (Homologação e Produção)."""
    class Ambiente(models.TextChoices):
        HOMOLOGACAO = 'HOMOLOGACAO', 'Homologação gov.br (sso.staging.acesso.gov.br)'
        PRODUCAO = 'PRODUCAO', 'Produção gov.br (sso.acesso.gov.br)'

    class NivelMinimo(models.TextChoices):
        BRONZE = 'BRONZE', 'Bronze'
        PRATA = 'PRATA', 'Prata'
        OURO = 'OURO', 'Ouro'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    ambiente = models.CharField(max_length=14, choices=Ambiente.choices, default=Ambiente.HOMOLOGACAO)
    client_id = models.CharField(max_length=120, blank=True)
    issuer_url = models.URLField(max_length=300, default='https://sso.staging.acesso.gov.br')
    redirect_uri = models.URLField(max_length=400, blank=True)
    escopos = models.CharField(max_length=180, default='openid email profile govbr_confiabilidades')
    referencia_secret = models.CharField(max_length=80, blank=True)
    nivel_minimo = models.CharField(max_length=10, choices=NivelMinimo.choices, default=NivelMinimo.PRATA)
    modo_homologacao_local = models.BooleanField(default=True)
    ativo = models.BooleanField(default=True)
    atualizada_em = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['organization'], name='rhmun_govbr_config_org'),
        ]


class SessaoCidadaoGovBr(MunicipalRecord):
    """Sessão verificada de cidadão autenticado via Login Único gov.br (ou Homologação)."""
    class Nivel(models.TextChoices):
        BRONZE = 'BRONZE', 'Bronze'
        PRATA = 'PRATA', 'Prata'
        OURO = 'OURO', 'Ouro'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    cpf = models.CharField(max_length=11)
    nome = models.CharField(max_length=180)
    email = models.EmailField(blank=True)
    telefone = models.CharField(max_length=30, blank=True)
    nivel_confiabilidade = models.CharField(max_length=10, choices=Nivel.choices, default=Nivel.PRATA)
    selos = models.JSONField(default=list, blank=True)
    origem_autenticacao = models.CharField(max_length=30, default='GOVBR_HOMOLOGACAO_LOCAL')
    sub_govbr = models.CharField(max_length=120, blank=True)
    autenticado_em = models.DateTimeField(default=timezone.now, editable=False)
    expira_em = models.DateTimeField(null=True, blank=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ['-autenticado_em', '-pk']
