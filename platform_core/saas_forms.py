from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from .models import (
    ERPProductModule, ERPProductResource, ERPCatalog, Organization,
    OrganizationMembership, ModuloOrganizacao,
)
from commercial.models import PlanoComercial
from .saas_models import (
    CORE_SAAS_MODULES, MODULES, ContratoSaaS, ItemContratoSaaS,
    PlanoProdutoModulo, PlanoSaaS,
)
from .validators import normalize_cnpj, validate_cnpj


class DadosEmpresaForm(forms.Form):
    razao_social = forms.CharField(label='Razão social', max_length=255)
    nome_fantasia = forms.CharField(label='Nome fantasia', max_length=150, required=False)
    cnpj = forms.CharField(label='CNPJ', max_length=18, validators=[validate_cnpj])
    email = forms.EmailField(label='E-mail da empresa')
    telefone = forms.CharField(label='Telefone / WhatsApp', max_length=30)
    endereco = forms.CharField(label='Endereço', max_length=255)
    cidade = forms.CharField(label='Cidade', max_length=100)
    uf = forms.CharField(label='UF', max_length=2, min_length=2)

    def clean_cnpj(self):
        value = normalize_cnpj(self.cleaned_data['cnpj'])
        if Organization.objects.filter(cnpj=value).exists():
            raise ValidationError('Já existe uma organização com este CNPJ.')
        return value

    def clean_uf(self):
        return self.cleaned_data['uf'].strip().upper()


class AdminInicialForm(forms.Form):
    nome = forms.CharField(label='Nome do administrador', max_length=150)
    email = forms.EmailField(label='E-mail de acesso')
    whatsapp = forms.CharField(label='WhatsApp do administrador', max_length=20)

    def clean_email(self):
        email = self.cleaned_data['email'].strip().casefold()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise ValidationError('Este e-mail já possui uma conta. Use um e-mail exclusivo para o administrador inicial.')
        return email


class BootstrapPrefeituraForm(forms.Form):
    """Select an existing organization membership for platform bootstrap."""
    organization = forms.ModelChoiceField(
        label='Organização / Prefeitura',
        queryset=Organization.objects.none(),
        empty_label='Selecione uma organização',
    )
    membership = forms.ModelChoiceField(
        label='Membership ativo',
        queryset=OrganizationMembership.objects.none(),
        empty_label='Selecione um usuário',
    )

    def __init__(self, *args, organization_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['organization'].queryset = Organization.objects.filter(
            status=Organization.Status.ACTIVE,
        ).order_by('razao_social', 'pk')
        value = organization_id
        if value is None:
            value = self.data.get(self.add_prefix('organization')) if self.is_bound else self.initial.get('organization')
        try:
            organization = self.fields['organization'].queryset.filter(pk=value).first()
        except (TypeError, ValueError):
            organization = None
        self.organization = organization
        if organization:
            self.fields['membership'].queryset = OrganizationMembership.objects.filter(
                organization=organization,
                status=OrganizationMembership.Status.ACTIVE,
                user__is_active=True,
            ).select_related('user').order_by('user__first_name', 'user__last_name', 'user__email', 'pk')

    def clean(self):
        cleaned = super().clean()
        organization = cleaned.get('organization')
        membership = cleaned.get('membership')
        if organization and membership and membership.organization_id != organization.pk:
            self.add_error('membership', 'O usuário selecionado não pertence à organização escolhida.')
        return cleaned


class NovaPrefeituraHomologacaoForm(forms.Form):
    """Platform-only, proposal-based municipal tenant provisioning."""
    razao_social = forms.CharField(label='Nome da Prefeitura', max_length=255)
    nome_fantasia = forms.CharField(label='Nome fantasia', max_length=150, required=False)
    administrador_nome = forms.CharField(label='Nome do primeiro administrador', max_length=150)
    administrador_email = forms.EmailField(label='E-mail do primeiro acesso')

    def clean_razao_social(self):
        return self.cleaned_data['razao_social'].strip()

    def clean_nome_fantasia(self):
        return self.cleaned_data['nome_fantasia'].strip()

    def clean_administrador_nome(self):
        return self.cleaned_data['administrador_nome'].strip()

    def clean_administrador_email(self):
        email = self.cleaned_data['administrador_email'].strip().casefold()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise ValidationError('Este e-mail já está associado a uma conta VERTYX. Nenhum vínculo foi criado.')
        return email


class PrimeiroAdministradorPlataformaForm(forms.Form):
    nome = forms.CharField(label='Nome completo', max_length=300)
    username = forms.CharField(label='Usuário de acesso', max_length=150)
    email = forms.EmailField(label='E-mail', max_length=254)
    senha = forms.CharField(
        label='Senha', strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )
    confirmar_senha = forms.CharField(
        label='Confirme a senha', strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )
    chave_bootstrap = forms.CharField(
        label='Chave de ativação', required=False, strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'one-time-code'}),
    )

    def clean_username(self):
        value = self.cleaned_data['username'].strip()
        User = get_user_model()
        if User._default_manager.filter(username__iexact=value).exists():
            raise ValidationError('Este nome de usuário já está em uso.')
        return value

    def clean_email(self):
        value = self.cleaned_data['email'].strip().casefold()
        User = get_user_model()
        if User._default_manager.filter(email__iexact=value).exists():
            raise ValidationError('Este e-mail já está associado a uma conta VERTYX.')
        return value

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get('senha')
        confirmation = cleaned.get('confirmar_senha')
        if password and confirmation and password != confirmation:
            self.add_error('confirmar_senha', 'As senhas não coincidem.')
        if password and not self.errors:
            User = get_user_model()
            name_parts = cleaned.get('nome', '').strip().split()
            user = User(
                username=cleaned.get('username', ''),
                email=cleaned.get('email', ''),
                first_name=name_parts[0][:150] if name_parts else '',
                last_name=' '.join(name_parts[1:])[:150],
            )
            try:
                password_validation.validate_password(password, user)
            except ValidationError as exc:
                self.add_error('senha', exc)
        return cleaned


class PlanoSaaSForm(forms.ModelForm):
    modulos = forms.MultipleChoiceField(
        label='Módulos incluídos', required=False,
        choices=[(value, label) for value, label in ModuloOrganizacao._meta.get_field('codigo').choices],
        widget=forms.CheckboxSelectMultiple,
        help_text=('PDV, Fiscal, Clientes, Produtos, Estoque básico e Financeiro básico são sempre incluídos. '
                   'O plano concede módulos; membership continua definindo permissões individuais. '
                   'Fiscal disponível não significa transmissão configurada. '
                   'Gestão Pública e RH Municipal ficam sob proposta.'),
    )
    limite_usuarios = forms.IntegerField(label='Limite de usuários', min_value=1, required=False, help_text='Deixe vazio para A definir.')
    limite_filiais = forms.IntegerField(label='Limite de filiais', min_value=0, required=False, help_text='Deixe vazio para A definir.')
    limite_armazenamento_mb = forms.IntegerField(label='Armazenamento (MB)', min_value=0, required=False, help_text='Deixe vazio para A definir.')
    limite_vendas_mes = forms.IntegerField(label='Vendas por mês', min_value=0, required=False, help_text='Considera vendas confirmadas/finalizadas no mês; vazio = A definir.')
    limite_ordens_mes = forms.IntegerField(label='Ordens de serviço por mês', min_value=0, required=False, help_text='Considera OS formalizadas no mês, inclusive canceladas; vazio = A definir.')
    modulos_produto = forms.ModelMultipleChoiceField(
        label='Módulos deste produto', required=False,
        queryset=ERPProductModule.objects.filter(active=True, erp__active=True).select_related('erp'),
        widget=forms.CheckboxSelectMultiple,
        help_text='Selecione módulos explicitamente cadastrados para o produto. Isso não concede permissões individuais.',
    )

    class Meta:
        model = PlanoSaaS
        fields = ['slug', 'nome', 'descricao', 'erp', 'plano_comercial', 'preco', 'periodicidade',
                  'publico_ativo', 'ativo', 'nivel_indicadores']
        widgets = {'descricao': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        commercial_qs=PlanoComercial.objects.filter(ativo=True)
        if self.instance and self.instance.plano_comercial_id:
            commercial_qs=(PlanoComercial.objects.filter(ativo=True) |
                           PlanoComercial.objects.filter(pk=self.instance.plano_comercial_id))
        self.fields['plano_comercial'].queryset = commercial_qs.order_by('ordem','nome')
        self.fields['plano_comercial'].required = False
        self.fields['plano_comercial'].empty_label = 'Não vinculado'
        self.fields['erp'].queryset = ERPCatalog.objects.filter(active=True).order_by('name')
        self.fields['erp'].required = False
        self.fields['erp'].empty_label = 'Plano legado sem produto classificado'
        selected_product = self.data.get('erp') if self.is_bound else (self.instance.erp_id if self.instance.pk else None)
        module_qs = ERPProductModule.objects.filter(active=True, erp__active=True).select_related('erp')
        if selected_product:
            module_qs = module_qs.filter(erp_id=selected_product)
        self.fields['modulos_produto'].queryset = module_qs.order_by('erp__name', 'sort_order', 'name')
        if self.instance and self.instance.pk:
            self.initial['modulos'] = self.instance.modulos
            self.initial['modulos_produto'] = self.instance.modulos_produto.values_list('modulo_id', flat=True)
            for key in ('usuarios', 'filiais', 'armazenamento_mb', 'vendas_mes', 'ordens_mes'):
                self.initial[f'limite_{key}'] = self.instance.limites.get(key)
        else:
            self.initial['modulos'] = list(CORE_SAAS_MODULES)

    def clean_slug(self):
        return slugify(self.cleaned_data['slug'])

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('limite_usuarios') == 0:
            self.add_error('limite_usuarios', 'O plano precisa comportar pelo menos o administrador inicial.')
        if cleaned.get('modulos'):
            cleaned['modulos'] = list(dict.fromkeys(cleaned['modulos']))
        # Keep the operational core checked even when a client omits disabled
        # or manually forged values from the form submission.
        cleaned['modulos'] = list(dict.fromkeys([
            *CORE_SAAS_MODULES, *(cleaned.get('modulos') or [])
        ]))
        product = cleaned.get('erp')
        selected_modules = cleaned.get('modulos_produto')
        if selected_modules and not product:
            self.add_error('erp', 'Classifique o produto antes de atribuir módulos específicos.')
        elif product and selected_modules and any(module.erp_id != product.pk for module in selected_modules):
            self.add_error('modulos_produto', 'Todos os módulos devem pertencer ao produto selecionado.')
        if cleaned.get('publico_ativo'):
            if product and product.code != 'assistencia':
                self.add_error('publico_ativo', 'A contratação multierp ficará desabilitada até que o checkout por produto esteja disponível.')
            restricted_public_modules = {'gestao_publica', 'rh_municipal'} & set(cleaned.get('modulos') or ())
            if restricted_public_modules:
                self.add_error('modulos', 'Gestão Pública/RH Municipal ficam sob proposta. O módulo Fiscal não habilita transmissão.')
            commercial=cleaned.get('plano_comercial')
            if commercial is None:
                self.add_error('plano_comercial','Vincule um plano comercial para liberar o cadastro público.')
            elif not commercial.ativo:
                self.add_error('plano_comercial','O plano comercial vinculado está inativo.')
            else:
                public_price=(commercial.valor_anual if cleaned.get('periodicidade')==PlanoSaaS.Period.YEAR
                              else commercial.preco)
                if public_price is None or cleaned.get('preco') != public_price:
                    self.add_error('preco','O preço precisa coincidir com o preço público da periodicidade selecionada.')
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.modulos = list(self.cleaned_data.get('modulos', []))
        instance.limites = {
            'usuarios': self.cleaned_data.get('limite_usuarios'),
            'filiais': self.cleaned_data.get('limite_filiais'),
            'armazenamento_mb': self.cleaned_data.get('limite_armazenamento_mb'),
            'vendas_mes': self.cleaned_data.get('limite_vendas_mes'),
            'ordens_mes': self.cleaned_data.get('limite_ordens_mes'),
        }
        if commit:
            instance.save()
            self.save_product_modules(instance)
        return instance

    def save_product_modules(self, instance):
        selected = {module.pk: module for module in self.cleaned_data.get('modulos_produto', [])}
        existing = {
            row.modulo_id: row
            for row in PlanoProdutoModulo.objects.filter(plano=instance)
        }
        for module_id in existing.keys() - selected.keys():
            existing[module_id].delete()
        for module_id in selected.keys() - existing.keys():
            PlanoProdutoModulo.objects.create(plano=instance, modulo=selected[module_id])


class ERPCatalogForm(forms.ModelForm):
    class Meta:
        model = ERPCatalog
        fields = ['code', 'name', 'description', 'active', 'operational', 'public_path', 'entry_path']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('operational') and not cleaned.get('entry_path'):
            self.add_error('entry_path', 'Informe uma rota inicial antes de liberar o produto para entrada.')
        for field_name in ('public_path', 'entry_path'):
            value = cleaned.get(field_name, '')
            if value and (not value.startswith('/') or value.startswith('//') or '..' in value.split('/')):
                self.add_error(field_name, 'Use uma rota relativa local, sem host externo ou segmentos de caminho pai.')
        if self.instance.pk and cleaned.get('code') != self.instance.code and (
            self.instance.organizationerp_set.exists() or self.instance.membershiperp_set.exists()
        ):
            self.add_error('code', 'O código é imutável depois que o produto recebe vínculos.')
        return cleaned


class ERPProductModuleForm(forms.ModelForm):
    class Meta:
        model = ERPProductModule
        fields = ['erp', 'code', 'name', 'description', 'active', 'sort_order']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, product=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['erp'].queryset = ERPCatalog.objects.filter(active=True).order_by('name')
        if product:
            self.fields['erp'].initial = product.pk
            self.fields['erp'].disabled = True


class ERPProductResourceForm(forms.ModelForm):
    class Meta:
        model = ERPProductResource
        fields = ['module', 'code', 'name', 'description', 'active', 'sort_order']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, product=None, module=None, **kwargs):
        super().__init__(*args, **kwargs)
        modules = ERPProductModule.objects.filter(active=True, erp__active=True)
        if product:
            modules = modules.filter(erp=product)
        if module:
            modules = modules.filter(pk=module.pk)
            self.fields['module'].initial = module.pk
            self.fields['module'].disabled = True
        self.fields['module'].queryset = modules.select_related('erp').order_by('erp__name', 'name')


class ContratoSaaSForm(forms.Form):
    organization = forms.ModelChoiceField(
        label='Organização', queryset=Organization.objects.filter(status=Organization.Status.ACTIVE).order_by('nome_fantasia', 'razao_social'),
    )
    inicio_em = forms.DateField(label='Início previsto', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    itens = forms.MultipleChoiceField(label='Produtos e planos', choices=(), widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = []
        self._plan_by_pair = {}
        plans = PlanoSaaS.objects.filter(ativo=True, erp__active=True).select_related('erp').prefetch_related('modulos_produto__modulo')
        for plan in plans:
            key = f'{plan.erp_id}:{plan.pk}'
            choices.append((key, f'{plan.erp.name} · {plan.nome} · {plan.preco if plan.preco is not None else "Preço a definir"} / {plan.get_periodicidade_display().lower()}'))
            self._plan_by_pair[key] = plan
        self.fields['itens'].choices = choices

    def clean_itens(self):
        values = self.cleaned_data['itens']
        if not values:
            raise ValidationError('Selecione ao menos um produto e plano.')
        product_ids = []
        plans = []
        for value in values:
            plan = self._plan_by_pair.get(value)
            if plan is None:
                raise ValidationError('Produto/plano inválido ou inativo.')
            if plan.erp_id in product_ids:
                raise ValidationError('Selecione somente um plano por produto no mesmo contrato.')
            product_ids.append(plan.erp_id)
            plans.append(plan)
        self.cleaned_data['selected_plans'] = plans
        return values

    def save(self, *, actor):
        from django.db import transaction
        with transaction.atomic():
            contract = ContratoSaaS.objects.create(
                organization=self.cleaned_data['organization'],
                inicio_em=self.cleaned_data.get('inicio_em'),
                criado_por=actor,
                status=ContratoSaaS.Status.RASCUNHO,
            )
            for plan in self.cleaned_data['selected_plans']:
                modules = list(plan.modulos_produto.values_list('modulo__code', flat=True))
                ItemContratoSaaS.objects.create(
                    contrato=contract, erp=plan.erp, plano=plan,
                    modulos_snapshot=modules, limites_snapshot=plan.limites,
                    nivel_indicadores_snapshot=plan.nivel_indicadores,
                    preco_snapshot=plan.preco, periodicidade_snapshot=plan.periodicidade,
                )
        return contract


class EditarClienteForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ['razao_social', 'nome_fantasia', 'cnpj', 'email', 'telefone', 'endereco', 'cidade', 'uf']


class EditarModulosClienteForm(forms.Form):
    modulos = forms.MultipleChoiceField(
        label='Módulos habilitados para este cliente',
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    erps = forms.MultipleChoiceField(
        label='Produtos ERP habilitados para a organização',
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    motivo = forms.CharField(
        label='Observação / motivo do ajuste (opcional)',
        required=False,
        max_length=255,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .saas_models import MODULE_LABELS
        self.fields['modulos'].choices = sorted(MODULE_LABELS.items())
        self.fields['erps'].choices = [
            (erp.code, erp.name)
            for erp in ERPCatalog.objects.filter(active=True).order_by('-operational', 'name')
        ]


class AlterarPlanoForm(forms.Form):
    plano = forms.ModelChoiceField(label='Novo plano', queryset=PlanoSaaS.objects.none())
    motivo = forms.CharField(label='Motivo (opcional)', required=False, max_length=300, widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['plano'].queryset = PlanoSaaS.objects.filter(ativo=True)
