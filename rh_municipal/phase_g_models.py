"""Phase G municipal health domain records.

Clinical content is stored encrypted by the service layer. No public/anonymous
query exposes these models and every clinical read must create an access audit.
This is a municipal operational foundation, not a claim of e-SUS/SUS compliance.
"""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from .models import UnidadeMunicipal
from .phase_f_models import PhaseFRecord
from .phase_d_models import CondutorMunicipal, VeiculoMunicipal


HEALTH_MODULE = 'saude_sus'


class HealthRecord(PhaseFRecord):
    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        health_unit = getattr(self, 'unidade_saude', None)
        if health_unit and not self.unidade_id:
            self.unidade = health_unit.unidade
            self.setor = self.unidade.setor
            self.departamento = self.setor.departamento
            self.secretaria = self.departamento.secretaria
        for field in self._meta.fields:
            if not isinstance(field, models.ForeignKey) or not getattr(self, field.attname, None):
                continue
            related = getattr(self, field.name, None)
            related_org = getattr(related, 'organization_id', None)
            if related_org is not None and related_org != self.organization_id:
                raise ValidationError({field.name: 'O vínculo pertence a outra Prefeitura.'})


class UnidadeSaudeMunicipal(HealthRecord):
    """Configuração de saúde para a Unidade Municipal já existente."""
    unidade = models.OneToOneField(UnidadeMunicipal, on_delete=models.PROTECT, related_name='configuracao_saude')
    tipo = models.CharField(max_length=24, choices=[
        ('UBS', 'UBS'), ('CENTRO', 'Centro de Saúde'), ('HOSPITAL', 'Hospital'),
        ('FARMACIA', 'Farmácia'), ('VIGILANCIA', 'Vigilância'), ('OUTRA', 'Outra'),
    ], default='UBS')
    cnes = models.CharField(max_length=20, blank=True)
    servicos = models.JSONField(default=list, blank=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'unidade'], name='rhmun_saude_unit_org')]


class EquipeSaudeMunicipal(HealthRecord):
    codigo = models.CharField(max_length=32)
    nome = models.CharField(max_length=140)
    tipo = models.CharField(max_length=24, choices=[('ESF', 'Saúde da Família'), ('ACS', 'ACS'), ('ACE', 'ACE'), ('OUTRA', 'Outra')])
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='equipes')
    microarea = models.CharField(max_length=30, blank=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_saude_team_org_code')]


class ProfissionalSaudeMunicipal(HealthRecord):
    servidor = models.ForeignKey('rh_municipal.Servidor', on_delete=models.PROTECT, related_name='vinculos_saude')
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='profissionais')
    equipe = models.ForeignKey(EquipeSaudeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='profissionais')
    ocupacao = models.CharField(max_length=120)
    conselho = models.CharField(max_length=32, blank=True)
    registro_conselho = models.CharField(max_length=40, blank=True)
    especialidade = models.CharField(max_length=120, blank=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'servidor', 'unidade_saude'], name='rhmun_saude_prof_unit')]


class CidadaoSaudeMunicipal(HealthRecord):
    """Shared municipal patient/citizen identity source for future education reuse."""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    cpf = models.CharField(max_length=11, blank=True)
    cns = models.CharField(max_length=15, blank=True)
    nome = models.CharField(max_length=180)
    data_nascimento = models.DateField(null=True, blank=True)
    telefone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    cep = models.CharField(max_length=9, blank=True)
    endereco = models.CharField(max_length=220, blank=True)
    bairro = models.CharField(max_length=100, blank=True)
    municipio = models.CharField(max_length=100, blank=True)
    uf = models.CharField(max_length=2, blank=True)
    situacao = models.CharField(max_length=12, choices=[('ATIVO', 'Ativo'), ('INATIVO', 'Inativo')], default='ATIVO')
    criado_em = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['organization', 'cpf'], condition=~models.Q(cpf=''), name='rhmun_saude_citizen_cpf'),
            models.UniqueConstraint(fields=['organization', 'cns'], condition=~models.Q(cns=''), name='rhmun_saude_citizen_cns'),
        ]

    def clean(self):
        super().clean()
        if self.cpf and (not self.cpf.isdigit() or len(self.cpf) != 11):
            raise ValidationError({'cpf': 'Informe CPF normalizado com 11 dígitos.'})
        if self.cns and (not self.cns.isdigit() or len(self.cns) != 15):
            raise ValidationError({'cns': 'Informe CNS normalizado com 15 dígitos.'})


class FamiliaTerritorialSaude(HealthRecord):
    codigo = models.CharField(max_length=36)
    referencia = models.CharField(max_length=160, blank=True)
    responsavel_nome = models.CharField(max_length=140, blank=True)
    endereco = models.CharField(max_length=220)
    microarea = models.CharField(max_length=30, blank=True)
    equipe = models.ForeignKey(EquipeSaudeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='familias')
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='familias')
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    risco_territorial = models.CharField(
        max_length=12,
        choices=[('BAIXO', 'Baixo'), ('MEDIO', 'Médio'), ('ALTO', 'Alto'), ('CRITICO', 'Crítico')],
        default='BAIXO',
    )
    condicoes_sentinelas = models.JSONField(default=list, blank=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_saude_family_org_code')]


class VinculoFamiliaSaude(HealthRecord):
    familia = models.ForeignKey(FamiliaTerritorialSaude, on_delete=models.PROTECT, related_name='membros')
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, on_delete=models.PROTECT, related_name='familias')
    parentesco = models.CharField(max_length=50, blank=True)
    referencia = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'familia', 'cidadao'], name='rhmun_saude_family_member')]


class AgendaSaudeMunicipal(HealthRecord):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='agendas')
    profissional = models.ForeignKey(ProfissionalSaudeMunicipal, on_delete=models.PROTECT, related_name='agendas')
    inicio = models.DateTimeField()
    fim = models.DateTimeField()
    capacidade = models.PositiveSmallIntegerField(default=1)
    situacao = models.CharField(max_length=12, choices=[('ABERTA', 'Aberta'), ('BLOQUEADA', 'Bloqueada')], default='ABERTA')

    class Meta:
        ordering = ['inicio']
        constraints = [models.CheckConstraint(condition=models.Q(fim__gt=models.F('inicio')), name='rhmun_saude_agenda_time')]


class AtendimentoSaudeMunicipal(HealthRecord):
    class Status(models.TextChoices):
        AGENDADO = 'AGENDADO', 'Agendado'
        EM_ATENDIMENTO = 'EM_ATENDIMENTO', 'Em atendimento'
        CONCLUIDO = 'CONCLUIDO', 'Concluído'
        CANCELADO = 'CANCELADO', 'Cancelado'
        FALTOU = 'FALTOU', 'Faltou'
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, on_delete=models.PROTECT, related_name='atendimentos')
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='atendimentos')
    profissional = models.ForeignKey(ProfissionalSaudeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='atendimentos')
    agenda = models.ForeignKey(AgendaSaudeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='atendimentos')
    iniciado_em = models.DateTimeField(null=True, blank=True)
    concluido_em = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.AGENDADO)
    codigo_procedimento = models.CharField(max_length=32, blank=True)
    queixa_portal_cifrada = models.BinaryField(blank=True, default=bytes)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class EvolucaoClinicaMunicipal(HealthRecord):
    """Encrypted clinical note payload; plaintext is never stored by service APIs."""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    atendimento = models.ForeignKey(AtendimentoSaudeMunicipal, on_delete=models.PROTECT, related_name='evolucoes')
    conteudo_cifrado = models.BinaryField()
    conteudo_hash = models.CharField(max_length=64)
    autor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criada_em = models.DateTimeField(default=timezone.now, editable=False)
    retifica = models.OneToOneField('self', null=True, blank=True, on_delete=models.PROTECT,
                                   related_name='retificacao')

    class Meta:
        ordering = ['criada_em', 'pk']


class AuditoriaAcessoSaude(HealthRecord):
    acao = models.CharField(max_length=40)
    entidade = models.CharField(max_length=80)
    entidade_id = models.PositiveBigIntegerField(null=True, blank=True)
    paciente_id = models.PositiveBigIntegerField(null=True, blank=True)
    justificativa = models.CharField(max_length=300, blank=True)
    detalhes = models.JSONField(default=dict, blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    ocorrido_em = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ['-ocorrido_em', '-pk']


class PrescricaoSaudeMunicipal(HealthRecord):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    atendimento = models.ForeignKey(AtendimentoSaudeMunicipal, on_delete=models.PROTECT, related_name='prescricoes')
    profissional = models.ForeignKey(ProfissionalSaudeMunicipal, on_delete=models.PROTECT, related_name='prescricoes')
    emitida_em = models.DateTimeField(default=timezone.now)
    orientacao_cifrada = models.BinaryField(blank=True, default=bytes)
    status = models.CharField(max_length=12, choices=[('ATIVA', 'Ativa'), ('CANCELADA', 'Cancelada')], default='ATIVA')


class ItemPrescricaoSaude(HealthRecord):
    prescricao = models.ForeignKey(PrescricaoSaudeMunicipal, on_delete=models.PROTECT, related_name='itens')
    medicamento_nome = models.CharField(max_length=140, blank=True)
    medicamento_nome_cifrado = models.BinaryField(blank=True, default=bytes)
    apresentacao = models.CharField(max_length=100, blank=True)
    quantidade = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0.01)])
    posologia_cifrada = models.BinaryField(blank=True, default=bytes)


class VisitaTerritorialSaude(HealthRecord):
    class Tipo(models.TextChoices):
        ACS = 'ACS', 'Agente Comunitário de Saúde'
        ACE = 'ACE', 'Agente de Combate a Endemias'
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tipo = models.CharField(max_length=3, choices=Tipo.choices)
    familia = models.ForeignKey(FamiliaTerritorialSaude, null=True, blank=True, on_delete=models.PROTECT, related_name='visitas')
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='visitas')
    imovel = models.ForeignKey('rh_municipal.ImovelMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='visitas_saude')
    agente = models.ForeignKey(ProfissionalSaudeMunicipal, on_delete=models.PROTECT, related_name='visitas')
    realizada_em = models.DateTimeField(default=timezone.now)
    observacoes_cifradas = models.BinaryField(blank=True, default=bytes)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    pa_sistolica = models.PositiveIntegerField(null=True, blank=True)
    pa_diastolica = models.PositiveIntegerField(null=True, blank=True)
    glicemia = models.PositiveIntegerField(null=True, blank=True)
    peso_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    condicao_acompanhada = models.CharField(max_length=32, blank=True)
    alerta_epidemiologico = models.CharField(max_length=180, blank=True)
    status = models.CharField(max_length=16, choices=[('REALIZADA', 'Realizada'), ('PENDENTE', 'Pendente'), ('CANCELADA', 'Cancelada')], default='REALIZADA')


class MedicamentoMunicipal(HealthRecord):
    codigo = models.CharField(max_length=40)
    nome = models.CharField(max_length=160)
    apresentacao = models.CharField(max_length=100)
    unidade_medida = models.CharField(max_length=20)
    estoque_minimo = models.DecimalField(max_digits=12, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    ativo = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_saude_medicine_code')]


class LoteMedicamentoMunicipal(HealthRecord):
    medicamento = models.ForeignKey(MedicamentoMunicipal, on_delete=models.PROTECT, related_name='lotes')
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='lotes')
    lote = models.CharField(max_length=60)
    validade = models.DateField()
    saldo = models.DecimalField(max_digits=12, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    recebido_em = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'medicamento', 'unidade_saude', 'lote'], name='rhmun_saude_medicine_lot')]


class DispensacaoMedicamentoMunicipal(HealthRecord):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, on_delete=models.PROTECT, related_name='dispensacoes')
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='dispensacoes')
    prescricao = models.ForeignKey(PrescricaoSaudeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='dispensacoes')
    request_id = models.UUIDField(default=uuid.uuid4)
    dispensada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    dispensada_em = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=12, choices=[('CONFIRMADA', 'Confirmada'), ('CANCELADA', 'Cancelada')], default='CONFIRMADA')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'request_id'], name='rhmun_saude_dispense_req')]


class ItemDispensacaoSaude(HealthRecord):
    dispensacao = models.ForeignKey(DispensacaoMedicamentoMunicipal, on_delete=models.PROTECT, related_name='itens')
    lote = models.ForeignKey(LoteMedicamentoMunicipal, on_delete=models.PROTECT, related_name='dispensacoes')
    quantidade = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(0.001)])


class EstabelecimentoSanitarioMunicipal(HealthRecord):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    contribuinte = models.ForeignKey('rh_municipal.ContribuinteMunicipal', null=True, blank=True, on_delete=models.PROTECT, related_name='estabelecimentos_sanitarios')
    razao_social = models.CharField(max_length=180)
    documento = models.CharField(max_length=14, blank=True)
    atividade = models.CharField(max_length=120, blank=True)
    endereco = models.CharField(max_length=220)
    situacao = models.CharField(max_length=12, choices=[('ATIVO', 'Ativo'), ('SUSPENSO', 'Suspenso'), ('INATIVO', 'Inativo')], default='ATIVO')


class InspecaoSanitariaMunicipal(HealthRecord):
    estabelecimento = models.ForeignKey(EstabelecimentoSanitarioMunicipal, on_delete=models.PROTECT, related_name='inspecoes')
    realizada_por = models.ForeignKey(ProfissionalSaudeMunicipal, on_delete=models.PROTECT, related_name='inspecoes_sanitarias')
    realizada_em = models.DateTimeField(default=timezone.now)
    resultado = models.CharField(max_length=16, choices=[('PENDENTE', 'Pendente'), ('REGULAR', 'Regular'), ('IRREGULAR', 'Irregular')], default='PENDENTE')
    observacoes_cifradas = models.BinaryField(blank=True, default=bytes)
    prazo_retorno = models.DateField(null=True, blank=True)


class NotificacaoEpidemiologicaMunicipal(HealthRecord):
    """Restricted record; classifications/protocols require official curation."""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='notificacoes_epidemiologicas')
    codigo_agravo = models.CharField(max_length=40, blank=True)
    descricao_agravo = models.CharField(max_length=160, blank=True)
    notificada_em = models.DateField(default=timezone.localdate)
    detalhes_cifrados = models.BinaryField(blank=True, default=bytes)
    situacao = models.CharField(max_length=14, choices=[('ABERTA', 'Aberta'), ('EM_ANALISE', 'Em análise'), ('ENCERRADA', 'Encerrada')], default='ABERTA')
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class SolicitacaoRegulacaoSaude(HealthRecord):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, on_delete=models.PROTECT, related_name='solicitacoes_regulacao')
    unidade_origem = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='regulacoes_origem')
    unidade_destino = models.ForeignKey(UnidadeSaudeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='regulacoes_destino')
    procedimento = models.CharField(max_length=160)
    solicitada_em = models.DateTimeField(default=timezone.now)
    prioridade = models.CharField(max_length=16, choices=[('ROTINA', 'Rotina'), ('PREFERENCIAL', 'Preferencial'), ('URGENTE', 'Urgente')], default='ROTINA')
    prioridade_justificativa = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=16, choices=[('FILA', 'Na fila'), ('AGENDADA', 'Agendada'), ('ATENDIDA', 'Atendida'), ('CANCELADA', 'Cancelada')], default='FILA')
    justificativa_cifrada = models.BinaryField(blank=True, default=bytes)
    solicitado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class VagaRegulacaoSaude(HealthRecord):
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='vagas_regulacao')
    procedimento = models.CharField(max_length=160)
    inicio = models.DateTimeField()
    capacidade = models.PositiveSmallIntegerField(default=1)
    ativo = models.BooleanField(default=True)


class AgendamentoRegulacaoSaude(HealthRecord):
    """Integrity-preserving link between a referral, offered slot and appointment."""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    solicitacao = models.OneToOneField(
        SolicitacaoRegulacaoSaude, on_delete=models.PROTECT, related_name='agendamento_regulacao',
    )
    vaga = models.ForeignKey(VagaRegulacaoSaude, on_delete=models.PROTECT, related_name='agendamentos_regulacao')
    atendimento = models.OneToOneField(
        AtendimentoSaudeMunicipal, on_delete=models.PROTECT, related_name='agendamento_regulacao',
    )
    situacao = models.CharField(
        max_length=12,
        choices=[('AGENDADO', 'Agendado'), ('ATENDIDO', 'Atendido'), ('CANCELADO', 'Cancelado')],
        default='AGENDADO',
    )
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)


class TFDMunicipal(HealthRecord):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, on_delete=models.PROTECT, related_name='solicitacoes_tfd')
    destino = models.CharField(max_length=180)
    tratamento = models.CharField(max_length=180)
    acompanhante = models.CharField(max_length=180, blank=True)
    solicitada_em = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=16, choices=[('PENDENTE', 'Pendente'), ('AUTORIZADA', 'Autorizada'), ('NEGADA', 'Negada'), ('CONCLUIDA', 'Concluída')], default='PENDENTE')
    documentos = models.JSONField(default=list, blank=True)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class VeiculoSanitarioMunicipal(HealthRecord):
    """Assignment to the existing municipal fleet vehicle."""
    veiculo = models.ForeignKey(VeiculoMunicipal, on_delete=models.PROTECT, related_name='uso_sanitario')
    condutor = models.ForeignKey(CondutorMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='uso_sanitario')
    acessivel = models.BooleanField(default=False)
    ativo = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'veiculo'], name='rhmun_saude_vehicle_org')]


class ServicoFilaSaude(HealthRecord):
    """Serviços de atendimento configurados na Unidade de Saúde (padrão Novo SGA)."""
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='servicos_fila')
    codigo = models.CharField(max_length=8)
    nome = models.CharField(max_length=120)
    descricao = models.CharField(max_length=255, blank=True)
    icone = models.CharField(max_length=60, default='activity')
    ordem = models.PositiveIntegerField(default=1)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ['ordem', 'nome']


class LocalAtendimentoSaude(HealthRecord):
    """Guichês, consultórios e salas da Unidade de Saúde (padrão Novo SGA)."""
    class Tipo(models.TextChoices):
        CONSULTORIO = 'CONSULTORIO', 'Consultório Médico'
        TRIAGEM = 'TRIAGEM', 'Sala de Triagem / Enfermagem'
        GUICHE = 'GUICHE', 'Guichê de Atendimento / Recepção'
        SALA_VACINA = 'SALA_VACINA', 'Sala de Vacinação'
        ODONTOLOGIA = 'ODONTOLOGIA', 'Consultório Odontológico'
        FARMACIA = 'FARMACIA', 'Farmácia Básica Municipal'
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='locais_atendimento')
    tipo = models.CharField(max_length=30, choices=Tipo.choices, default=Tipo.CONSULTORIO)
    numero = models.PositiveIntegerField(default=1)
    nome = models.CharField(max_length=120)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ['tipo', 'numero']


class SenhaFilaAtendimento(HealthRecord):
    """Senhas emitidas no Totem, chamadas na SmartTV e atendidas no Console (padrão Novo SGA)."""
    class Status(models.TextChoices):
        EMITIDA = 'EMITIDA', 'Emitida (Aguardando)'
        CHAMADA = 'CHAMADA', 'Chamada na SmartTV'
        EM_ATENDIMENTO = 'EM_ATENDIMENTO', 'Em Atendimento'
        ENCERRADA = 'ENCERRADA', 'Atendimento Encerrado'
        NAO_COMPARECEU = 'NAO_COMPARECEU', 'Não Compareceu'
        CANCELADA = 'CANCELADA', 'Cancelada'

    class TipoPrioridade(models.TextChoices):
        CONVENCIONAL = 'CONVENCIONAL', 'Convencional'
        PRIORITARIA = 'PRIORITARIA', 'Prioritária (Lei 10.048/00)'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='senhas_fila')
    servico = models.ForeignKey(ServicoFilaSaude, on_delete=models.PROTECT, related_name='senhas')
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, null=True, blank=True, on_delete=models.SET_NULL, related_name='senhas_fila')
    local_chamado = models.ForeignKey(LocalAtendimentoSaude, null=True, blank=True, on_delete=models.SET_NULL, related_name='senhas_atendidas')
    profissional = models.ForeignKey(ProfissionalSaudeMunicipal, null=True, blank=True, on_delete=models.SET_NULL, related_name='senhas_atendidas')
    numero_sequencial = models.PositiveIntegerField()
    tipo_prioridade = models.CharField(max_length=20, choices=TipoPrioridade.choices, default=TipoPrioridade.CONVENCIONAL)
    detalhe_prioridade = models.CharField(max_length=60, blank=True)
    codigo_formatado = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.EMITIDA)
    emitida_em = models.DateTimeField(default=timezone.now)
    chamada_em = models.DateTimeField(null=True, blank=True)
    iniciada_em = models.DateTimeField(null=True, blank=True)
    encerrada_em = models.DateTimeField(null=True, blank=True)
    atendimento_clinico = models.ForeignKey(AtendimentoSaudeMunicipal, null=True, blank=True, on_delete=models.SET_NULL, related_name='senhas_vinculadas')

    class Meta:
        ordering = ['-emitida_em']


class ClassificacaoManchester(HealthRecord):
    """Classificação de Risco e Triagem Manchester (Enfermagem e-SUS APS)."""
    class Cor(models.TextChoices):
        VERMELHO = 'VERMELHO', 'Vermelho - Emergência (0 min)'
        LARANJA = 'LARANJA', 'Laranja - Muito Urgente (10 min)'
        AMARELO = 'AMARELO', 'Amarelo - Urgente (60 min)'
        VERDE = 'VERDE', 'Verde - Pouco Urgente (120 min)'
        AZUL = 'AZUL', 'Azul - Não Urgente (240 min)'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    senha = models.OneToOneField(SenhaFilaAtendimento, null=True, blank=True, on_delete=models.CASCADE, related_name='triagem_manchester')
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, on_delete=models.PROTECT, related_name='classificacoes_manchester')
    enfermeiro = models.ForeignKey(ProfissionalSaudeMunicipal, on_delete=models.PROTECT, related_name='triagens_realizadas')
    fluxograma = models.CharField(max_length=120)
    discriminador = models.CharField(max_length=140)
    cor = models.CharField(max_length=15, choices=Cor.choices, default=Cor.VERDE)
    tempo_alvo_minutos = models.PositiveIntegerField(default=120)
    pa_sistolica = models.PositiveIntegerField(null=True, blank=True)
    pa_diastolica = models.PositiveIntegerField(null=True, blank=True)
    frequencia_cardiaca = models.PositiveIntegerField(null=True, blank=True)
    frequencia_respiratoria = models.PositiveIntegerField(null=True, blank=True)
    temperatura = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    glicemia = models.PositiveIntegerField(null=True, blank=True)
    saturacao_o2 = models.PositiveIntegerField(null=True, blank=True)
    peso_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    altura_cm = models.PositiveIntegerField(null=True, blank=True)
    queixa_principal = models.CharField(max_length=255, blank=True)
    queixa_principal_cifrada = models.BinaryField(blank=True, default=bytes)
    avaliada_em = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-avaliada_em']


class RegistroProducaoSUS(HealthRecord):
    class Tipo(models.TextChoices):
        PROCEDIMENTO = 'PROCEDIMENTO', 'Produção de procedimento'
        AIH_RASCUNHO = 'AIH_RASCUNHO', 'Rascunho AIH'
        APAC_RASCUNHO = 'APAC_RASCUNHO', 'Rascunho APAC'
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    competencia = models.CharField(max_length=7)
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='producoes')
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='producoes')
    profissional = models.ForeignKey(ProfissionalSaudeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='producoes')
    codigo_procedimento = models.CharField(max_length=40, blank=True)
    quantidade = models.PositiveIntegerField(default=1)
    dados_snapshot_cifrado = models.BinaryField(blank=True, default=bytes)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)
    status_transmissao = models.CharField(max_length=18, default='NAO_TRANSMITIDO')


class AlaEnfermariaSaude(HealthRecord):
    """Alas hospitalares, observação UPA, enfermarias e UTIs municipais."""
    class Tipo(models.TextChoices):
        OBSERVACAO = 'OBSERVACAO', 'Observação / Pronto Atendimento'
        SALA_VERMELHA = 'SALA_VERMELHA', 'Sala Vermelha / Estabilização'
        ENFERMARIA = 'ENFERMARIA', 'Enfermaria Clínica'
        UTI_ADULTO = 'UTI_ADULTO', 'UTI Adulto'
        UTI_PEDIATRICA = 'UTI_PEDIATRICA', 'UTI Pediátrica / Neo'
        OBSTETRICIA = 'OBSTETRICIA', 'Obstetrícia / Parto'
        ISOLAMENTO = 'ISOLAMENTO', 'Isolamento Respiratório / Contato'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='alas')
    codigo = models.CharField(max_length=24)
    nome = models.CharField(max_length=120)
    tipo = models.CharField(max_length=24, choices=Tipo.choices, default=Tipo.OBSERVACAO)
    capacidade_planejada = models.PositiveSmallIntegerField(default=8)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ['unidade_saude', 'codigo']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'unidade_saude', 'codigo'], name='rhmun_ala_saude_org_unit_cod'),
        ]


class LeitoSaudeMunicipal(HealthRecord):
    """Mapa de leitos hospitalares e observação municipal integrado ao prontuário e monitor."""
    class Status(models.TextChoices):
        DISPONIVEL = 'DISPONIVEL', 'Disponível'
        OCUPADO = 'OCUPADO', 'Ocupado'
        HIGIENIZACAO = 'HIGIENIZACAO', 'Em Higienização'
        MANUTENCAO = 'MANUTENCAO', 'Manutenção'
        RESERVADO = 'RESERVADO', 'Reservado / Regulação'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='leitos')
    ala = models.ForeignKey(AlaEnfermariaSaude, on_delete=models.PROTECT, related_name='leitos')
    codigo = models.CharField(max_length=24)
    descricao = models.CharField(max_length=120, blank=True)
    tipo_leito = models.CharField(max_length=24, choices=AlaEnfermariaSaude.Tipo.choices, default=AlaEnfermariaSaude.Tipo.OBSERVACAO)
    status = models.CharField(max_length=18, choices=Status.choices, default=Status.DISPONIVEL)
    paciente_atual = models.ForeignKey(CidadaoSaudeMunicipal, null=True, blank=True, on_delete=models.SET_NULL, related_name='leitos_ocupados')
    atendimento_atual = models.ForeignKey(AtendimentoSaudeMunicipal, null=True, blank=True, on_delete=models.SET_NULL, related_name='leitos')
    profissional_responsavel = models.ForeignKey(ProfissionalSaudeMunicipal, null=True, blank=True, on_delete=models.SET_NULL, related_name='leitos_responsaveis')
    admitido_em = models.DateTimeField(null=True, blank=True)
    alta_prevista_em = models.DateField(null=True, blank=True)
    precaucao_isolamento = models.CharField(max_length=60, blank=True)
    ativo = models.BooleanField(default=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['ala', 'codigo']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'unidade_saude', 'codigo'], name='rhmun_leito_saude_org_unit_cod'),
        ]


class EquipamentoMonitorSaude(HealthRecord):
    """Monitor multiparamétrico conectado à rede da unidade (Wi-Fi/HL7/HTTP Gateway)."""
    class Protocolo(models.TextChoices):
        HTTP_JSON = 'HTTP_JSON', 'Gateway HTTP/Wi-Fi JSON'
        HL7_V2 = 'HL7_V2', 'Ponte HL7 v2 / MLLP'
        MQTT_TLS = 'MQTT_TLS', 'Broker MQTT TLS'
        SIMULADOR = 'SIMULADOR', 'Simulador Clínico Local'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    unidade_saude = models.ForeignKey(UnidadeSaudeMunicipal, on_delete=models.PROTECT, related_name='monitores')
    leito = models.OneToOneField(LeitoSaudeMunicipal, null=True, blank=True, on_delete=models.SET_NULL, related_name='monitor')
    serial_dispositivo = models.CharField(max_length=64)
    modelo = models.CharField(max_length=100, default='Monitor Multiparamétrico 5 Parâmetros')
    protocolo = models.CharField(max_length=20, choices=Protocolo.choices, default=Protocolo.HTTP_JSON)
    endpoint_ou_ip = models.CharField(max_length=120, blank=True)
    bateria_percentual = models.PositiveSmallIntegerField(default=100)
    online = models.BooleanField(default=False)
    ultimo_heartbeat = models.DateTimeField(null=True, blank=True)
    token_ingestao = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['serial_dispositivo']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'serial_dispositivo'], name='rhmun_monitor_saude_serial_org'),
        ]


class TelemetriaMonitorLeito(HealthRecord):
    """Aferições em tempo real dos sinais vitais enviadas pelo monitor multiparamétrico."""
    class NivelAlerta(models.TextChoices):
        NORMAL = 'NORMAL', 'Estável / Normal'
        ATENCAO = 'ATENCAO', 'Atenção'
        CRITICO = 'CRITICO', 'Crítico / Alarme'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    equipamento = models.ForeignKey(EquipamentoMonitorSaude, on_delete=models.PROTECT, related_name='telemetrias')
    leito = models.ForeignKey(LeitoSaudeMunicipal, on_delete=models.PROTECT, related_name='telemetrias')
    cidadao = models.ForeignKey(CidadaoSaudeMunicipal, null=True, blank=True, on_delete=models.SET_NULL, related_name='telemetrias_leito')
    coletado_em = models.DateTimeField(default=timezone.now)
    fc_bpm = models.PositiveSmallIntegerField(null=True, blank=True)
    spo2_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    pa_sistolica = models.PositiveSmallIntegerField(null=True, blank=True)
    pa_diastolica = models.PositiveSmallIntegerField(null=True, blank=True)
    pam_mmhg = models.PositiveSmallIntegerField(null=True, blank=True)
    fr_irpm = models.PositiveSmallIntegerField(null=True, blank=True)
    temperatura_c = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    etco2_mmhg = models.PositiveSmallIntegerField(null=True, blank=True)
    ritmo_ecg = models.CharField(max_length=40, default='SINUSAL')
    nivel_alerta = models.CharField(max_length=12, choices=NivelAlerta.choices, default=NivelAlerta.NORMAL)
    alertas_detectados = models.JSONField(default=list, blank=True)
    origem_leitura = models.CharField(max_length=24, default='TELEMETRIA_REAL')

    class Meta:
        ordering = ['-coletado_em', '-pk']


class EventoAlertaMonitorSaude(HealthRecord):
    """Histórico auditado de alarmes clínicos do monitor e reconhecimento pela equipe."""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    leito = models.ForeignKey(LeitoSaudeMunicipal, on_delete=models.PROTECT, related_name='alertas')
    telemetria = models.ForeignKey(TelemetriaMonitorLeito, null=True, blank=True, on_delete=models.SET_NULL, related_name='eventos_alerta')
    severidade = models.CharField(max_length=12, choices=[('ATENCAO', 'Atenção'), ('CRITICO', 'Crítico')], default='CRITICO')
    parametro = models.CharField(max_length=40)
    valor_aferido = models.CharField(max_length=40)
    mensagem = models.CharField(max_length=240)
    ocorrido_em = models.DateTimeField(default=timezone.now)
    reconhecido = models.BooleanField(default=False)
    reconhecido_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='alertas_monitor_reconhecidos')
    reconhecido_em = models.DateTimeField(null=True, blank=True)
    conduta = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-ocorrido_em', '-pk']
