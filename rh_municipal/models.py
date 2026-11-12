import uuid
import hashlib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from platform_core.context import current_organization

from platform_core.models import ERPCatalog, MembershipERP, OrganizationERP, OrganizationMembership
from platform_core.tenancy import OrganizationOwned, TenantManager, TenantOwned, TenantQuerySet


PRODUCT_CODE = 'gestao-publica'
PUBLIC_PERMISSION_CODES = {
    'dashboard.view', 'executive.dashboard.view', 'structure.view', 'structure.manage',
    'access.view', 'access.manage', 'invite.create', 'invite.revoke',
    'servers.view', 'servers.manage', 'rh.access', 'profile.manage',
    'planning.view', 'planning.manage', 'budget.view', 'budget.manage',
    'accounting.view', 'accounting.manage', 'treasury.view', 'treasury.manage',
    'compras.view', 'compras.manage', 'compras.approve',
    'cotacao.view', 'cotacao.manage', 'licitacao.view', 'licitacao.manage',
    'licitacao.judge', 'licitacao.homologate', 'contract.view',
    'contract.manage', 'contract.approve', 'contract.execute',
    'contract.documents', 'price_registry.view', 'price_registry.manage',
    'rh.view', 'rh.manage', 'rh.documents', 'rh.approve',
    'ponto.view', 'ponto.register', 'ponto.adjust', 'ponto.approve',
    'ponto.close', 'ponto.reopen', 'ponto.report', 'folha.view',
    'folha.manage', 'servidor.portal',
    'patrimonio.view', 'patrimonio.manage', 'patrimonio.transfer',
    'patrimonio.inventory', 'patrimonio.writeoff', 'patrimonio.report',
    'almoxarifado.view', 'almoxarifado.manage', 'almoxarifado.request',
    'almoxarifado.approve', 'almoxarifado.issue', 'almoxarifado.transfer',
    'almoxarifado.inventory', 'frotas.view', 'frotas.manage',
    'frotas.dispatch', 'frotas.fuel', 'frotas.maintenance',
    'frotas.documents', 'frotas.report',
    'protocol.view', 'protocol.manage', 'protocol.assign',
    'process.view', 'process.manage', 'process.dispatch', 'process.analyze',
    'process.close', 'process.signature', 'process.documents',
    'process.restricted', 'process.sensitive',
    'transparency.view', 'transparency.manage', 'transparency.publish',
    'ouvidoria.view', 'ouvidoria.manage', 'ouvidoria.respond',
    'esic.view', 'esic.manage', 'esic.respond', 'esic.appeal',
    'contribuinte.view', 'contribuinte.manage',
    'imovel.view', 'imovel.manage', 'tributo.view', 'tributo.manage',
    'arrecadacao.view', 'arrecadacao.manage', 'divida.view', 'divida.manage',
    'nfse.view', 'nfse.manage',
    'saude.dashboard.view', 'saude.units.view', 'saude.units.manage',
    'saude.primary.view', 'saude.primary.manage',
    'saude.teams.view', 'saude.teams.manage', 'saude.professionals.view',
    'saude.professionals.manage', 'saude.patients.view', 'saude.patients.manage',
    'saude.records.view', 'saude.records.manage', 'saude.records.sensitive',
    'saude.records.create', 'saude.records.edit',
    'saude.appointments.view', 'saude.appointments.manage',
    'saude.field.view', 'saude.field.manage', 'saude.pharmacy.view',
    'saude.pharmacy.manage', 'saude.dispense', 'saude.sanitary.view',
    'saude.sanitary.manage', 'saude.epidemiology.view',
    'saude.epidemiology.manage', 'saude.regulation.view',
    'saude.regulation.manage', 'saude.regulation.prioritize',
    'saude.tfd.view', 'saude.tfd.manage', 'saude.transport.view',
    'saude.transport.manage', 'saude.production.view', 'saude.production.manage',
    'educacao.dashboard.view', 'educacao.schools.view', 'educacao.schools.manage',
    'educacao.students.view', 'educacao.students.manage', 'educacao.enrollment.manage',
    'educacao.teachers.view', 'educacao.teachers.manage', 'educacao.attendance.view',
    'educacao.attendance.manage', 'educacao.grades.view', 'educacao.grades.manage',
    'educacao.calendar.view', 'educacao.calendar.manage', 'educacao.meals.view',
    'educacao.meals.manage', 'educacao.transport.view', 'educacao.transport.manage',
    'governance.dashboard.view', 'governance.risks.view', 'governance.risks.manage',
    'governance.controls.view', 'governance.controls.manage',
    'governance.findings.view', 'governance.findings.manage', 'governance.findings.close',
    'governance.actions.view', 'governance.actions.manage', 'governance.actions.approve',
    'governance.evidence.view', 'governance.evidence.manage',
    'integration.view', 'integration.manage',
}


def product_enabled(organization_id):
    return bool(organization_id and OrganizationERP.objects.filter(
        organization_id=organization_id, erp__code=PRODUCT_CODE,
        erp__active=True, enabled=True,
    ).exists())


class MunicipalRecord(OrganizationOwned):
    """Tenant-owned record that can only exist under the Public Management ERP."""

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if not product_enabled(self.organization_id):
            raise ValidationError('O registro exige ERP Gestão Pública habilitado nesta organização.')

    def save(self, *args, **kwargs):
        self.validate_tenant()
        self.full_clean()
        return super().save(*args, **kwargs)


class CatalogoMunicipal(MunicipalRecord):
    codigo = models.CharField(max_length=30)
    nome = models.CharField(max_length=150)
    descricao = models.TextField(blank=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        abstract = True
        ordering = ['nome']

    def __str__(self):
        return self.nome


class PerfilPrefeitura(MunicipalRecord):
    """Complemento institucional da Organization usada como Prefeitura."""
    nome_institucional = models.CharField(max_length=180, blank=True)
    codigo_ibge = models.CharField(max_length=7, blank=True)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization'], name='rhmun_perfil_prefeitura_org')]

    @property
    def nome_exibicao(self):
        return self.nome_institucional or self.organization.display_name

    def clean(self):
        super().clean()
        if self.codigo_ibge and (len(self.codigo_ibge) != 7 or not self.codigo_ibge.isdigit()):
            raise ValidationError({'codigo_ibge': 'Informe um código IBGE com sete dígitos ou deixe em branco.'})


class Secretaria(CatalogoMunicipal):
    responsavel = models.ForeignKey('rh_municipal.Servidor', null=True, blank=True, on_delete=models.PROTECT, related_name='secretarias_responsavel')

    class Meta(CatalogoMunicipal.Meta):
        abstract = False
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_secretaria_codigo')]


class DepartamentoMunicipal(CatalogoMunicipal):
    secretaria = models.ForeignKey(Secretaria, on_delete=models.PROTECT, related_name='departamentos')
    responsavel = models.ForeignKey('rh_municipal.Servidor', null=True, blank=True, on_delete=models.PROTECT, related_name='departamentos_responsavel')

    class Meta(CatalogoMunicipal.Meta):
        abstract = False
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_depto_codigo')]


class SetorMunicipal(CatalogoMunicipal):
    departamento = models.ForeignKey(DepartamentoMunicipal, on_delete=models.PROTECT, related_name='setores')
    responsavel = models.ForeignKey('rh_municipal.Servidor', null=True, blank=True, on_delete=models.PROTECT, related_name='setores_responsavel')

    class Meta(CatalogoMunicipal.Meta):
        abstract = False
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_setor_codigo')]


class UnidadeMunicipal(CatalogoMunicipal):
    setor = models.ForeignKey(SetorMunicipal, on_delete=models.PROTECT, related_name='unidades')
    responsavel = models.ForeignKey('rh_municipal.Servidor', null=True, blank=True, on_delete=models.PROTECT, related_name='unidades_responsavel')

    class Meta(CatalogoMunicipal.Meta):
        abstract = False
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_unidade_codigo')]


class CargoMunicipal(CatalogoMunicipal):
    carga_horaria_semanal = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta(CatalogoMunicipal.Meta):
        abstract = False
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_cargo_codigo')]


class FuncaoMunicipal(CatalogoMunicipal):
    class Meta(CatalogoMunicipal.Meta):
        abstract = False
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_funcao_codigo')]


class VinculoFuncional(CatalogoMunicipal):
    class Meta(CatalogoMunicipal.Meta):
        abstract = False
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='rhmun_vinculo_codigo')]


class JornadaMunicipal(MunicipalRecord):
    nome = models.CharField(max_length=100)
    semana = models.JSONField(default=dict, help_text='Parâmetros informativos; regras locais devem ser homologadas antes do cálculo de folha/ponto.')
    ativa = models.BooleanField(default=True)

    class Meta:
        ordering = ['nome']
        constraints = [models.UniqueConstraint(fields=['organization', 'nome'], name='rhmun_jornada_nome')]

    def __str__(self):
        return self.nome


class Servidor(MunicipalRecord):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='servidores_municipais')
    matricula = models.CharField(max_length=30)
    nome = models.CharField(max_length=180)
    nome_social = models.CharField(max_length=180, blank=True)
    cpf = models.CharField(max_length=14, blank=True)
    email = models.EmailField(blank=True)
    telefone = models.CharField(max_length=30, blank=True)
    secretaria = models.ForeignKey(Secretaria, on_delete=models.PROTECT, related_name='servidores')
    departamento = models.ForeignKey(DepartamentoMunicipal, on_delete=models.PROTECT, related_name='servidores')
    setor = models.ForeignKey(SetorMunicipal, on_delete=models.PROTECT, related_name='servidores')
    unidade = models.ForeignKey(UnidadeMunicipal, on_delete=models.PROTECT, related_name='servidores')
    cargo = models.ForeignKey(CargoMunicipal, on_delete=models.PROTECT, related_name='servidores')
    funcao = models.ForeignKey(FuncaoMunicipal, on_delete=models.PROTECT, null=True, blank=True, related_name='servidores')
    vinculo = models.ForeignKey(VinculoFuncional, on_delete=models.PROTECT, related_name='servidores')
    jornada = models.ForeignKey(JornadaMunicipal, on_delete=models.PROTECT, null=True, blank=True, related_name='servidores')
    data_admissao = models.DateField(null=True, blank=True)
    data_desligamento = models.DateField(null=True, blank=True)
    situacao_funcional = models.CharField(max_length=18, choices=[
        ('ATIVO', 'Ativo'), ('AFASTADO', 'Afastado'),
        ('DESLIGADO', 'Desligado'), ('APOSENTADO', 'Aposentado'),
    ], default='ATIVO')
    observacoes_funcionais = models.TextField(blank=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ['nome']
        constraints = [
            models.UniqueConstraint(fields=['organization', 'matricula'], name='rhmun_matricula_org'),
            models.UniqueConstraint(fields=['organization', 'usuario'], condition=models.Q(usuario__isnull=False), name='rhmun_usuario_org'),
        ]

    def clean(self):
        super().clean()
        if not self.secretaria_id or not self.departamento_id or not self.setor_id or not self.unidade_id:
            return
        if self.departamento.secretaria_id != self.secretaria_id:
            raise ValidationError({'departamento': 'O departamento não pertence à secretaria informada.'})
        if self.setor.departamento_id != self.departamento_id:
            raise ValidationError({'setor': 'O setor não pertence ao departamento informado.'})
        if self.unidade.setor_id != self.setor_id:
            raise ValidationError({'unidade': 'A unidade não pertence ao setor informado.'})
        if self.data_admissao and self.data_desligamento and self.data_desligamento < self.data_admissao:
            raise ValidationError({'data_desligamento': 'A data de desligamento não pode anteceder a admissão.'})
        if self.usuario_id and not MembershipERP.objects.filter(
            membership__user_id=self.usuario_id, membership__organization_id=self.organization_id,
            membership__status='ATIVO', erp__code=PRODUCT_CODE, enabled=True, erp__active=True,
        ).exists():
            raise ValidationError({'usuario': 'A conta precisa ter membership ativo e acesso ao ERP Gestão Pública nesta organização.'})

    def __str__(self):
        return f'{self.matricula} · {self.nome}'


class AcessoRH(TenantOwned):
    tenant_lookup = 'membership__organization_id'
    class Papel(models.TextChoices):
        ADMIN_MUNICIPAL = 'ADMIN_MUNICIPAL', 'Administrador municipal'
        GESTOR_RH = 'GESTOR_RH', 'Gestor de RH'
        RH_OPERADOR = 'RH_OPERADOR', 'Operador de RH'
        CHEFIA = 'CHEFIA', 'Chefia'
        SERVIDOR = 'SERVIDOR', 'Servidor'

    membership = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT, related_name='acessos_rh_municipal')
    papel = models.CharField(max_length=24, choices=Papel.choices)
    ativo = models.BooleanField(default=True)
    atribuido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='acessos_rh_municipal_atribuidos')
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['membership', 'papel'], name='rhmun_acesso_membership_papel')]

    def clean(self):
        super().clean()
        if self.membership_id and (self.membership.status != 'ATIVO' or not MembershipERP.objects.filter(
            membership=self.membership, erp__code=PRODUCT_CODE, erp__active=True, enabled=True,
        ).exists()):
            raise ValidationError('Acesso exige membership ativo no ERP Gestão Pública.')
        if self.atribuido_por_id and not self.atribuido_por.memberships.filter(
            organization_id=self.membership.organization_id, status='ATIVO',
        ).exists():
            raise ValidationError({'atribuido_por': 'Quem atribui o acesso deve pertencer à mesma organização.'})

    def save(self, *args, **kwargs):
        self.validate_tenant()
        self.full_clean()
        return super().save(*args, **kwargs)


class AcessoMunicipal(TenantOwned):
    """Papel e escopo do ERP Público; nunca inferido do papel ERP genérico."""
    tenant_lookup = 'membership__organization_id'

    class Papel(models.TextChoices):
        ADMIN_PREFEITURA = 'ADMIN_PREFEITURA', 'Administrador da Prefeitura'
        ADMIN_SECRETARIA = 'ADMIN_SECRETARIA', 'Administrador de Secretaria'
        GESTOR_DEPARTAMENTO = 'GESTOR_DEPARTAMENTO', 'Gestor de Departamento'
        PROFISSIONAL = 'PROFISSIONAL', 'Profissional/Servidor'

    class Escopo(models.TextChoices):
        PREFEITURA = 'PREFEITURA', 'Prefeitura'
        SECRETARIA = 'SECRETARIA', 'Secretaria'
        DEPARTAMENTO = 'DEPARTAMENTO', 'Departamento'
        SETOR = 'SETOR', 'Setor'
        UNIDADE = 'UNIDADE', 'Unidade'

    membership = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT, related_name='acessos_gestao_publica')
    papel = models.CharField(max_length=24, choices=Papel.choices)
    escopo = models.CharField(max_length=16, choices=Escopo.choices)
    secretaria = models.ForeignKey(Secretaria, null=True, blank=True, on_delete=models.PROTECT, related_name='acessos_municipais')
    departamento = models.ForeignKey(DepartamentoMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='acessos_municipais')
    setor = models.ForeignKey(SetorMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='acessos_municipais')
    unidade = models.ForeignKey(UnidadeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='acessos_municipais')
    funcao = models.ForeignKey(FuncaoMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='acessos_municipais')
    ativo = models.BooleanField(default=True)
    atribuido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='acessos_municipais_atribuidos')
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['membership__user__username', 'papel']

    def clean(self):
        super().clean()
        if not self.membership_id:
            return
        org_id = self.membership.organization_id
        if self.membership.status != OrganizationMembership.Status.ACTIVE:
            raise ValidationError({'membership': 'O vínculo do usuário com a Prefeitura precisa estar ativo.'})
        if not MembershipERP.objects.filter(membership=self.membership, erp__code=PRODUCT_CODE, erp__active=True, enabled=True).exists():
            raise ValidationError({'membership': 'O usuário precisa ter acesso ao ERP Gestão Pública nesta Prefeitura.'})
        if not product_enabled(org_id):
            raise ValidationError('O ERP Gestão Pública não está habilitado para esta Prefeitura.')
        valid_scopes = {value for value, _ in self.Escopo.choices}
        if self.escopo not in valid_scopes:
            raise ValidationError({'escopo': 'Escopo municipal inválido.'})
        scope_fields = ('secretaria', 'departamento', 'setor', 'unidade')
        expected = {
            self.Escopo.PREFEITURA: None,
            self.Escopo.SECRETARIA: 'secretaria',
            self.Escopo.DEPARTAMENTO: 'departamento',
            self.Escopo.SETOR: 'setor',
            self.Escopo.UNIDADE: 'unidade',
        }[self.escopo]
        if expected is None and any(getattr(self, f'{name}_id') for name in scope_fields):
            raise ValidationError({'escopo': 'Escopo Prefeitura não deve apontar para uma unidade inferior.'})
        if expected and not getattr(self, f'{expected}_id'):
            raise ValidationError({expected: 'Selecione o registro correspondente ao escopo.'})
        for field in scope_fields:
            value = getattr(self, field)
            if value and value.organization_id != org_id:
                raise ValidationError({field: 'O escopo precisa pertencer à mesma Prefeitura.'})
        if self.departamento_id and self.secretaria_id and self.departamento.secretaria_id != self.secretaria_id:
            raise ValidationError({'departamento': 'O departamento não pertence à Secretaria selecionada.'})
        if self.setor_id and self.departamento_id and self.setor.departamento_id != self.departamento_id:
            raise ValidationError({'setor': 'O setor não pertence ao Departamento selecionado.'})
        if self.unidade_id and self.setor_id and self.unidade.setor_id != self.setor_id:
            raise ValidationError({'unidade': 'A unidade não pertence ao Setor selecionado.'})
        if self.unidade_id:
            unit = self.unidade
            if self.departamento_id and unit.setor.departamento_id != self.departamento_id:
                raise ValidationError({'unidade': 'A Unidade não pertence ao Departamento selecionado.'})
            if self.secretaria_id and unit.setor.departamento.secretaria_id != self.secretaria_id:
                raise ValidationError({'unidade': 'A Unidade não pertence à Secretaria selecionada.'})
        if self.setor_id and self.secretaria_id and self.setor.departamento.secretaria_id != self.secretaria_id:
            raise ValidationError({'setor': 'O Setor não pertence à Secretaria selecionada.'})
        required_scope = {
            self.Papel.ADMIN_PREFEITURA: self.Escopo.PREFEITURA,
            self.Papel.ADMIN_SECRETARIA: self.Escopo.SECRETARIA,
            self.Papel.GESTOR_DEPARTAMENTO: self.Escopo.DEPARTAMENTO,
        }.get(self.papel)
        if required_scope and self.escopo != required_scope:
            raise ValidationError({'escopo': f'{self.get_papel_display()} exige escopo {required_scope.label}.'})
        if self.papel == self.Papel.PROFISSIONAL and self.escopo not in {self.Escopo.SETOR, self.Escopo.UNIDADE}:
            raise ValidationError({'escopo': 'Profissional/Servidor deve possuir escopo de Setor ou Unidade.'})
        if self.funcao_id and self.funcao.organization_id != org_id:
            raise ValidationError({'funcao': 'A função precisa pertencer à mesma Prefeitura.'})
        if self.atribuido_por_id:
            tenant_attributor = OrganizationMembership.objects.filter(
                user=self.atribuido_por, organization_id=org_id,
                status=OrganizationMembership.Status.ACTIVE,
            ).exists()
            if not tenant_attributor:
                # The first municipal access has no municipal administrator
                # who could assign it yet. Permit only an explicitly trusted
                # platform administrator to be recorded as its attributor.
                from platform_core.saas_models import AdministradorPlataforma
                platform_attributor = self.atribuido_por.is_active and (
                    self.atribuido_por.is_superuser
                    or AdministradorPlataforma.objects.filter(
                        user=self.atribuido_por, active=True,
                    ).exists()
                )
                if not platform_attributor:
                    raise ValidationError({'atribuido_por': 'Quem atribui o acesso deve pertencer à Prefeitura ou ser administrador autorizado da plataforma.'})

    def save(self, *args, **kwargs):
        self.validate_tenant()
        self.full_clean()
        return super().save(*args, **kwargs)


class PermissaoMunicipal(TenantOwned):
    tenant_lookup = 'acesso__membership__organization_id'
    acesso = models.ForeignKey(AcessoMunicipal, on_delete=models.PROTECT, related_name='permissoes')
    capacidade = models.CharField(max_length=60)
    permitido = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['acesso', 'capacidade'], name='rhmun_permissao_acesso_cap')]

    def clean(self):
        super().clean()
        if self.capacidade not in PUBLIC_PERMISSION_CODES:
            raise ValidationError({'capacidade': 'Capacidade municipal desconhecida.'})
        organization = current_organization()
        if self.acesso_id and (organization is None or self.acesso.membership.organization_id != organization.pk):
            raise ValidationError({'acesso': 'Acesso pertence a outra Prefeitura.'})

    def save(self, *args, **kwargs):
        self.validate_tenant()
        self.full_clean()
        return super().save(*args, **kwargs)


class ConviteMunicipal(MunicipalRecord):
    """Convite bearer; somente o digest do token é persistido."""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    email = models.EmailField()
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    papel = models.CharField(max_length=24, choices=AcessoMunicipal.Papel.choices)
    escopo = models.CharField(max_length=16, choices=AcessoMunicipal.Escopo.choices)
    secretaria = models.ForeignKey(Secretaria, null=True, blank=True, on_delete=models.PROTECT, related_name='convites_municipais')
    departamento = models.ForeignKey(DepartamentoMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='convites_municipais')
    setor = models.ForeignKey(SetorMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='convites_municipais')
    unidade = models.ForeignKey(UnidadeMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='convites_municipais')
    funcao = models.ForeignKey(FuncaoMunicipal, null=True, blank=True, on_delete=models.PROTECT, related_name='convites_municipais')
    permissoes = models.JSONField(default=list, blank=True)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='convites_municipais_criados')
    criado_em = models.DateTimeField(auto_now_add=True)
    expira_em = models.DateTimeField()
    usado_em = models.DateTimeField(null=True, blank=True)
    aceito_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='convites_municipais_aceitos')
    revogado_em = models.DateTimeField(null=True, blank=True)
    revogado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='convites_municipais_revogados')

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    @property
    def valido(self):
        return not self.usado_em and not self.revogado_em and self.expira_em > timezone.now()

    def clean(self):
        super().clean()
        if self.escopo == AcessoMunicipal.Escopo.PREFEITURA and any((self.secretaria_id, self.departamento_id, self.setor_id, self.unidade_id)):
            raise ValidationError({'escopo': 'Escopo Prefeitura não deve apontar para uma unidade inferior.'})
        required_scope = {
            AcessoMunicipal.Papel.ADMIN_PREFEITURA: AcessoMunicipal.Escopo.PREFEITURA,
            AcessoMunicipal.Papel.ADMIN_SECRETARIA: AcessoMunicipal.Escopo.SECRETARIA,
            AcessoMunicipal.Papel.GESTOR_DEPARTAMENTO: AcessoMunicipal.Escopo.DEPARTAMENTO,
        }.get(self.papel)
        if required_scope and self.escopo != required_scope:
            raise ValidationError({'escopo': f'{self.get_papel_display()} exige seu escopo hierárquico correspondente.'})
        if self.papel == AcessoMunicipal.Papel.PROFISSIONAL and self.escopo not in {AcessoMunicipal.Escopo.SETOR, AcessoMunicipal.Escopo.UNIDADE}:
            raise ValidationError({'escopo': 'Profissional/Servidor deve possuir escopo de Setor ou Unidade.'})
        if self.secretaria_id and self.secretaria.organization_id != self.organization_id:
            raise ValidationError({'secretaria': 'Secretaria de outra Prefeitura.'})
        if self.departamento_id and self.departamento.organization_id != self.organization_id:
            raise ValidationError({'departamento': 'Departamento de outra Prefeitura.'})
        if self.setor_id and self.setor.organization_id != self.organization_id:
            raise ValidationError({'setor': 'Setor de outra Prefeitura.'})
        if self.unidade_id and self.unidade.organization_id != self.organization_id:
            raise ValidationError({'unidade': 'Unidade de outra Prefeitura.'})
        if self.funcao_id and self.funcao.organization_id != self.organization_id:
            raise ValidationError({'funcao': 'Função de outra Prefeitura.'})
        if self.criado_por_id and not OrganizationMembership.objects.filter(
            user=self.criado_por, organization_id=self.organization_id, status=OrganizationMembership.Status.ACTIVE,
        ).exists():
            raise ValidationError({'criado_por': 'Autor do convite precisa pertencer à Prefeitura.'})
        if not isinstance(self.permissoes, list) or any(code not in PUBLIC_PERMISSION_CODES for code in self.permissoes):
            raise ValidationError({'permissoes': 'O convite contém permissões municipais inválidas.'})
        required_scope = {
            AcessoMunicipal.Papel.ADMIN_PREFEITURA: AcessoMunicipal.Escopo.PREFEITURA,
            AcessoMunicipal.Papel.ADMIN_SECRETARIA: AcessoMunicipal.Escopo.SECRETARIA,
            AcessoMunicipal.Papel.GESTOR_DEPARTAMENTO: AcessoMunicipal.Escopo.DEPARTAMENTO,
        }.get(self.papel)
        if required_scope and self.escopo != required_scope:
            raise ValidationError({'escopo': 'Papel e escopo do convite são incompatíveis.'})
        if self.papel == AcessoMunicipal.Papel.PROFISSIONAL and self.escopo not in {AcessoMunicipal.Escopo.SETOR, AcessoMunicipal.Escopo.UNIDADE}:
            raise ValidationError({'escopo': 'Profissional/Servidor exige escopo de Setor ou Unidade.'})
        if self.departamento_id and self.secretaria_id and self.departamento.secretaria_id != self.secretaria_id:
            raise ValidationError({'departamento': 'Departamento não pertence à Secretaria do convite.'})
        if self.setor_id and self.departamento_id and self.setor.departamento_id != self.departamento_id:
            raise ValidationError({'setor': 'Setor não pertence ao Departamento do convite.'})
        if self.unidade_id and self.setor_id and self.unidade.setor_id != self.setor_id:
            raise ValidationError({'unidade': 'Unidade não pertence ao Setor do convite.'})
        if self.unidade_id:
            unit = self.unidade
            if self.departamento_id and unit.setor.departamento_id != self.departamento_id:
                raise ValidationError({'unidade': 'Unidade não pertence ao Departamento do convite.'})
            if self.secretaria_id and unit.setor.departamento.secretaria_id != self.secretaria_id:
                raise ValidationError({'unidade': 'Unidade não pertence à Secretaria do convite.'})
        if self.setor_id and self.secretaria_id and self.setor.departamento.secretaria_id != self.secretaria_id:
            raise ValidationError({'setor': 'Setor não pertence à Secretaria do convite.'})
        required_field = {
            AcessoMunicipal.Escopo.SECRETARIA: ('secretaria', self.secretaria_id),
            AcessoMunicipal.Escopo.DEPARTAMENTO: ('departamento', self.departamento_id),
            AcessoMunicipal.Escopo.SETOR: ('setor', self.setor_id),
            AcessoMunicipal.Escopo.UNIDADE: ('unidade', self.unidade_id),
        }.get(self.escopo)
        if required_field and not required_field[1]:
            raise ValidationError({required_field[0]: 'Selecione o registro correspondente ao escopo do convite.'})

    def save(self, *args, **kwargs):
        self.validate_tenant()
        self.full_clean()
        return super().save(*args, **kwargs)


class MunicipalHistoryQuerySet(TenantQuerySet):
    def delete(self):
        raise ValidationError('Histórico funcional municipal não pode ser excluído.')


class RegistroMunicipalImutavel(MunicipalRecord):
    objects = TenantManager.from_queryset(MunicipalHistoryQuerySet)()
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk or not getattr(self, '_service_write', False):
            raise ValidationError('Use o serviço municipal auditado; o registro é imutável.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Histórico municipal não pode ser excluído.')


class HistoricoServidor(RegistroMunicipalImutavel):
    servidor = models.ForeignKey(Servidor, on_delete=models.PROTECT, related_name='historico')
    acao = models.CharField(max_length=60)
    antes = models.JSONField(default=dict, blank=True)
    depois = models.JSONField(default=dict, blank=True)
    motivo = models.TextField(blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)


class SolicitacaoServidor(RegistroMunicipalImutavel):
    servidor = models.ForeignKey(Servidor, on_delete=models.PROTECT, related_name='solicitacoes')
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request_id = models.UUIDField(default=uuid.uuid4, editable=False)
    tipo = models.CharField(max_length=32, choices=[
        ('FERIAS', 'Férias'), ('AFASTAMENTO', 'Afastamento'),
        ('CORRECAO_PONTO', 'Correção de ponto'), ('DOCUMENTO', 'Documento'),
        ('ATUALIZACAO_CADASTRAL', 'Atualização cadastral'),
    ])
    descricao = models.TextField()
    dados = models.JSONField(default=dict, blank=True)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'request_id'], name='rhmun_solic_request')]

    @property
    def status(self):
        try:
            return 'APROVADA' if self.decisao.aprovada else 'RECUSADA'
        except DecisaoSolicitacaoServidor.DoesNotExist:
            return 'PENDENTE'


class DecisaoSolicitacaoServidor(RegistroMunicipalImutavel):
    solicitacao = models.OneToOneField(SolicitacaoServidor, on_delete=models.PROTECT, related_name='decisao')
    aprovada = models.BooleanField()
    motivo = models.TextField()
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(default=timezone.now, editable=False)

    def clean(self):
        super().clean()
        if self.usuario_id and not self.usuario.memberships.filter(organization_id=self.organization_id, status='ATIVO').exists():
            raise ValidationError({'usuario': 'Quem decide deve pertencer à mesma organização.'})


class PontoServidor(RegistroMunicipalImutavel):
    servidor = models.ForeignKey(Servidor, on_delete=models.PROTECT, related_name='marcacoes_ponto')
    request_id = models.UUIDField(default=uuid.uuid4, editable=False)
    tipo = models.CharField(max_length=24, choices=[('ENTRADA','Entrada'),('SAIDA_INTERVALO','Saída para intervalo'),('RETORNO_INTERVALO','Retorno do intervalo'),('SAIDA','Saída')])
    data_hora = models.DateTimeField(default=timezone.now, editable=False)
    origem = models.CharField(max_length=12, choices=[('WEB','Web'),('MOBILE','Mobile'),('MANUAL','Registro manual autorizado')], default='WEB')
    dispositivo = models.CharField(max_length=160, blank=True)
    endereco_ip = models.GenericIPAddressField(null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    observacao = models.CharField(max_length=500, blank=True)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='pontos_municipais_registrados')
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'request_id'], name='rhmun_ponto_request')]

    def clean(self):
        super().clean()
        if self.servidor_id and self.servidor.organization_id != self.organization_id:
            raise ValidationError({'servidor': 'A ficha pertence a outra Prefeitura.'})
        if self.registrado_por_id and not self.registrado_por.memberships.filter(
            organization_id=self.organization_id, status=OrganizationMembership.Status.ACTIVE,
        ).exists():
            raise ValidationError({'registrado_por': 'O responsável precisa de membership ativo nesta Prefeitura.'})


class FeriasServidor(MunicipalRecord):
    servidor = models.ForeignKey(Servidor, on_delete=models.PROTECT, related_name='ferias')
    inicio = models.DateField()
    fim = models.DateField()
    status = models.CharField(max_length=16, choices=[('SOLICITADA','Solicitada'),('APROVADA','Aprovada'),('RECUSADA','Recusada'),('CANCELADA','Cancelada')], default='SOLICITADA')
    observacao = models.TextField(blank=True)

    def clean(self):
        super().clean()
        if self.inicio and self.fim and self.fim < self.inicio:
            raise ValidationError({'fim': 'O fim das férias deve ser igual ou posterior ao início.'})


class AfastamentoServidor(MunicipalRecord):
    servidor = models.ForeignKey(Servidor, on_delete=models.PROTECT, related_name='afastamentos')
    tipo = models.CharField(max_length=40)
    inicio = models.DateField()
    fim = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=[('SOLICITADO','Solicitado'),('REGISTRADO','Registrado'),('ENCERRADO','Encerrado'),('RECUSADO','Recusado')], default='SOLICITADO')
    observacao = models.TextField(blank=True)

    def clean(self):
        super().clean()
        if self.inicio and self.fim and self.fim < self.inicio:
            raise ValidationError({'fim': 'O fim do afastamento deve ser igual ou posterior ao início.'})


class DocumentoServidor(MunicipalRecord):
    servidor = models.ForeignKey(Servidor, on_delete=models.PROTECT, related_name='documentos')
    titulo = models.CharField(max_length=160)
    categoria = models.CharField(max_length=40)
    storage_key = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64, blank=True)
    enviado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        prefix = f'organizations/{self.organization.public_id}/gestao-publica/servidores/{self.servidor.public_id}/'
        if not self.storage_key.startswith(prefix) or '..' in self.storage_key.split('/') or '\\' in self.storage_key:
            raise ValidationError({'storage_key': 'O documento deve usar chave privada deste servidor e organização.'})


# Phase C extends the municipal HR foundation without duplicating the
# municipality hierarchy, employee, journey, vacation, leave or document rows.
from .phase_c_models import (  # noqa: E402,F401
    AtribuicaoJornadaMunicipal, BeneficioServidorMunicipal,
    CompetenciaFolhaMunicipal, DependenteServidorMunicipal,
    DecisaoPontoMunicipal, EscalaMunicipal, EventoFolhaMunicipal,
    EventoPontoDigital, FeriadoMunicipal, FolgaMunicipal,
    GrupoJornadaMunicipal, HistoricoLotacaoServidor,
    HorarioJornadaMunicipal, JustificativaPontoMunicipal,
    LancamentoFolhaMunicipal, MovimentacaoBancoHorasMunicipal,
    PeriodoPontoMunicipal, RemuneracaoBaseServidorMunicipal,
    SolicitacaoHoraExtraMunicipal, SnapshotFechamentoPontoMunicipal,
)


# Keep the public-sector foundations in a separate module while registering
# them in the existing municipal app and reusing its tenant base model.
from .phase_a_models import (  # noqa: E402,F401
    AcaoPlanejamentoMunicipal, BancoMunicipal, ConciliacaoBancariaMunicipal,
    ContaBancariaMunicipal, ContaContabilMunicipal, CreditoAdicionalMunicipal,
    DotacaoOrcamentariaMunicipal, ElementoDespesaMunicipal,
    ExercicioMunicipal, FechamentoContabilMunicipal,
    FonteDestinacaoRecursoMunicipal, FuncaoOrcamentariaMunicipal,
    InstrumentoPlanejamentoMunicipal, IndicadorPlanejamentoMunicipal,
    LancamentoContabilMunicipal, MovimentacaoTesourariaMunicipal,
    NaturezaDespesaMunicipal, OrgaoOrcamentarioMunicipal,
    PeriodoContabilMunicipal, PlanoContasMunicipal,
    ProgramaPlanejamentoMunicipal, SubfuncaoOrcamentariaMunicipal,
    UnidadeOrcamentariaMunicipal,
)

# Phase B procurement and public contracting models share the same municipal
# tenant base and are imported here so Django registers them in this app.
from .phase_b_models import (  # noqa: E402,F401
    AditivoContratoPublico, AtaRegistroPrecosMunicipal, CotacaoFornecedorMunicipal,
    DocumentoContratacaoMunicipal, ExecucaoContratoPublico,
    FornecedorProcessoMunicipal, HistoricoContratacaoPublica,
    ItemAtaRegistroPrecosMunicipal, ItemContratacaoMunicipal,
    ItemPesquisaPrecoMunicipal, ItemPropostaMunicipal,
    ItemSolicitacaoCompra, LicitacaoMunicipal,
    LoteContratacaoMunicipal, ModalidadeContratacaoMunicipal,
    PesquisaPrecoMunicipal, ProcessoContratacaoMunicipal,
    PropostaMunicipal, SolicitacaoCompra, UtilizacaoAtaPrecosMunicipal,
    ContratoPublicoMunicipal,
)

# Phase D municipal asset, warehouse, and fleet models. The organization
# hierarchy, civil servants, suppliers, and public procurement models above
# remain the shared sources of truth.
from .phase_d_models import (  # noqa: E402,F401
    AbastecimentoMunicipal, BemPatrimonial, CondutorMunicipal,
    DepreciacaoPatrimonial, DepositoAlmoxarifadoMunicipal,
    DocumentoFrotaMunicipal, EventoAlmoxarifadoMunicipal,
    EventoFrotaMunicipal, EventoPatrimonioMunicipal,
    InventarioAlmoxarifadoMunicipal, InventarioPatrimonial,
    ItemAlmoxarifadoMunicipal, ItemInventarioAlmoxarifadoMunicipal,
    ItemInventarioPatrimonial, ItemSolicitacaoMaterialMunicipal,
    LoteAlmoxarifadoMunicipal, ManutencaoVeiculoMunicipal,
    MovimentoAlmoxarifadoMunicipal, MovimentacaoPatrimonial,
    RecebimentoAlmoxarifadoMunicipal, RequisicaoMaterialAuditada,
    SaldoAlmoxarifadoMunicipal, SolicitacaoMaterialMunicipal,
    VeiculoMunicipal, ViagemMunicipal,
)

# Phase E: digital process, public transparency, ombudsman and E-SIC records.
from .phase_e_models import (  # noqa: E402,F401
    AuditoriaProcessoDigitalMunicipal, ContadorExpedienteMunicipal,
    DespachoProcessoMunicipal, DocumentoProcessoMunicipal,
    EventoAssinaturaMunicipal, EventoEsicMunicipal, EventoOuvidoriaMunicipal,
    ManifestacaoOuvidoriaMunicipal, ParecerProcessoMunicipal,
    ParteProcessoMunicipal, PedidoEsicMunicipal,
    ProcessoDigitalMunicipal, ProtocoloMunicipal,
    PublicacaoTransparenciaMunicipal, RecursoEsicMunicipal,
    RespostaEsicMunicipal, RespostaOuvidoriaMunicipal,
    SignatarioMunicipal, SolicitacaoAssinaturaMunicipal,
    TarefaProcessoMunicipal, TramiteProcessoMunicipal,
    VersaoDocumentoProcessoMunicipal,
)

# Phase F: one municipal property record is shared by territorial data and IPTU.
from .phase_f_models import (  # noqa: E402,F401
    AuditoriaTributariaMunicipal, ContribuinteMunicipal,
    ImovelMunicipal, InscricaoDividaAtivaMunicipal,
    LancamentoTributarioMunicipal, NotaServicoMunicipal,
    PagamentoArrecadacaoMunicipal, ParcelaTributariaMunicipal,
    RegraTributariaMunicipal,
)

# Phase G: municipal health records reuse municipal units, staff and fleet.
from .phase_g_models import (  # noqa: E402,F401
    AgendaSaudeMunicipal, AtendimentoSaudeMunicipal, AuditoriaAcessoSaude,
    CidadaoSaudeMunicipal, DispensacaoMedicamentoMunicipal,
    EquipeSaudeMunicipal, EstabelecimentoSanitarioMunicipal,
    EvolucaoClinicaMunicipal, FamiliaTerritorialSaude, InspecaoSanitariaMunicipal,
    ItemDispensacaoSaude, ItemPrescricaoSaude, LoteMedicamentoMunicipal, MedicamentoMunicipal,
    NotificacaoEpidemiologicaMunicipal, PrescricaoSaudeMunicipal,
    ProfissionalSaudeMunicipal, RegistroProducaoSUS, SolicitacaoRegulacaoSaude,
    TFDMunicipal, UnidadeSaudeMunicipal, VeiculoSanitarioMunicipal,
    VagaRegulacaoSaude, VisitaTerritorialSaude, VinculoFamiliaSaude,
    ServicoFilaSaude, LocalAtendimentoSaude, SenhaFilaAtendimento, ClassificacaoManchester,
)

# Phase H: municipal education uses the shared citizen, employee, unit,
# warehouse-item and fleet records without copying their identities.
from .phase_h_models import (  # noqa: E402,F401
    AnoLetivoMunicipal, AvaliacaoEscolarMunicipal, CalendarioEscolarMunicipal,
    CardapioAlimentacaoEscolar, EscolaMunicipal, FrequenciaEscolarMunicipal,
    ItemCardapioEscolar, LotacaoProfessorTurma, MatriculaAlunoMunicipal,
    NotaEscolarMunicipal, ResponsavelAlunoMunicipal, SalaEscolarMunicipal,
    TransporteEscolarMunicipal, TurmaMunicipal,
)
from .phase_i_models import (  # noqa: E402,F401
    AchadoControleInterno, AcaoPlanoAcaoInterno, ControleInternoMunicipal,
    EvidenciaControleInterno, EventoControleInterno, PlanoAcaoInterno,
    RecomendacaoControleInterno, RiscoControleInterno,
)

# Phase L: tenant-scoped integration metadata. Provider adapters remain disabled.
from .phase_l_models import (  # noqa: E402,F401
    ConfiguracaoIntegracaoMunicipal, EventoIntegracaoMunicipal,
    TentativaIntegracaoMunicipal,
)
