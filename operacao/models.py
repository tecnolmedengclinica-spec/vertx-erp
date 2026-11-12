"""Complementos do ERP; identidade, estoque e financeiro permanecem nos apps existentes."""
import uuid
from datetime import time
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Sum, Q
from django.utils import timezone
from gestao.models import Validado, RegistroImutavel, ZERO

PRIORIDADES = [('BAIXA', 'Baixa'), ('NORMAL', 'Normal'), ('ALTA', 'Alta'), ('URGENTE', 'Urgente')]
TIPOS_SERVICO = [('CORRETIVA', 'Corretiva'), ('PREVENTIVA', 'Preventiva'), ('INSTALACAO', 'Instalação'), ('CALIBRACAO', 'Calibração'), ('INSPECAO', 'Inspeção'), ('VISITA', 'Visita técnica'), ('RETIRADA', 'Retirada'), ('ENTREGA', 'Entrega'), ('TREINAMENTO', 'Treinamento'), ('AVALIACAO', 'Avaliação/orçamento')]
TIPOS_RESPOSTA = [('BOOLEANO', 'Sim/não'), ('CONFORMIDADE', 'Conforme/não conforme'), ('TEXTO', 'Texto'), ('NUMERO', 'Número'), ('MEDICAO', 'Medição'), ('DATA', 'Data'), ('SELECAO', 'Seleção'), ('FOTO', 'Foto'), ('OBSERVACAO', 'Observação')]
ENCERRADAS = ['FINALIZADA', 'APROVADA', 'FATURADA', 'CANCELADA']

def dias_alerta_padrao():
    return [90, 60, 30, 15, 7]

def validar_dias_alerta(value):
    if not isinstance(value, list) or not value or len(value) > 12 or any(type(day) is not int or not 1 <= day <= 365 for day in value) or len(set(value)) != len(value):
        raise ValidationError('Informe uma lista de dias distintos entre 1 e 365.')

def privado(instance, filename):
    from pathlib import PurePosixPath
    suffix = PurePosixPath(filename).suffix.lower()
    return f'organizations/{instance.organization.public_id}/assistencia/documentos/{uuid.uuid4().hex}{suffix}'

def checklist_valido(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValidationError('Checklist deve ser lista de até 100 itens.')
    for row in value:
        if not isinstance(row, dict) or set(row) - {'item', 'obrigatorio', 'concluido', 'observacao', 'tipo', 'resposta', 'opcoes', 'unidade'} or not isinstance(row.get('item'), str) or not row['item'].strip() or len(row['item']) > 255:
            raise ValidationError('Cada item precisa de descrição e campos de resposta válidos.')
        if any(key in row and not isinstance(row[key], bool) for key in ['obrigatorio', 'concluido']) or len(str(row.get('observacao', ''))) > 1000:
            raise ValidationError('Resposta de checklist inválida.')
        kind = row.get('tipo')
        if kind is not None and kind not in dict(TIPOS_RESPOSTA):
            raise ValidationError('Tipo de resposta inválido.')
        if not isinstance(row.get('unidade', ''), str) or len(row.get('unidade', '')) > 40:
            raise ValidationError('Unidade de medição inválida.')
        options = row.get('opcoes', [])
        if not isinstance(options, list) or len(options) > 50 or any(not isinstance(option, str) or not option or len(option) > 150 for option in options) or len(set(options)) != len(options):
            raise ValidationError('Opções devem ser textos distintos, até 150 caracteres.')
        if kind == 'SELECAO' and not options:
            raise ValidationError('Seleção exige opções.')
        answer = row.get('resposta')
        if answer is None or answer == '':
            continue
        if kind in ['BOOLEANO', 'CONFORMIDADE'] and type(answer) is not bool:
            raise ValidationError('Informe sim/não ou conforme/não conforme.')
        if kind in ['TEXTO', 'OBSERVACAO'] and (not isinstance(answer, str) or len(answer) > 4000):
            raise ValidationError('Resposta textual inválida.')
        if kind in ['NUMERO', 'MEDICAO']:
            try:
                if isinstance(answer, bool) or not Decimal(str(answer)).is_finite():
                    raise ValueError
            except (ValueError, ArithmeticError):
                raise ValidationError('Informe um número finito.')
        if kind == 'DATA':
            from datetime import date
            try:
                date.fromisoformat(answer)
            except (ValueError, TypeError):
                raise ValidationError('Informe data no formato AAAA-MM-DD.')
        if kind == 'SELECAO' and answer not in options:
            raise ValidationError('Resposta fora das opções do checklist.')
        if kind == 'FOTO' and (type(answer) is not int or answer < 1):
            raise ValidationError('Informe uma foto deste atendimento.')

class Nomeado(Validado):
    nome = models.CharField(max_length=150)
    ativo = models.BooleanField(default=True)
    class Meta:
        abstract = True
        ordering = ['nome']
        constraints = [models.UniqueConstraint(fields=['organization', 'nome'], name='%(class)s_nome_organizacao')]
    def __str__(self):
        return self.nome

BLOCOS_PAINEL = [('indicadores','Indicadores'),('agora','Atividades agora'),('equipe','Equipe'),('timeline','Linha do tempo'),('fila','Fila de espera'),('concluidas','Concluídas hoje'),('preventivas','Preventivas'),('alertas','Alertas')]

def blocos_painel_padrao():
    return [key for key, label in BLOCOS_PAINEL]

def validar_blocos_painel(value):
    if not isinstance(value,list) or not value or any(not isinstance(key,str) for key in value) or len(value)!=len(set(value)) or any(key not in dict(BLOCOS_PAINEL) for key in value):
        raise ValidationError('Escolha blocos válidos, sem duplicação.')

class Configuracao(Validado):
    painel_intervalo = models.PositiveSmallIntegerField(default=30, choices=[(15, '15 segundos'), (30, '30 segundos'), (60, '60 segundos')])
    painel_tema = models.CharField(max_length=8, default='ESCURO', choices=[('ESCURO', 'Escuro'), ('CLARO', 'Claro')])
    painel_blocos = models.JSONField(default=blocos_painel_padrao, validators=[validar_blocos_painel])
    estoque_minimo_padrao = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO, validators=[MinValueValidator(ZERO)])
    estoque_ideal_padrao = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO, validators=[MinValueValidator(ZERO)])
    intervalo_alertas_minutos = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1), MaxValueValidator(1440)])
    dias_alerta_validade = models.JSONField(default=dias_alerta_padrao, validators=[validar_dias_alerta])
    relatorio_semanal_ativo = models.BooleanField(default=True)
    relatorio_dia_semana = models.PositiveSmallIntegerField(default=0, choices=[(0,'Segunda'),(1,'Terça'),(2,'Quarta'),(3,'Quinta'),(4,'Sexta'),(5,'Sábado'),(6,'Domingo')])
    relatorio_horario = models.TimeField(default=time(8, 0))
    consumo_historico_dias = models.PositiveSmallIntegerField(default=90, validators=[MinValueValidator(30), MaxValueValidator(365)])
    reposicao_dias = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1), MaxValueValidator(365)])
    ultima_verificacao_estoque = models.DateTimeField(null=True, blank=True, editable=False)
    endereco = models.TextField('Endereço completo', blank=True)
    prefixo_os = models.CharField(max_length=12, default='OS')
    prefixo_orcamento = models.CharField(max_length=12, default='ORC')
    prefixo_contrato = models.CharField(max_length=12, default='CT')
    digitos_numero = models.PositiveSmallIntegerField(default=6, validators=[MinValueValidator(3), MaxValueValidator(12)])
    prazo_financeiro_dias = models.PositiveSmallIntegerField(default=30)
    juros_mensal_percentual = models.DecimalField(max_digits=6, decimal_places=3, default=ZERO, validators=[MinValueValidator(ZERO)])
    multa_percentual = models.DecimalField(max_digits=6, decimal_places=3, default=ZERO, validators=[MinValueValidator(ZERO)])
    municipio_fiscal = models.CharField('Código do município fiscal', max_length=10, blank=True)
    codigo_servico = models.CharField(max_length=30, blank=True)
    ambiente_fiscal = models.CharField(max_length=10, choices=[('SANDBOX', 'Sandbox'), ('PRODUCAO', 'Produção')], default='SANDBOX')
    provedor_fiscal = models.CharField(max_length=100, blank=True, help_text='Nome do adaptador aprovado; segredos ficam no ambiente.')
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization'], name='configuracao_org_unica')]
    def __str__(self):
        return 'Configuração da organização'
    def clean(self):
        super().clean()
        import re
        for field in ['prefixo_os','prefixo_orcamento','prefixo_contrato']:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,12}', getattr(self,field)):
                raise ValidationError({field:'Use apenas letras, números, hífen e sublinhado.'})

class ConfiguracaoNotificacaoEmail(Validado):
    servidor_smtp = models.CharField('Servidor SMTP (Host)', max_length=150, default='smtp.gmail.com')
    porta_smtp = models.PositiveIntegerField('Porta SMTP', default=587)
    email_remetente = models.EmailField('E-mail do Remetente', max_length=120, default='notificacoes@vertyx.com.br')
    nome_remetente = models.CharField('Nome de Exibição do Remetente', max_length=100, default='VERTYX Notificações')
    usuario_smtp = models.CharField('Usuário SMTP', max_length=120, blank=True)
    senha_smtp = models.CharField('Senha / App Password SMTP', max_length=120, blank=True)
    usar_tls = models.BooleanField('Usar TLS (STARTTLS)', default=True)
    usar_ssl = models.BooleanField('Usar SSL Direto', default=False)
    ativo = models.BooleanField('Disparos de E-mail Habilitados', default=True)

    notificar_conclusao_os = models.BooleanField('Notificar cliente ao concluir Ordem de Serviço (com Laudo e DANFSE)', default=True)
    notificar_preventiva = models.BooleanField('Notificar cliente sobre preventiva próxima do vencimento', default=True)
    dias_antecedencia_preventiva = models.PositiveSmallIntegerField('Dias de antecedência para alerta de preventiva', default=15)
    notificar_cobranca = models.BooleanField('Notificar cliente ao gerar cobrança / fatura financeira', default=True)

    ultimo_teste_em = models.DateTimeField(null=True, blank=True, editable=False)
    ultimo_teste_status = models.CharField(max_length=60, blank=True, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization'], name='config_email_notif_org_unica')]

    def __str__(self):
        return f'Notificações por E-mail ({self.email_remetente})'

    def clean(self):
        super().clean()
        if self.porta_smtp <= 0 or self.porta_smtp > 65535:
            raise ValidationError({'porta_smtp': 'Porta SMTP deve estar entre 1 e 65535.'})
        if self.usar_tls and self.usar_ssl:
            raise ValidationError('Selecione TLS ou SSL, não ambos simultaneamente.')

class Deposito(Nomeado):
    endereco = models.CharField(max_length=255, blank=True)

class SaldoDeposito(Validado):
    deposito = models.ForeignKey(Deposito, on_delete=models.PROTECT)
    peca = models.ForeignKey('gestao.Peca', on_delete=models.PROTECT)
    quantidade = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['deposito', 'peca'], name='saldo_deposito_peca'), models.CheckConstraint(check=Q(quantidade__gte=0), name='saldo_deposito_positivo')]
    def save(self, *args, **kwargs):
        if not getattr(self, '_service_write', False):
            raise ValidationError('Saldo por depósito exige serviço transacional.')
        return super().save(*args, **kwargs)

class CategoriaPeca(Nomeado):
    pass

class Chamado(Validado):
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.PROTECT)
    unidade = models.ForeignKey('clientes.Unidade', on_delete=models.PROTECT, null=True, blank=True)
    equipamento = models.ForeignKey('equipamentos.Equipamento', on_delete=models.PROTECT, null=True, blank=True)
    contrato = models.ForeignKey('gestao.Contrato', on_delete=models.PROTECT, null=True, blank=True)
    titulo = models.CharField('Título', max_length=180)
    descricao = models.TextField('Descrição')
    prioridade = models.CharField(max_length=10, choices=PRIORIDADES, default='NORMAL')
    prioridade_solicitada = models.CharField(max_length=10, choices=PRIORIDADES, blank=True)
    solicitante = models.CharField(max_length=150, blank=True)
    telefone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    origem = models.CharField(max_length=15, blank=True, choices=[('INTERNO', 'Interno'), ('PORTAL', 'Portal do cliente'), ('TELEFONE', 'Telefone'), ('EMAIL', 'E-mail'), ('PRESENCIAL', 'Presencial'), ('OUTRO', 'Outro')])
    tipo = models.CharField(max_length=30, choices=TIPOS_SERVICO, blank=True)
    tecnico = models.ForeignKey('contas.Tecnico', on_delete=models.PROTECT, null=True, blank=True)
    agendada_para = models.DateTimeField(null=True, blank=True)
    agendada_ate = models.DateTimeField(null=True, blank=True)
    prazo = models.DateTimeField(null=True, blank=True)
    sla_horas = models.PositiveIntegerField(null=True, blank=True, editable=False)
    observacoes_internas = models.TextField(blank=True)
    request_id = models.UUIDField(null=True, blank=True, editable=False)
    status = models.CharField(max_length=20, choices=[('ABERTO', 'Aberto'), ('TRIAGEM', 'Triagem'), ('AG_AGENDAMENTO', 'Aguardando agendamento'), ('AGENDADO', 'Agendado'), ('CONVERTIDO', 'Convertido em OS'), ('EM_ATENDIMENTO', 'Em atendimento'), ('AG_PECA', 'Aguardando peça'), ('AG_APROVACAO', 'Aguardando aprovação'), ('AG_CLIENTE', 'Aguardando cliente'), ('CONCLUIDO', 'Concluído'), ('CANCELADO', 'Cancelado')], default='ABERTO', editable=False)
    ordem = models.OneToOneField('ordens.OrdemServico', on_delete=models.PROTECT, null=True, blank=True, editable=False)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['-criado_em']
        constraints = [models.UniqueConstraint(fields=['organization', 'request_id'], name='chamado_org_request_unico')]
    @property
    def numero(self):
        return f'CH-{self.pk:06d}' if self.pk else 'Novo chamado'
    def __str__(self):
        return f'CH-{self.pk:06d} · {self.titulo}' if self.pk else 'Novo chamado'
    def clean(self):
        super().clean()
        from gestao.models import validar_cliente
        validar_cliente(self)
        if self.contrato_id and self.contrato.cliente_id != self.cliente_id:
            raise ValidationError({'contrato': 'Contrato pertence a outro cliente.'})
        if self.agendada_ate and (not self.agendada_para or self.agendada_ate <= self.agendada_para):
            raise ValidationError({'agendada_ate': 'Término deve ser posterior ao início.'})
        if self.tecnico_id and not self.tecnico.ativo:
            raise ValidationError({'tecnico': 'Técnico inativo.'})
    def save(self, *args, **kwargs):
        if self.pk and not getattr(self, '_service_write', False):
            old = type(self).objects.get(pk=self.pk)
            if self.status != old.status or self.ordem_id != old.ordem_id:
                raise ValidationError('Use o fluxo auditado do chamado para alterar situação/OS.')
        return super().save(*args, **kwargs)

class ModeloChecklist(Nomeado):
    itens = models.JSONField(default=list, validators=[checklist_valido], help_text='Lista JSON: [{"item":"Inspeção","obrigatorio":true}].')
    categoria = models.ForeignKey('equipamentos.CategoriaEquipamento', on_delete=models.PROTECT, null=True, blank=True)
    tipo_equipamento = models.CharField(max_length=100, blank=True)
    fabricante = models.CharField(max_length=150, blank=True)
    modelo = models.CharField(max_length=150, blank=True)
    tipo_servico = models.CharField(max_length=30, choices=TIPOS_SERVICO, blank=True)

class PlanoPreventivo(Nomeado):
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.PROTECT)
    unidade = models.ForeignKey('clientes.Unidade', on_delete=models.PROTECT, null=True, blank=True)
    tipo_manutencao = models.CharField(max_length=30, choices=TIPOS_SERVICO, blank=True)
    equipe = models.ManyToManyField('contas.Tecnico', through='PlanoTecnico', blank=True, related_name='planos_auxiliares')
    contrato = models.ForeignKey('gestao.Contrato', on_delete=models.PROTECT, null=True, blank=True)
    equipamentos = models.ManyToManyField('equipamentos.Equipamento', through='PlanoEquipamento')
    checklist = models.ForeignKey(ModeloChecklist, on_delete=models.PROTECT, null=True, blank=True)
    tecnico = models.ForeignKey('contas.Tecnico', on_delete=models.PROTECT, null=True, blank=True)
    periodicidade = models.CharField(max_length=15, choices=[('DIARIO', 'Diário'), ('SEMANAL', 'Semanal'), ('QUINZENAL', 'Quinzenal'), ('MENSAL', 'Mensal'), ('BIMESTRAL', 'Bimestral'), ('TRIMESTRAL', 'Trimestral'), ('QUADRIMESTRAL', 'Quadrimestral'), ('SEMESTRAL', 'Semestral'), ('ANUAL', 'Anual'), ('PERSONALIZADA', 'Personalizada')], default='MENSAL')
    intervalo_dias = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    inicio = models.DateField(default=timezone.localdate)
    fim = models.DateField()
    ultima_execucao = models.DateField(null=True, blank=True, editable=False)
    observacoes = models.TextField(blank=True)
    def clean(self):
        super().clean()
        if self.unidade_id and self.unidade.cliente_id != self.cliente_id:
            raise ValidationError({'unidade': 'Unidade pertence a outro cliente.'})
        if self.inicio and self.fim and self.fim < self.inicio:
            raise ValidationError({'fim': 'Fim anterior ao início.'})
        if self.periodicidade == 'PERSONALIZADA' and not self.intervalo_dias:
            raise ValidationError({'intervalo_dias': 'Informe o intervalo personalizado.'})
        if self.contrato_id and (self.contrato.cliente_id != self.cliente_id or not self.contrato.data_inicial <= self.inicio <= self.fim <= self.contrato.data_final):
            raise ValidationError({'contrato': 'Cliente/vigência devem estar dentro do contrato.'})
        if self.pk and self.ocorrencias.exists():
            old = type(self).objects.get(pk=self.pk)
            if any(getattr(old, field) != getattr(self, field) for field in ['cliente_id', 'contrato_id', 'inicio', 'fim', 'periodicidade', 'intervalo_dias']):
                raise ValidationError('Plano já gerado preserva cliente/vigência/periodicidade; desative e crie outro.')
    @property
    def proxima_execucao(self):
        if not self.ativo:
            return None
        item = self.ocorrencias.filter(cancelada=False).exclude(ordem__status__in=ENCERRADAS).order_by('data').first()
        return item.data if item else None

class PlanoEquipamento(Validado):
    plano = models.ForeignKey(PlanoPreventivo, on_delete=models.PROTECT)
    equipamento = models.ForeignKey('equipamentos.Equipamento', on_delete=models.PROTECT)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['plano', 'equipamento'], name='plano_equipamento_unico')]
    def clean(self):
        super().clean()
        if self.equipamento.cliente_id != self.plano.cliente_id or self.plano.contrato_id and not self.plano.contrato.equipamentos.filter(pk=self.equipamento_id).exists():
            raise ValidationError('Equipamento fora da cobertura do plano/contrato.')
        if self.plano.unidade_id and self.equipamento.unidade_id != self.plano.unidade_id:
            raise ValidationError('Equipamento fora da unidade do plano.')

class PlanoTecnico(Validado):
    plano = models.ForeignKey(PlanoPreventivo, on_delete=models.PROTECT)
    tecnico = models.ForeignKey('contas.Tecnico', on_delete=models.PROTECT)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['plano', 'tecnico'], name='plano_tecnico_unico')]
    def clean(self):
        super().clean()
        if not self.tecnico.ativo:
            raise ValidationError('Técnico inativo.')

class PreventivaProgramada(Validado):
    plano = models.ForeignKey(PlanoPreventivo, on_delete=models.PROTECT, related_name='ocorrencias')
    equipamento = models.ForeignKey('equipamentos.Equipamento', on_delete=models.PROTECT)
    data = models.DateField()
    ordem = models.OneToOneField('ordens.OrdemServico', on_delete=models.PROTECT, null=True, blank=True, editable=False)
    cancelada = models.BooleanField(default=False)
    class Meta:
        ordering = ['data', 'pk']
        constraints = [models.UniqueConstraint(fields=['plano', 'equipamento', 'data'], name='preventiva_plano_equip_data')]
    def __str__(self):
        return f'{self.equipamento.nome} · {self.data}'
    @property
    def situacao(self):
        if self.ordem_id and self.ordem.status in ['FINALIZADA', 'APROVADA', 'FATURADA']:
            return 'CONCLUIDA'
        if self.cancelada or not self.plano.ativo or self.ordem_id and self.ordem.status == 'CANCELADA':
            return 'CANCELADA'
        return 'AGENDADA' if self.ordem_id else 'ATRASADA' if self.data < timezone.localdate() else 'PENDENTE'
    def clean(self):
        super().clean()
        if not self.plano.equipamentos.filter(pk=self.equipamento_id).exists() or not self.plano.inicio <= self.data <= self.plano.fim:
            raise ValidationError('Equipamento/data fora do plano.')

class Fluxo(Validado):
    status = models.CharField(max_length=15, default='RASCUNHO', editable=False)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    observacoes = models.TextField(blank=True)
    class Meta:
        abstract = True
        ordering = ['-criado_em']
    def save(self, *args, **kwargs):
        if not getattr(self, '_service_write', False):
            old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
            if self.status != 'RASCUNHO' or old and old.status != 'RASCUNHO':
                raise ValidationError('Registro fora de rascunho exige serviço transacional.')
        return super().save(*args, **kwargs)
    @property
    def total(self):
        return sum((row.total for row in self.itens.all()), ZERO) if self.pk else ZERO
    def get_status_display(self):
        return {'RASCUNHO':'Rascunho','APROVADA':'Aprovada','PEDIDO':'Pedido','RECEBIDA':'Recebida','CONFIRMADA':'Confirmada','CANCELADA':'Cancelada'}.get(self.status,self.status)

class Compra(Fluxo):
    previsao_recebimento = models.DateField(null=True, blank=True)
    relatorio_estoque = models.ForeignKey('RelatorioEstoque', on_delete=models.PROTECT, null=True, blank=True, editable=False)
    pedido_relatorio_id = models.UUIDField(null=True, blank=True, editable=False)
    selecao_relatorio = models.JSONField(default=dict, blank=True, editable=False)
    fornecedor = models.ForeignKey('gestao.Fornecedor', on_delete=models.PROTECT, null=True, blank=True)
    deposito = models.ForeignKey(Deposito, on_delete=models.PROTECT)
    necessidade = models.TextField('Motivo da solicitação')
    vencimento = models.DateField()
    conta = models.OneToOneField('gestao.Lancamento', on_delete=models.PROTECT, null=True, blank=True, editable=False, related_name='compra_origem')
    def __str__(self):
        return f'COMP-{self.pk:06d}' if self.pk else 'Nova compra'
    class Meta(Fluxo.Meta):
        constraints = [models.UniqueConstraint(fields=['organization','pedido_relatorio_id'],condition=Q(pedido_relatorio_id__isnull=False),name='compra_relatorio_request_unico')]

class ItemCompra(Validado):
    conversao=models.JSONField(default=dict,blank=True,editable=False)
    lote = models.CharField(max_length=100, blank=True)
    fabricacao = models.DateField(null=True, blank=True)
    validade = models.DateField(null=True, blank=True)
    documento_entrada = models.CharField(max_length=180, blank=True)
    relatorio_item = models.ForeignKey('ItemRelatorioEstoque', on_delete=models.PROTECT, null=True, blank=True, editable=False)
    compra = models.ForeignKey(Compra, on_delete=models.PROTECT, related_name='itens')
    peca = models.ForeignKey('gestao.Peca', on_delete=models.PROTECT)
    quantidade = models.DecimalField(max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal('.001'))])
    custo_unitario = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(ZERO)])
    @property
    def quantidade_recebida(self):
        if not self.pk:
            return ZERO
        value = self.recebimentos.aggregate(total=Sum('quantidade'))['total']
        return self.quantidade if value is None and self.compra.status == 'RECEBIDA' else value or ZERO
    @property
    def quantidade_pendente(self):
        return max(ZERO, self.quantidade - self.quantidade_recebida)
    @property
    def total(self):
        return (self.quantidade * self.custo_unitario).quantize(Decimal('.01'))
    def clean(self):
        super().clean()
        if Compra.objects.get(pk=self.compra_id).status != 'RASCUNHO' and not getattr(self, '_receiving_write', False):
            raise ValidationError('Itens somente em solicitação rascunho.')
        if getattr(self, '_receiving_write', False):
            old = type(self).objects.get(pk=self.pk)
            if any(getattr(old, field) != getattr(self, field) for field in ['compra_id','peca_id','quantidade','custo_unitario','relatorio_item_id','conversao']):
                raise ValidationError('Recebimento preserva produto, quantidade e custo aprovados.')
        if self.fabricacao and self.validade and self.fabricacao > self.validade:
            raise ValidationError('Fabricação posterior à validade.')

class CotacaoCompra(Validado):
    compra = models.ForeignKey(Compra, on_delete=models.PROTECT, related_name='cotacoes')
    fornecedor = models.ForeignKey('gestao.Fornecedor', on_delete=models.PROTECT)
    valor = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('.01'))])
    validade = models.DateField()
    prazo = models.CharField(max_length=100, blank=True)
    observacoes = models.TextField(blank=True)
    def __str__(self):
        return f'{self.fornecedor} · R$ {self.valor}'
    def clean(self):
        super().clean()
        if Compra.objects.get(pk=self.compra_id).status != 'RASCUNHO':
            raise ValidationError('Cotação exige compra em rascunho.')

class Venda(Fluxo):
    public_id = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    status = models.CharField(max_length=30, default='RASCUNHO', editable=False)
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.PROTECT, null=True, blank=True)
    sessao_caixa = models.ForeignKey('SessaoCaixa', on_delete=models.PROTECT, null=True, blank=True, editable=False)
    filial = models.ForeignKey('platform_core.FilialEmpresa', on_delete=models.PROTECT, null=True, blank=True)
    centro_custo = models.ForeignKey('gestao.CentroCusto', on_delete=models.PROTECT, null=True, blank=True)
    orcamento_origem = models.OneToOneField('gestao.Orcamento', on_delete=models.PROTECT, null=True, blank=True, editable=False)
    desconto_global = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    acrescimo = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    snapshot = models.JSONField(default=dict, blank=True, editable=False)
    finalizacao_id = models.UUIDField(null=True, blank=True, editable=False)
    finalizada_em = models.DateTimeField(null=True,blank=True,editable=False)
    criacao_id = models.UUIDField(null=True,blank=True,editable=False)
    dados_criacao = models.JSONField(default=dict,blank=True,editable=False)
    finalizacao_dados = models.JSONField(default=dict, blank=True, editable=False)
    vendedor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='vendas')
    deposito = models.ForeignKey(Deposito, on_delete=models.PROTECT)
    vencimento = models.DateField()
    conta = models.OneToOneField('gestao.Lancamento', on_delete=models.PROTECT, null=True, blank=True, editable=False, related_name='venda_origem')
    def __str__(self):
        return f'VENDA-{self.pk:06d}' if self.pk else 'Nova venda'
    def clean(self):
        super().clean()
        if self.vendedor_id and not self.vendedor.memberships.filter(organization=self.organization, status='ATIVO').exists():
            raise ValidationError({'vendedor': 'Vendedor precisa de vínculo ativo na organização.'})

    @property
    def total(self):
        return super().total - self.desconto_global + self.acrescimo

    def save(self,*args,**kwargs):
        if self._state.adding and self.public_id is None:
            self.public_id=uuid.uuid4()
        return super().save(*args,**kwargs)

    def get_status_display(self):
        return {'AG_PAGAMENTO':'Aguardando pagamento','ORCAMENTO':'Orçamento comercial','FINALIZADA':'Finalizada','PARCIALMENTE_DEVOLVIDA':'Parcialmente devolvida','DEVOLVIDA':'Devolvida'}.get(self.status,super().get_status_display())

    class Meta(Fluxo.Meta):
        constraints=[models.UniqueConstraint(fields=['organization','criacao_id'],condition=Q(criacao_id__isnull=False),name='venda_criacao_request_org'),models.UniqueConstraint(fields=['organization','finalizacao_id'],condition=Q(finalizacao_id__isnull=False),name='venda_finalizacao_request_org')]

class ItemVenda(Validado):
    conversao = models.JSONField(default=dict, blank=True, editable=False)
    movimento = models.OneToOneField('gestao.Movimentacao', on_delete=models.PROTECT, null=True, blank=True, editable=False)
    custo_total = models.DecimalField(max_digits=30, decimal_places=5, null=True, blank=True, editable=False)
    venda = models.ForeignKey(Venda, on_delete=models.PROTECT, related_name='itens')
    peca = models.ForeignKey('gestao.Peca', on_delete=models.PROTECT)
    quantidade = models.DecimalField(max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal('.001'))])
    valor_unitario = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(ZERO)])
    desconto = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    custo_unitario = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, editable=False)
    @property
    def total(self):
        return (self.quantidade * self.valor_unitario).quantize(Decimal('.01')) - self.desconto
    @property
    def margem(self):
        from decimal import ROUND_HALF_UP
        cost = self.custo_total if self.custo_total is not None else self.quantidade * self.custo_unitario
        return self.total - cost.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    def clean(self):
        super().clean()
        if Venda.objects.get(pk=self.venda_id).status != 'RASCUNHO' and not getattr(self, '_service_write', False):
            raise ValidationError('Itens somente em venda rascunho.')
        if self.quantidade is not None and self.valor_unitario is not None and self.desconto is not None and self.total < 0:
            raise ValidationError({'desconto': 'Desconto excede o valor do item.'})

    def delete(self,*args,**kwargs):
        self.clean()
        return super().delete(*args,**kwargs)

class ContaBancaria(Nomeado):
    banco = models.CharField(max_length=100, blank=True)
    agencia = models.CharField(max_length=30, blank=True)
    numero = models.CharField(max_length=40, blank=True)
    titular = models.CharField(max_length=150, blank=True)
    tipo = models.CharField(max_length=10, choices=[('BANCO', 'Banco'), ('CAIXA', 'Caixa'), ('CARTAO', 'Cartão')], default='BANCO')
    deposito_padrao = models.ForeignKey('operacao.Deposito', on_delete=models.PROTECT, null=True, blank=True, related_name='caixas_padrao')
    saldo_inicial = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    permite_saldo_negativo = models.BooleanField(default=False)
    @property
    def saldo(self):
        amounts = self.movimentos.aggregate(entrada=Sum('valor', filter=Q(tipo='ENTRADA')), saida=Sum('valor', filter=Q(tipo='SAIDA')))
        return self.saldo_inicial + (amounts['entrada'] or ZERO) - (amounts['saida'] or ZERO)
    def clean(self):
        super().clean()
        if self.pk and self.movimentos.exists() and type(self).objects.get(pk=self.pk).saldo_inicial != self.saldo_inicial:
            raise ValidationError({'saldo_inicial': 'Saldo de abertura já movimentado é preservado.'})

class MovimentoCaixa(RegistroImutavel):
    conta = models.ForeignKey(ContaBancaria, on_delete=models.PROTECT, related_name='movimentos')
    tipo = models.CharField(max_length=7, choices=[('ENTRADA', 'Entrada'), ('SAIDA', 'Saída')])
    valor = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('.01'))])
    data = models.DateField(default=timezone.localdate)
    descricao = models.CharField(max_length=255)
    referencia = models.UUIDField(default=uuid.uuid4, editable=False)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    origem = models.CharField(max_length=16, default='MANUAL', choices=[('MANUAL','Manual'),('LANCAMENTO','Pagamento/recebimento'),('TRANSFERENCIA','Transferência'),('CONCILIACAO','Conciliação'),('PDV','PDV'),('OUTRO','Outro')])
    categoria = models.ForeignKey('gestao.CategoriaFinanceira', on_delete=models.PROTECT, null=True, blank=True)
    centro_custo = models.ForeignKey('gestao.CentroCusto', on_delete=models.PROTECT, null=True, blank=True)
    lancamento = models.ForeignKey('gestao.Lancamento', on_delete=models.PROTECT, null=True, blank=True, related_name='movimentos_caixa')
    conta_contraparte = models.ForeignKey('operacao.ContaBancaria', on_delete=models.PROTECT, null=True, blank=True, related_name='transferencias_relacionadas')
    documento_ref = models.CharField(max_length=150, blank=True)
    class Meta:
        ordering = ['-data', '-pk']
        constraints = [models.UniqueConstraint(fields=['conta', 'referencia'], name='movimento_caixa_referencia')]

    def clean(self):
        super().clean()
        expected = 'RECEITA' if self.tipo == 'ENTRADA' else 'DESPESA'
        if self.categoria_id and self.categoria.natureza not in ['AMBAS', expected]:
            raise ValidationError({'categoria': 'Natureza da categoria não corresponde à movimentação.'})

class Pagamento(RegistroImutavel):
    @property
    def controle_caixa(self):
        return str(self.conta) if self.conta_id else 'Não bancarizado — sem conta informada'

    @property
    def origem_financeira(self):
        return self.lancamento.origem_financeira
    lancamento = models.ForeignKey('gestao.Lancamento', on_delete=models.PROTECT, related_name='pagamentos')
    valor = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('.01'))])
    desconto = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    juros = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    multa = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    data = models.DateField()
    conta = models.ForeignKey(ContaBancaria, on_delete=models.PROTECT, null=True, blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    observacoes = models.TextField(blank=True)
    request_id = models.UUIDField(default=uuid.uuid4, editable=False)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'request_id'], name='pagamento_request_org')]

class Parcela(Validado):
    origem = models.ForeignKey('gestao.Lancamento', on_delete=models.PROTECT, related_name='parcelas')
    lancamento = models.OneToOneField('gestao.Lancamento', on_delete=models.PROTECT, related_name='parcela_origem')
    numero = models.PositiveIntegerField()
    class Meta:
        constraints = [models.UniqueConstraint(fields=['origem', 'numero'], name='parcela_origem_numero')]

class ExtratoBancario(Validado):
    conta = models.ForeignKey(ContaBancaria, on_delete=models.PROTECT)
    identificador = models.CharField('Identificador externo do extrato', max_length=150)
    data = models.DateField()
    valor = models.DecimalField('Valor assinado: entrada + / saída -', max_digits=14, decimal_places=2)
    descricao = models.CharField(max_length=255)
    movimento = models.OneToOneField(MovimentoCaixa, on_delete=models.PROTECT, null=True, blank=True, editable=False)
    ignorado = models.BooleanField(default=False)
    ignorado_em = models.DateTimeField(null=True, blank=True, editable=False)
    ignorado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, editable=False, related_name='extratos_ignorados')
    class Meta:
        constraints = [models.UniqueConstraint(fields=['conta', 'identificador'], name='extrato_conta_id_unico')]
    def __str__(self):
        return f'{self.data} · {self.descricao}'

class DocumentoFiscal(Validado):
    estabelecimento = models.ForeignKey('platform_core.FilialEmpresa', on_delete=models.PROTECT, null=True, blank=True)
    configuracao_fiscal = models.ForeignKey('operacao.ConfiguracaoFiscal', on_delete=models.PROTECT, null=True, blank=True)
    credencial_fiscal = models.ForeignKey('operacao.CredencialFiscal', on_delete=models.PROTECT, null=True, blank=True, editable=False)
    serie_fiscal = models.ForeignKey('operacao.SerieFiscal', on_delete=models.PROTECT, null=True, blank=True)
    regra_fiscal = models.ForeignKey('operacao.RegraTributariaFiscal', on_delete=models.PROTECT, null=True, blank=True, editable=False)
    modelo = models.CharField(max_length=10, blank=True, editable=False)
    chave = models.CharField(max_length=80, blank=True, editable=False)
    snapshot_fiscal = models.JSONField(default=dict, blank=True, editable=False)
    autorizado_em = models.DateTimeField(null=True, blank=True, editable=False)
    tipo = models.CharField(max_length=5, choices=[('NFSE', 'NFS-e'), ('NFE', 'NF-e'), ('NFCE', 'NFC-e')])
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.PROTECT, null=True, blank=True)
    lancamento = models.ForeignKey('gestao.Lancamento', on_delete=models.PROTECT, null=True, blank=True)
    ordem = models.ForeignKey('ordens.OrdemServico', on_delete=models.PROTECT, null=True, blank=True)
    venda = models.ForeignKey(Venda, on_delete=models.PROTECT, null=True, blank=True)
    numero = models.CharField(max_length=30, blank=True, editable=False)
    serie = models.CharField(max_length=10, blank=True)
    status = models.CharField(max_length=15, default='RASCUNHO', editable=False)
    protocolo = models.CharField(max_length=150, blank=True, editable=False)
    ambiente = models.CharField(max_length=12, choices=[('SANDBOX', 'Sandbox legado'), ('SIMULACAO', 'Simulação sem valor fiscal'), ('HOMOLOGACAO', 'Homologação oficial não habilitada'), ('PRODUCAO', 'Produção')], default='SANDBOX')
    municipio = models.CharField(max_length=10, blank=True)
    codigo_servico = models.CharField(max_length=30, blank=True)
    impostos = models.JSONField(default=dict, blank=True, help_text='Valores/classificações fornecidos pelo contador; nenhuma regra fiscal presumida.')
    retencoes = models.JSONField(default=dict, blank=True)
    descricao = models.TextField('Descrição fiscal')
    valor = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('.01'))])
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    def __str__(self):
        return f'{self.get_tipo_display()} · {self.numero or "Rascunho"} #{self.pk}'
    def get_status_display(self):
        return {'RASCUNHO':'Rascunho','ENVIADA':'Enviada','AUTORIZADA':'Autorizada','REJEITADA':'Rejeitada','CANCELADA':'Cancelada'}.get(self.status,self.status)
    def clean(self):
        super().clean()
        if self.configuracao_fiscal_id:
            config = self.configuracao_fiscal
            if self.estabelecimento_id != config.estabelecimento_id or self.tipo != config.tipo or self.ambiente != config.ambiente:
                raise ValidationError('Documento e configuração fiscal devem coincidir em estabelecimento, tipo e ambiente.')
        elif self.cliente_id is None:
            raise ValidationError('Documento legado exige cliente; consumidor não identificado usa o novo fluxo NFC-e.')
        for origin in [self.lancamento if self.lancamento_id else None, self.ordem if self.ordem_id else None, self.venda if self.venda_id else None]:
            if origin and origin.cliente_id != self.cliente_id:
                raise ValidationError('Cliente fiscal deve coincidir com a origem.')
        for data in [self.impostos, self.retencoes]:
            if not isinstance(data, dict) or len(data) > 30 or any(any(word in str(key).lower() for word in ['senha', 'token', 'secret', 'certificado', 'password']) for key in data):
                raise ValidationError('Use somente classificações/valores fiscais, nunca segredos.')
    def save(self, *args, **kwargs):
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if self.pk and (self.configuracao_fiscal_id or old and old.configuracao_fiscal_id) and not getattr(self, '_service_write', False):
            raise ValidationError('Documento da fundação fiscal exige serviço auditado; snapshot não pode ser editado silenciosamente.')
        if old and old.configuracao_fiscal_id and any(getattr(self, field) != getattr(old, field) for field in ['snapshot_fiscal', 'valor', 'tipo', 'ambiente', 'modelo', 'estabelecimento_id', 'configuracao_fiscal_id', 'credencial_fiscal_id', 'serie_fiscal_id', 'regra_fiscal_id', 'venda_id', 'ordem_id', 'cliente_id']):
            raise ValidationError('Identidade, origem e snapshot fiscal são imutáveis; use evento ou novo fluxo explícito.')
        if (self.status != 'RASCUNHO' or old and old.status != 'RASCUNHO') and not getattr(self, '_service_write', False):
            raise ValidationError('Estado fiscal só pode mudar pelo provedor validado.')
        return super().save(*args, **kwargs)

class Documento(Validado):
    peca=models.ForeignKey('gestao.Peca',on_delete=models.PROTECT,null=True,blank=True,related_name='documentos')
    funcionario=models.ForeignKey('operacao.Funcionario',on_delete=models.PROTECT,null=True,blank=True,related_name='documentos')
    compra=models.ForeignKey('operacao.Compra',on_delete=models.PROTECT,null=True,blank=True,related_name='documentos')
    venda=models.ForeignKey('operacao.Venda',on_delete=models.PROTECT,null=True,blank=True,related_name='documentos')
    from ordens.storage import private_storage
    titulo = models.CharField('Título', max_length=180)
    tipo = models.CharField(max_length=15, choices=[('PDF', 'PDF'), ('IMAGEM', 'Imagem'), ('CONTRATO', 'Contrato'), ('LAUDO', 'Laudo'), ('CERTIFICADO', 'Certificado'), ('MANUAL', 'Manual'), ('FISCAL_XML', 'XML fiscal'), ('FISCAL_PDF', 'PDF fiscal'), ('VIDEO', 'Vídeo')], default='PDF')
    arquivo = models.FileField(upload_to=privado, storage=private_storage, max_length=500)
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.PROTECT, null=True, blank=True)
    unidade = models.ForeignKey('clientes.Unidade', on_delete=models.PROTECT, null=True, blank=True)
    tecnico = models.ForeignKey('contas.Tecnico', on_delete=models.PROTECT, null=True, blank=True)
    fornecedor = models.ForeignKey('gestao.Fornecedor', on_delete=models.PROTECT, null=True, blank=True, related_name='documentos_tecnicos')
    chamado = models.ForeignKey(Chamado, on_delete=models.PROTECT, null=True, blank=True)
    categoria = models.CharField(max_length=20, blank=True, choices=[('CONTRATO', 'Contrato'), ('CERTIFICADO', 'Certificado'), ('LAUDO', 'Laudo'), ('MANUAL', 'Manual'), ('NOTA_FISCAL', 'Nota fiscal'), ('GARANTIA', 'Garantia'), ('ART_TRT', 'ART/TRT'), ('CALIBRACAO', 'Calibração'), ('LICENCA', 'Licença'), ('TECNICO', 'Documento técnico'), ('OUTRO', 'Outros')])
    emissao = models.DateField(null=True, blank=True)
    validade = models.DateField(null=True, blank=True)
    observacoes = models.TextField(blank=True)
    equipamento = models.ForeignKey('equipamentos.Equipamento', on_delete=models.PROTECT, null=True, blank=True)
    ordem = models.ForeignKey('ordens.OrdemServico', on_delete=models.PROTECT, null=True, blank=True)
    contrato = models.ForeignKey('gestao.Contrato', on_delete=models.PROTECT, null=True, blank=True)
    orcamento = models.ForeignKey('gestao.Orcamento', on_delete=models.PROTECT, null=True, blank=True)
    fiscal = models.ForeignKey(DocumentoFiscal, on_delete=models.PROTECT, null=True, blank=True)
    disponivel_portal = models.BooleanField('Disponível no portal do cliente', default=False)
    sha256 = models.CharField(max_length=64, editable=False)
    enviado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['-criado_em']
    def __str__(self):
        return self.titulo
    def clean(self):
        super().clean()
        for name in ['unidade', 'equipamento', 'ordem', 'contrato', 'orcamento', 'fiscal', 'chamado']:
            origin = getattr(self, name) if getattr(self, name + '_id') else None
            if origin:
                if self.cliente_id and self.cliente_id != origin.cliente_id:
                    raise ValidationError({'cliente': 'Vínculos do documento pertencem a clientes diferentes.'})
                self.cliente = origin.cliente
        if self.funcionario_id and (self.disponivel_portal or any([self.cliente_id,self.unidade_id,self.equipamento_id,self.ordem_id,self.contrato_id,self.orcamento_id,self.fiscal_id,self.chamado_id,self.compra_id,self.venda_id,self.peca_id,self.fornecedor_id])):
            raise ValidationError('Documento de funcionário é privado do RH e não pode ser misturado com vínculos comerciais/portal.')
        if self.venda_id and self.cliente_id and self.venda.cliente_id!=self.cliente_id:
            raise ValidationError('Cliente do documento deve coincidir com a venda.')
        if self.venda_id and self.venda.cliente_id:
            self.cliente=self.venda.cliente
        if self.ordem_id and self.equipamento_id and self.ordem.equipamento_id != self.equipamento_id:
            raise ValidationError('Documento vinculado a equipamentos diferentes.')
        if self.disponivel_portal and not self.cliente_id:
            raise ValidationError('Compartilhamento exige cliente identificado.')
        if self.unidade_id and self.equipamento_id and self.equipamento.unidade_id != self.unidade_id:
            raise ValidationError('Documento vinculado a unidades diferentes.')
        if self.emissao and self.validade and self.validade < self.emissao:
            raise ValidationError({'validade': 'Validade anterior à emissão.'})

class PortalCliente(Validado):
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    cliente = models.ForeignKey('clientes.Cliente', on_delete=models.PROTECT)
    ativo = models.BooleanField(default=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'usuario', 'cliente'], name='portal_usuario_cliente_org')]
    def __str__(self):
        return f'{self.usuario} · {self.cliente}'
    def clean(self):
        super().clean()
        if not self.usuario.memberships.filter(organization=self.organization, role='CLIENTE', status='ATIVO').exists():
            raise ValidationError({'usuario': 'Portal exige membership CLIENTE ativo na organização.'})

class Notificacao(Validado):
    ocorrencia_estoque = models.ForeignKey('OcorrenciaEstoque', on_delete=models.PROTECT, null=True, blank=True)
    resolvida_em = models.DateTimeField(null=True, blank=True, editable=False)
    titulo = models.CharField(max_length=180)
    mensagem = models.TextField()
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    chave = models.CharField(max_length=150)
    link = models.CharField(max_length=255, blank=True)
    lida_em = models.DateTimeField(null=True, blank=True, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['-criado_em']
        constraints = [models.UniqueConstraint(fields=['organization', 'usuario', 'chave'], name='notificacao_destino_chave')]

class SaidaNotificacao(Validado):
    notificacao = models.OneToOneField(Notificacao, on_delete=models.PROTECT)
    canal = models.CharField(max_length=10, choices=[('EMAIL', 'E-mail'), ('WHATSAPP', 'WhatsApp')])
    status = models.CharField(max_length=15, default='NAO_CONFIGURADO', editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)

class ArtigoConhecimento(Validado):
    titulo = models.CharField('Título', max_length=180)
    fabricante = models.CharField(max_length=150, blank=True)
    modelo = models.CharField(max_length=150, blank=True)
    equipamento = models.ForeignKey('equipamentos.Equipamento', on_delete=models.PROTECT, null=True, blank=True)
    ordem = models.ForeignKey('ordens.OrdemServico', on_delete=models.PROTECT, null=True, blank=True)
    manual = models.ForeignKey(Documento, on_delete=models.PROTECT, null=True, blank=True)
    sintoma = models.TextField(blank=True)
    defeito = models.TextField(blank=True)
    solucao = models.TextField('Solução', blank=True)
    procedimento = models.TextField(blank=True)
    observacoes = models.TextField(blank=True)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['titulo']
    def __str__(self):
        return self.titulo
    def clean(self):
        super().clean()
        if self.equipamento_id and self.ordem_id and self.ordem.equipamento_id != self.equipamento_id:
            raise ValidationError('Equipamento diferente da OS de referência.')

class RecebimentoCompra(RegistroImutavel):
    compra = models.ForeignKey(Compra, on_delete=models.PROTECT, related_name='recebimentos')
    request_id = models.UUIDField()
    deposito = models.ForeignKey(Deposito, on_delete=models.PROTECT)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recebido_em = models.DateTimeField(default=timezone.now)
    documento = models.CharField('Documento/NF de referência', max_length=180, blank=True)
    parametros = models.JSONField(default=dict)
    class Meta:
        ordering = ['recebido_em', 'pk']
        constraints = [models.UniqueConstraint(fields=['organization', 'request_id'], name='recebimento_org_request_unico')]

class ItemRecebimentoCompra(RegistroImutavel):
    recebimento = models.ForeignKey(RecebimentoCompra, on_delete=models.PROTECT, related_name='itens')
    item = models.ForeignKey(ItemCompra, on_delete=models.PROTECT, related_name='recebimentos')
    movimentacao = models.OneToOneField('gestao.Movimentacao', on_delete=models.PROTECT)
    quantidade = models.DecimalField(max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal('0.001'))])
    custo_unitario = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(ZERO)])
    lote = models.CharField(max_length=100, blank=True)
    fabricacao = models.DateField(null=True, blank=True)
    validade = models.DateField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['recebimento', 'item'], name='recebimento_item_unico')]
    def clean(self):
        super().clean()
        if self.item.compra_id != self.recebimento.compra_id or self.movimentacao.peca_id != self.item.peca_id:
            raise ValidationError('Item/movimentação fora da compra recebida.')

class NecessidadePecaOS(Validado):
    ordem = models.ForeignKey('ordens.OrdemServico', on_delete=models.PROTECT, related_name='necessidades_pecas')
    peca = models.ForeignKey('gestao.Peca', on_delete=models.PROTECT)
    deposito = models.ForeignKey(Deposito, on_delete=models.PROTECT)
    quantidade = models.DecimalField(max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal('0.001'))])
    urgencia = models.CharField(max_length=10, choices=PRIORIDADES, default='NORMAL')
    observacoes = models.TextField(blank=True)
    resolvida_em = models.DateTimeField(null=True, blank=True, editable=False)
    compra = models.ForeignKey(Compra, on_delete=models.PROTECT, null=True, blank=True, editable=False)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['ordem', 'peca', 'deposito'], condition=Q(resolvida_em__isnull=True), name='necessidade_os_peca_aberta')]
    def save(self, *args, **kwargs):
        if not getattr(self, '_service_write', False):
            raise ValidationError('Necessidade exige fluxo auditado da OS.')
        return super().save(*args, **kwargs)

from .stock_models import LoteEstoque, MovimentacaoLote, ReservaEstoque, AlocacaoReservaEstoque, OcorrenciaEstoque, RelatorioEstoque, ItemRelatorioEstoque  # noqa: E402,F401
from .commercial_models import ConfiguracaoPDV, SessaoCaixa, EventoCaixa, PagamentoVenda, DevolucaoVenda, ItemDevolucaoVenda  # noqa: E402,F401
from .workforce_models import Departamento, JornadaTrabalho, Funcionario, EscalaFuncionario, MarcacaoPonto, SolicitacaoAjustePonto, DecisaoAjustePonto, FechamentoPonto  # noqa: E402,F401
from .workforce_models import ReferenciaJornadaPonto, SolicitacaoHoraExtra, DecisaoHoraExtra  # noqa: E402,F401
from .inventory_models import Inventario, ItemInventario, ContagemInventario, AjusteInventario, TransferenciaEstoque, EtapaTransferencia  # noqa: E402,F401
from .contract_workday_models import ConfiguracaoJornadaContrato, JornadaContrato, EventoJornadaContrato, OrdemJornadaContrato, CorrecaoJornadaContrato, DecisaoJornadaContrato  # noqa: E402,F401
from .seller_models import MetaVendedor, SolicitacaoDescontoPDV  # noqa: E402,F401
from .pdv_offline_models import SincronizacaoPDV  # noqa: E402,F401
from .fiscal_models import PerfilFiscalEstabelecimento, ConfiguracaoFiscal, CredencialFiscal, VinculoCredencialFiscal, SerieFiscal, RegraTributariaFiscal, DetalhesNFSe, DetalhesNFe, DetalhesNFCe, SolicitacaoFiscal, ReservaNumeracaoFiscal, TentativaFiscal, EventoFiscal  # noqa: E402,F401
from .nfce_models import ConfiguracaoNFCe, TributacaoProdutoNFCe, ArtefatoNFCe, PagamentoFiscalNFCe  # noqa: E402,F401
from .import_models import LoteImportacao, LinhaImportacao  # noqa: E402,F401
from .banking_models import IntegracaoBancaria, CobrancaBancaria, WebhookBancarioEvento  # noqa: E402,F401
from .dfe_models import ConfiguracaoDFeRecebimento, NotaFiscalRecebida, ManifestacaoDFeEvento  # noqa: E402,F401
