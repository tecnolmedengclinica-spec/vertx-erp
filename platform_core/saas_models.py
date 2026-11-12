import hashlib
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


MODULE_LABELS = {
    # Products and scoped entitlements used by the approved public matrix.
    'clientes': 'Clientes',
    'produtos': 'Produtos',
    'assistencia': 'Assistência',
    'estoque_basico': 'Estoque básico',
    'estoque_completo': 'Estoque completo',
    'compras': 'Compras',
    'financeiro_basico': 'Financeiro básico',
    'financeiro_completo': 'Financeiro completo',
    'pdv': 'PDV',
    'contratos': 'Contratos',
    'portal': 'Portal do cliente',
    'pwa_tecnico': 'PWA Técnico',
    'rh_asst_basico': 'RH Assistência básico',
    'rh_asst_completo': 'RH Assistência completo',
    'documentos_basicos': 'Documentos básicos',
    'documentos_completos': 'Documentos completos',
    'relatorios_basicos': 'Relatórios básicos',
    'relatorios_completos': 'Relatórios completos',
    'pwa_vendedor': 'PWA Vendedor',
    'gestao_publica': 'Gestão Pública (sob proposta)',
    'estrutura_municipal': 'Estrutura Municipal',
    'usuarios_municipais': 'Usuários Municipais',
    'acessos_municipais': 'Acessos e Permissões Municipais',
    'convites_municipais': 'Convites Municipais',
    'planejamento_publico': 'Planejamento Público',
    'orcamento_publico': 'Orçamento Público',
    'contabilidade_pub': 'Contabilidade Pública (fundação)',
    'tesouraria_publica': 'Tesouraria (fundação)',
    'compras_publicas': 'Compras Públicas',
    'licitacoes_publicas': 'Licitações Públicas',
    'registro_precos': 'Registro de Preços Público',
    'contratos_publicos': 'Contratos Públicos',
    'rh_municipal': 'RH Municipal (sob proposta)',
    'ponto_digital': 'Ponto Digital Municipal',
    'patrimonio': 'Patrimônio Municipal',
    'almoxarifado': 'Almoxarifado Municipal',
    'frotas': 'Frotas Municipais',
    'processo_digital': 'Processo Digital e Protocolo',
    'transparencia': 'Transparência Pública',
    'ouvidoria': 'Ouvidoria Municipal',
    'esic': 'E-SIC Municipal',
    'tributos_municipais': 'Tributos e Arrecadação Municipal (parametrizável)',
    'gestao_territorial': 'Gestão Territorial e Cadastro Imobiliário',
    'nfse_municipal': 'NFS-e Municipal (preparação; transmissão pendente)',
    'saude_sus': 'Saúde SUS Municipal (fundação operacional; integrações pendentes)',
    'educacao_municipal': 'Educação Municipal (fundação operacional)',
    'controle_interno': 'Controle Interno e Governança Municipal',
    # Retained keys keep existing organizations and saved plans compatible.
    'estoque': 'Compatibilidade: Estoque legado',
    'financeiro': 'Compatibilidade: Financeiro legado',
    'ponto': 'Compatibilidade: Ponto legado',
    'rh': 'Compatibilidade: RH legado',
    'documentos': 'Compatibilidade: Documentos legado',
    'fiscal': 'Fiscal (homologação; transmissão bloqueada)',
}
MODULES = frozenset(MODULE_LABELS)
# Reporting is an included product capability in every tier. The tier below
# controls depth, not whether charts exist at all.
ANALYTICS_LEVELS = (
    ('BASICO', 'Básico'),
    ('AVANCADO', 'Avançado'),
    ('COMPLETO', 'Completo'),
)
# Operational core included by every VERTYX subscription. Fiscal access here
# is module availability only; live transmission remains independently gated.
CORE_SAAS_MODULES = (
    'clientes',          # customer records are a commercial core module
    'produtos',          # product catalog is a commercial core module
    'assistencia',       # preserves the existing basic service entitlement
    'estoque_basico',    # product catalog and basic inventory
    'financeiro_basico',
    'relatorios_basicos',
    'pdv',
    'fiscal',
)
LIMIT_KEYS = frozenset({'usuarios', 'filiais', 'armazenamento_mb', 'vendas_mes', 'ordens_mes'})


class AdministradorPlataforma(models.Model):
    """Explicit platform access; tenant ADMIN/superuser flags do not imply it."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='saas_platform_access')
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='saas_platform_grants')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'administrador da plataforma VERTYX'
        verbose_name_plural = 'administradores da plataforma VERTYX'

    def clean(self):
        if self.user_id and not self.user.is_active:
            raise ValidationError({'user': 'O usuário da plataforma precisa estar ativo.'})

    def delete(self, *args, **kwargs):
        raise ValidationError('Preserve o histórico; desative o acesso da plataforma.')


class PlanoSaaS(models.Model):
    class Period(models.TextChoices):
        MONTH = 'MENSAL', 'Mensal'
        YEAR = 'ANUAL', 'Anual'

    slug = models.SlugField(unique=True)
    plano_comercial = models.ForeignKey(
        'commercial.PlanoComercial', on_delete=models.PROTECT, null=True, blank=True,
        related_name='planos_saas', verbose_name='Plano comercial público',
    )
    # Null preserves legacy plans until the platform administrator explicitly
    # classifies them; a product must never be inferred from its name or price.
    erp = models.ForeignKey(
        'platform_core.ERPCatalog', on_delete=models.PROTECT, null=True, blank=True,
        related_name='planos_saas', verbose_name='Produto ERP',
    )
    publico_ativo = models.BooleanField(
        default=False, verbose_name='Disponível para contratação online',
        help_text='Só libera o cadastro público quando preço e periodicidade coincidirem com a oferta comercial.',
    )
    nome = models.CharField(max_length=100)
    descricao = models.CharField(max_length=300, blank=True)
    preco = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    periodicidade = models.CharField(max_length=8, choices=Period.choices, default=Period.MONTH)
    modulos = models.JSONField(default=list, blank=True)
    limites = models.JSONField(default=dict, blank=True)
    nivel_indicadores = models.CharField(
        max_length=12, choices=ANALYTICS_LEVELS, default='BASICO',
        verbose_name='Nível de indicadores',
        help_text='Gráficos básicos estão incluídos em todos os planos; este nível controla detalhamento e comparativos.',
    )
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['nome', 'pk']
        verbose_name = 'plano SaaS'
        verbose_name_plural = 'planos SaaS'
        constraints = [models.UniqueConstraint(
            fields=['plano_comercial', 'periodicidade'],
            name='saas_public_offer_period_unique',
        )]

    def __str__(self):
        return self.nome

    def clean(self):
        super().clean()
        if not isinstance(self.modulos, list) or any(not isinstance(code, str) or code not in MODULES for code in self.modulos) or len(set(self.modulos)) != len(self.modulos):
            raise ValidationError({'modulos': 'Selecione módulos válidos e sem duplicidade.'})
        # Core modules cannot be removed through the SaaS plan editor or API.
        self.modulos = list(dict.fromkeys([*CORE_SAAS_MODULES, *self.modulos]))
        if not isinstance(self.limites, dict):
            raise ValidationError({'limites': 'Limites devem ser um objeto.'})
        for key, value in self.limites.items():
            if key not in LIMIT_KEYS or value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValidationError({'limites': 'Use limites conhecidos como inteiros não negativos ou null para “A definir”.'})
        if self.limites.get('usuarios') == 0:
            raise ValidationError({'limites': 'O limite de usuários precisa comportar o administrador inicial.'})
        if self.preco is not None and self.preco < 0:
            raise ValidationError({'preco': 'Preço não pode ser negativo.'})
        if self.publico_ativo:
            if not self.ativo:
                raise ValidationError({'publico_ativo': 'Um plano SaaS inativo não pode ser oferecido para contratação.'})
            if self.erp_id and self.erp.code != 'assistencia':
                raise ValidationError({
                    'publico_ativo': 'A contratação pública multierp ainda não está habilitada; use contratos da Administração VERTYX até a próxima fase.'
                })
            if {'gestao_publica', 'rh_municipal'} & set(self.modulos or ()):
                raise ValidationError({
                    'modulos': 'Gestão Pública/RH Municipal ficam sob proposta. Acesso ao módulo Fiscal não habilita transmissão.'
                })
            if not self.plano_comercial_id or not self.plano_comercial.ativo:
                raise ValidationError({'plano_comercial': 'Vincule um plano comercial ativo antes de liberar a contratação.'})
            public_price=(self.plano_comercial.valor_anual if self.periodicidade==self.Period.YEAR
                          else self.plano_comercial.preco)
            if public_price is None or self.preco != public_price:
                raise ValidationError({'preco': 'Preço e periodicidade devem coincidir com a oferta comercial vinculada.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Planos não são excluídos; desative o plano para preservar o histórico.')


class PlanoProdutoModulo(models.Model):
    """Explicit module assignment for a product-scoped SaaS plan."""
    plano = models.ForeignKey(PlanoSaaS, on_delete=models.PROTECT, related_name='modulos_produto')
    modulo = models.ForeignKey('platform_core.ERPProductModule', on_delete=models.PROTECT, related_name='planos')

    class Meta:
        ordering = ['modulo__sort_order', 'modulo__name', 'pk']
        constraints = [models.UniqueConstraint(fields=['plano', 'modulo'], name='saas_plan_module_unique')]

    def clean(self):
        super().clean()
        if self.plano_id and self.modulo_id and self.plano.erp_id != self.modulo.erp_id:
            raise ValidationError({'modulo': 'O módulo precisa pertencer ao mesmo produto do plano.'})


class ContratoSaaS(models.Model):
    """Commercial agreement capable of carrying multiple independent ERP lines."""
    class Status(models.TextChoices):
        RASCUNHO = 'RASCUNHO', 'Rascunho'
        ATIVO = 'ATIVO', 'Ativo'
        SUSPENSO = 'SUSPENSO', 'Suspenso'
        ENCERRADO = 'ENCERRADO', 'Encerrado'

    organization = models.ForeignKey('platform_core.Organization', on_delete=models.PROTECT, related_name='contratos_saas')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RASCUNHO)
    inicio_em = models.DateField(null=True, blank=True)
    fim_em = models.DateField(null=True, blank=True)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='contratos_saas_criados')
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-criado_em', '-pk']

    def clean(self):
        super().clean()
        if self.inicio_em and self.fim_em and self.fim_em < self.inicio_em:
            raise ValidationError({'fim_em': 'O encerramento não pode ser anterior ao início do contrato.'})


class ItemContratoSaaS(models.Model):
    """One ERP and its own plan/entitlements inside a customer contract."""
    contrato = models.ForeignKey(ContratoSaaS, on_delete=models.PROTECT, related_name='itens')
    erp = models.ForeignKey('platform_core.ERPCatalog', on_delete=models.PROTECT, related_name='itens_contrato_saas')
    plano = models.ForeignKey(PlanoSaaS, on_delete=models.PROTECT, related_name='itens_contrato_saas')
    ativo = models.BooleanField(default=True)
    modulos_snapshot = models.JSONField(default=list, blank=True)
    limites_snapshot = models.JSONField(default=dict, blank=True)
    nivel_indicadores_snapshot = models.CharField(
        max_length=12, choices=ANALYTICS_LEVELS, default='BASICO',
        verbose_name='Nível de indicadores contratado',
    )
    preco_snapshot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    periodicidade_snapshot = models.CharField(max_length=8, choices=PlanoSaaS.Period.choices)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['erp__name', 'pk']
        constraints = [models.UniqueConstraint(fields=['contrato', 'erp'], name='saas_contract_erp_unique')]

    def clean(self):
        super().clean()
        if self.plano_id and self.erp_id and self.plano.erp_id != self.erp_id:
            raise ValidationError({'plano': 'O plano deve pertencer ao produto contratado neste item.'})
        if not isinstance(self.modulos_snapshot, list) or not isinstance(self.limites_snapshot, dict):
            raise ValidationError('Snapshots de módulos e limites precisam ser lista e objeto, respectivamente.')
        if self.nivel_indicadores_snapshot not in dict(ANALYTICS_LEVELS):
            raise ValidationError({'nivel_indicadores_snapshot': 'Nível de indicadores contratado inválido.'})
        if self.preco_snapshot is not None and self.preco_snapshot < 0:
            raise ValidationError({'preco_snapshot': 'O preço contratado não pode ser negativo.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                'contrato_id', 'erp_id', 'plano_id', 'modulos_snapshot', 'limites_snapshot',
                'nivel_indicadores_snapshot', 'preco_snapshot', 'periodicidade_snapshot',
            ).first()
            if previous and any(previous[field] != getattr(self, field) for field in previous):
                raise ValidationError('Snapshots comerciais do item são imutáveis; crie uma nova versão de contrato.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Itens de contrato são preservados para manter o histórico comercial.')


class AssinaturaSaaS(models.Model):
    class Status(models.TextChoices):
        IMPLANTACAO = 'IMPLANTACAO', 'Em implantação'
        ACTIVE = 'ATIVA', 'Ativa'
        SUSPENDED = 'SUSPENSA', 'Suspensa'

    organization = models.OneToOneField('platform_core.Organization', on_delete=models.PROTECT, related_name='assinatura_saas')
    plano = models.ForeignKey(PlanoSaaS, on_delete=models.PROTECT, related_name='assinaturas')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IMPLANTACAO)
    periodicidade = models.CharField(max_length=8, choices=PlanoSaaS.Period.choices, default=PlanoSaaS.Period.MONTH)
    inicio_em = models.DateTimeField(default=timezone.now)
    vencimento_em = models.DateField(null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'vínculo SaaS'
        verbose_name_plural = 'vínculos SaaS'

    def clean(self):
        if self.plano_id and not self.plano.ativo and not self.pk:
            raise ValidationError({'plano': 'Não é possível contratar um plano inativo.'})


class HistoricoPlanoSaaS(models.Model):
    assinatura = models.ForeignKey(AssinaturaSaaS, on_delete=models.PROTECT, related_name='historico_planos')
    plano_anterior = models.ForeignKey(PlanoSaaS, on_delete=models.PROTECT, null=True, blank=True, related_name='historico_anterior')
    plano_novo = models.ForeignKey(PlanoSaaS, on_delete=models.PROTECT, related_name='historico_novo')
    anterior = models.JSONField(default=dict)
    posterior = models.JSONField(default=dict)
    motivo = models.CharField(max_length=300, blank=True)
    alterado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    criado_em = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Histórico de plano é imutável.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Histórico de plano não pode ser apagado.')


class PrimeiroAcessoSaaS(models.Model):
    organization = models.ForeignKey('platform_core.Organization', on_delete=models.PROTECT, related_name='primeiros_acessos_saas')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='primeiros_acessos_saas')
    token_digest = models.CharField(max_length=64, unique=True)
    expira_em = models.DateTimeField()
    usado_em = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']

    def clean(self):
        if not re.fullmatch(r'[0-9a-f]{64}', self.token_digest or ''):
            raise ValidationError({'token_digest': 'Armazene somente o SHA-256 do token.'})
        if self.user_id and self.organization_id and not self.access_is_active():
            raise ValidationError({'user': 'O usuário deve possuir vínculo e acesso ativos na organização do convite.'})

    def access_is_active(self):
        from .models import OrganizationMembership
        if not self.user.is_active or self.organization.status != 'ATIVA':
            return False
        membership = OrganizationMembership.objects.filter(
            user_id=self.user_id, organization_id=self.organization_id, status='ATIVO',
        ).first()
        if membership is None:
            return False
        if membership.role == 'ADMIN':
            return True  # Preserve the initial tenant administrator flow.
        from .access import has_erp_access
        if membership.role in ('TECNICO', 'ESTOQUE', 'FINANCEIRO', 'ATENDIMENTO', 'VENDEDOR'):
            return not self.user.is_staff and not self.user.is_superuser and has_erp_access(
                self.user, self.organization, code='assistencia',
            )
        from rh_municipal.models import AcessoMunicipal
        return AcessoMunicipal.all_objects.filter(
            membership=membership, papel=AcessoMunicipal.Papel.ADMIN_PREFEITURA,
            escopo=AcessoMunicipal.Escopo.PREFEITURA, ativo=True,
        ).exists()

    @staticmethod
    def digest(raw_token):
        return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


class AuditoriaSaaS(models.Model):
    organization = models.ForeignKey('platform_core.Organization', on_delete=models.PROTECT, null=True, blank=True, related_name='auditoria_saas')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name='auditoria_saas')
    action = models.CharField(max_length=80)
    record = models.CharField(max_length=160)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'auditoria SaaS'
        verbose_name_plural = 'auditoria SaaS'

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError('Auditoria SaaS é imutável.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Auditoria SaaS não pode ser apagada.')


class CobrancaSaaS(models.Model):
    class Metodo(models.TextChoices):
        PIX = 'PIX', 'Pix Instantâneo'
        BOLETO = 'BOLETO', 'Boleto Bancário'
        CARTAO = 'CARTAO', 'Cartão de Crédito'

    class Status(models.TextChoices):
        PENDENTE = 'PENDENTE', 'Pendente'
        PAGA = 'PAGA', 'Paga / Confirmada'
        CANCELADA = 'CANCELADA', 'Cancelada'
        EXPIRADA = 'EXPIRADA', 'Expirada'

    organization = models.ForeignKey('platform_core.Organization', on_delete=models.CASCADE, related_name='cobrancas_saas')
    assinatura = models.ForeignKey(AssinaturaSaaS, on_delete=models.CASCADE, related_name='cobrancas')
    valor = models.DecimalField('Valor', max_digits=12, decimal_places=2)
    metodo = models.CharField(max_length=20, choices=Metodo.choices, default=Metodo.PIX)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDENTE)
    gateway = models.CharField(max_length=30, default='ASAAS')
    gateway_id = models.CharField(max_length=100, blank=True, db_index=True)
    pix_copia_cola = models.TextField(blank=True)
    pix_qr_code_url = models.URLField(max_length=500, blank=True)
    linha_digitavel = models.CharField(max_length=100, blank=True)
    url_fatura = models.URLField(max_length=500, blank=True)
    vencimento = models.DateField(null=True, blank=True)
    pago_em = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']
        verbose_name = 'cobrança SaaS'
        verbose_name_plural = 'cobranças SaaS'

    def __str__(self):
        return f"Cobrança #{self.pk} - {self.organization.display_name} - R$ {self.valor} ({self.status})"

    def marcar_como_paga(self, actor=None):
        if self.status == self.Status.PAGA:
            return
        hoje = timezone.localdate()
        self.status = self.Status.PAGA
        self.pago_em = timezone.now()
        self.save(update_fields=['status', 'pago_em'])

        assinatura = self.assinatura
        dias = 365 if assinatura.periodicidade == PlanoSaaS.Period.YEAR else 30
        data_base = assinatura.vencimento_em if (assinatura.vencimento_em and assinatura.vencimento_em >= hoje) else hoje
        assinatura.vencimento_em = data_base + timezone.timedelta(days=dias)
        assinatura.status = AssinaturaSaaS.Status.ACTIVE
        assinatura.save(update_fields=['vencimento_em', 'status', 'atualizado_em'])

        AuditoriaSaaS.objects.create(
            organization=self.organization,
            actor=actor,
            action='COBRANCA_PAGA',
            record=f'Cobrança SaaS #{self.pk}',
            details={
                'valor': str(self.valor),
                'metodo': self.metodo,
                'gateway': self.gateway,
                'gateway_id': self.gateway_id,
                'novo_vencimento': str(assinatura.vencimento_em),
            }
        )


class AjusteModuloSaaS(models.Model):
    """Explicit per-client module grant or block managed by VERTYX SaaS Admin."""
    class Modo(models.TextChoices):
        ALLOW = 'LIBERADO', 'Liberado manualmente'
        DENY = 'BLOQUEADO', 'Bloqueado manualmente'

    organization = models.ForeignKey(
        'platform_core.Organization',
        on_delete=models.CASCADE,
        related_name='ajustes_modulos_saas',
    )
    codigo = models.CharField(max_length=40, choices=sorted(MODULE_LABELS.items()))
    modo = models.CharField(max_length=12, choices=Modo.choices)
    motivo = models.CharField(max_length=255, blank=True)
    atualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='ajustes_modulos_saas',
    )
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['codigo']
        verbose_name = 'ajuste manual de módulo SaaS'
        verbose_name_plural = 'ajustes manuais de módulos SaaS'
        constraints = [
            models.UniqueConstraint(fields=['organization', 'codigo'], name='ajuste_modulo_saas_org_codigo_unico'),
        ]

    def __str__(self):
        return f'{self.organization_id}:{self.codigo}={self.modo}'
