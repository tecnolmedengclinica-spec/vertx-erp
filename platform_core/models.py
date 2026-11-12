import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from .validators import normalize_cnpj, validate_cnpj, formatted_cnpj


class Organization(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'ATIVA', 'Ativa'
        INACTIVE = 'INATIVA', 'Inativa'
        SUSPENDED = 'SUSPENSA', 'Suspensa'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    slug = models.SlugField(unique=True)
    razao_social = models.CharField('Razão social', max_length=255)
    nome_fantasia = models.CharField(max_length=150, blank=True)
    cnpj = models.CharField('CNPJ', max_length=14, blank=True, default='', validators=[validate_cnpj])
    email = models.EmailField(blank=True)
    telefone = models.CharField(max_length=30, blank=True)
    endereco = models.CharField('Endereço', max_length=255, blank=True)
    cidade = models.CharField(max_length=100, blank=True)
    uf = models.CharField('UF', max_length=2, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    # Chave relativa do storage privado; futura personalização sem filesystem.path.
    logo_key = models.CharField('Chave da logomarca no storage privado', max_length=255, blank=True)
    document_details = models.TextField('Informações adicionais nos documentos', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['cnpj'], condition=~models.Q(cnpj=''), name='organization_cnpj_unique')]

    @property
    def display_name(self):
        return self.nome_fantasia or self.razao_social

    @property
    def cnpj_formatado(self):
        return formatted_cnpj(self.cnpj)

    def __str__(self):
        return self.display_name

    def clean(self):
        super().clean()
        self.cnpj = normalize_cnpj(self.cnpj)
        validate_cnpj(self.cnpj)
        if self.logo_key and (not self.logo_key.startswith(f'organizations/{self.public_id}/branding/') or '..' in self.logo_key.split('/') or '\\' in self.logo_key):
            raise ValidationError({'logo_key': 'A logomarca deve usar uma chave privada de branding desta organização.'})

    def save(self, *args, **kwargs):
        self.cnpj = normalize_cnpj(self.cnpj)
        self.full_clean()
        return super().save(*args, **kwargs)


class ERPCatalog(models.Model):
    code = models.SlugField(unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    operational = models.BooleanField(default=False, help_text='Indica que a experiência deste produto está implementada e liberada para entrada.')
    public_path = models.CharField(max_length=150, blank=True)
    entry_path = models.CharField(max_length=150, blank=True)

    def __str__(self):
        return self.name


class ERPProductModule(models.Model):
    """Explicit module catalog scoped to one canonical ERP product."""
    erp = models.ForeignKey(ERPCatalog, on_delete=models.PROTECT, related_name='modules')
    code = models.SlugField(max_length=40)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['erp__name', 'sort_order', 'name', 'pk']
        constraints = [models.UniqueConstraint(fields=['erp', 'code'], name='erp_module_code_unique')]

    def __str__(self):
        return f'{self.erp.name} · {self.name}'


class ERPProductResource(models.Model):
    """Feature/resource registry beneath a product module."""
    module = models.ForeignKey(ERPProductModule, on_delete=models.PROTECT, related_name='resources')
    code = models.SlugField(max_length=50)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['module__erp__name', 'module__sort_order', 'sort_order', 'name', 'pk']
        constraints = [models.UniqueConstraint(fields=['module', 'code'], name='erp_resource_code_unique')]

    def __str__(self):
        return f'{self.module} · {self.name}'


class OrganizationERP(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='erp_accesses')
    erp = models.ForeignKey(ERPCatalog, on_delete=models.PROTECT)
    enabled = models.BooleanField(default=True)
    enabled_at = models.DateTimeField(default=timezone.now)
    disabled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'erp'], name='organization_erp_unique')]


class OrganizationMembership(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'ATIVO', 'Ativo'
        INACTIVE = 'INATIVO', 'Inativo'
        BLOCKED = 'BLOQUEADO', 'Bloqueado'

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='memberships')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    role = models.CharField(max_length=20, choices=[('ADMIN', 'Administrador da organização'), ('GESTOR', 'Gestor'), ('TECNICO', 'Técnico'), ('PLANEJADOR', 'Planejador'), ('ATENDIMENTO', 'Atendimento'), ('PAINEL', 'Somente painel operacional'), ('ESTOQUE', 'Estoquista'), ('FINANCEIRO', 'Financeiro'), ('CONTABILIDADE', 'Contabilidade'), ('CLIENTE', 'Cliente do portal'), ('PDV', 'Operador de PDV'), ('VENDEDOR', 'Vendedor'), ('COMPRAS', 'Compras'), ('RH', 'Recursos humanos'), ('PAINEL_TV', 'Painel TV somente leitura'), ('SERVIDOR_PUBLICO', 'Servidor público (acesso municipal)')], default='TECNICO')
    joined_at = models.DateTimeField(default=timezone.now)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'user'], name='organization_user_unique')]

    def __str__(self):
        return f'{self.user} · {self.organization} · {self.get_role_display()}'


class MembershipERP(models.Model):
    visualizar_painel_operacional = models.BooleanField('Visualizar painel operacional', default=False)
    membership = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT, related_name='erp_accesses')
    erp = models.ForeignKey(ERPCatalog, on_delete=models.PROTECT)
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['membership', 'erp'], name='membership_erp_unique')]


class PlatformAudit(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    erp = models.ForeignKey(ERPCatalog, on_delete=models.PROTECT, null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=80)
    record = models.CharField(max_length=150)
    changes = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Auditoria da plataforma é imutável.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Auditoria não pode ser excluída.')


from .tenancy import OrganizationOwned


class ConfiguracaoTenant(OrganizationOwned):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.validate_tenant()
        self.full_clean()
        return super().save(*args, **kwargs)


class FilialEmpresa(ConfiguracaoTenant):
    """Unidade da empresa operadora; nunca uma Unidade de cliente."""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    codigo = models.CharField(max_length=30)
    nome = models.CharField(max_length=150)
    cnpj = models.CharField(max_length=14, blank=True, validators=[validate_cnpj])
    endereco = models.TextField(blank=True)
    inscricao_estadual = models.CharField(max_length=30, blank=True)
    inscricao_municipal = models.CharField(max_length=30, blank=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ['nome']
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='filial_codigo_org')]

    def __str__(self):
        return self.nome

    def clean(self):
        super().clean()
        self.cnpj = normalize_cnpj(self.cnpj)


class ConfiguracaoEmpresa(ConfiguracaoTenant):
    """Metadados da PJ principal identificada pela Organization existente."""
    inscricao_estadual = models.CharField(max_length=30, blank=True)
    inscricao_municipal = models.CharField(max_length=30, blank=True)
    regime_tributario = models.CharField(max_length=60, blank=True)
    crt = models.CharField(max_length=2, blank=True)
    natureza_operacao = models.CharField(max_length=150, blank=True)
    codigo_servico = models.CharField(max_length=60, blank=True)
    iss_informativo = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    retencoes_informativas = models.JSONField(default=dict, blank=True)
    plano = models.CharField(max_length=20, choices=[('', 'Não definido'), ('START', 'Start'), ('PROFESSIONAL', 'Professional'), ('ENTERPRISE', 'Enterprise'), ('GOVERNO', 'Governo')], blank=True)
    limites = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization'], name='config_empresa_org_unica')]

    def clean(self):
        super().clean()
        if not isinstance(self.limites, dict) or any(key not in ['usuarios', 'filiais', 'armazenamento_mb'] or isinstance(value, bool) or not isinstance(value, int) or value < 0 for key, value in self.limites.items()):
            raise ValidationError({'limites': 'Informe limites inteiros não negativos para usuários, filiais ou armazenamento_mb.'})
        if self.iss_informativo is not None and not 0 <= self.iss_informativo <= 100:
            raise ValidationError({'iss_informativo': 'Percentual deve estar entre 0 e 100; não implica apuração fiscal.'})


class ModuloOrganizacao(ConfiguracaoTenant):
    from .saas_models import MODULE_LABELS
    codigo = models.CharField(max_length=40, choices=sorted(MODULE_LABELS.items()))
    habilitado = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'codigo'], name='modulo_organizacao_unico')]


class PermissaoMembership(ConfiguracaoTenant):
    membership = models.ForeignKey(OrganizationMembership, on_delete=models.PROTECT, related_name='capabilities')
    capacidade = models.CharField(max_length=60)
    permitido = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['membership', 'capacidade'], name='membership_capacidade_unica')]

    def clean(self):
        super().clean()
        from .modules import CAPABILITIES
        if self.membership.organization_id != self.organization_id:
            raise ValidationError({'membership': 'Vínculo deve pertencer à mesma organização.'})
        if self.capacidade not in CAPABILITIES:
            raise ValidationError({'capacidade': 'Capacidade desconhecida.'})


# SaaS administration is a platform capability, never a tenant membership role.
from .saas_models import (
    AdministradorPlataforma, AjusteModuloSaaS, PlanoSaaS, PlanoProdutoModulo, ContratoSaaS,
    ItemContratoSaaS, AssinaturaSaaS,
    HistoricoPlanoSaaS, PrimeiroAcessoSaaS, AuditoriaSaaS,
)
