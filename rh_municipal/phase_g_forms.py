from django import forms
from django.utils import timezone
from decimal import Decimal
import uuid

from platform_core.context import current_organization
from . import phase_g_models as health
from . import phase_f_models as territory
from .models import AcessoMunicipal, DepartamentoMunicipal, Secretaria, Servidor, SetorMunicipal, UnidadeMunicipal
from .public_services import accesses_for, has_municipal_permission, scoped_queryset


class HealthBrandingForm(forms.Form):
    nome_institucional = forms.CharField(max_length=120)
    orgao_gestor = forms.CharField(max_length=120, required=False)
    unidade_nome = forms.CharField(max_length=120)
    municipio = forms.CharField(max_length=100)
    uf = forms.RegexField(r'^[A-Z]{2}$', max_length=2)
    cnpj = forms.CharField(max_length=18, required=False)
    codigo_ibge = forms.RegexField(r'^\d{0,7}$', max_length=7, required=False)
    cor_primaria = forms.RegexField(r'^#[0-9a-fA-F]{6}$', required=False)
    cor_secundaria = forms.RegexField(r'^#[0-9a-fA-F]{6}$', required=False)
    slogan = forms.CharField(max_length=200, required=False)


class _HealthScopedForm(forms.ModelForm):
    """Forms only offer related records in the active tenant and user scope."""

    def __init__(self, *args, actor=None, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if getattr(field.widget, 'input_type', None) != 'hidden':
                field.widget.attrs['class'] = 'gp-input'

    def scoped(self, model, *, active=True):
        organization = current_organization()
        rows = model.objects.filter(organization=organization)
        if active and any(field.name == 'ativo' for field in model._meta.fields):
            rows = rows.filter(ativo=True)
        return scoped_queryset(self.actor, rows) if organization and self.actor else model.objects.none()


class PatientForm(_HealthScopedForm):
    class Meta:
        model = health.CidadaoSaudeMunicipal
        fields = (
            'unidade', 'nome', 'cpf', 'cns', 'data_nascimento', 'telefone', 'email',
            'cep', 'endereco', 'bairro', 'municipio', 'uf',
        )
        widgets = {
            'unidade': forms.Select(attrs={'class': 'gp-input'}),
            'nome': forms.TextInput(attrs={'class': 'gp-input', 'autocomplete': 'name'}),
            'cpf': forms.TextInput(attrs={'class': 'gp-input', 'inputmode': 'numeric', 'autocomplete': 'off'}),
            'cns': forms.TextInput(attrs={'class': 'gp-input', 'inputmode': 'numeric', 'autocomplete': 'off'}),
            'data_nascimento': forms.DateInput(attrs={'class': 'gp-input', 'type': 'date'}),
            'telefone': forms.TextInput(attrs={'class': 'gp-input', 'autocomplete': 'tel'}),
            'email': forms.EmailInput(attrs={'class': 'gp-input', 'autocomplete': 'email'}),
            'cep': forms.TextInput(attrs={'class': 'gp-input', 'autocomplete': 'postal-code'}),
            'endereco': forms.TextInput(attrs={'class': 'gp-input', 'autocomplete': 'street-address'}),
            'bairro': forms.TextInput(attrs={'class': 'gp-input'}),
            'municipio': forms.TextInput(attrs={'class': 'gp-input', 'autocomplete': 'address-level2'}),
            'uf': forms.TextInput(attrs={'class': 'gp-input', 'maxlength': '2', 'autocomplete': 'address-level1'}),
        }

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, actor=actor, **kwargs)
        accesses = list(accesses_for(actor)) if actor else []
        self.fields['unidade'].required = bool(accesses) and not any(
            access.escopo == AcessoMunicipal.Escopo.PREFEITURA for access in accesses
        )
        self.fields['unidade'].queryset = self.scoped(UnidadeMunicipal)

    def clean_cpf(self):
        return ''.join(character for character in self.cleaned_data.get('cpf', '') if character.isdigit())

    def clean_cns(self):
        return ''.join(character for character in self.cleaned_data.get('cns', '') if character.isdigit())


class HealthUnitForm(_HealthScopedForm):
    class Meta:
        model = health.UnidadeSaudeMunicipal
        fields = ('unidade', 'tipo', 'cnes', 'servicos', 'ativo')
        widgets = {
            'cnes': forms.TextInput(attrs={'class': 'gp-input'}),
            'servicos': forms.Textarea(attrs={'class': 'gp-input', 'rows': 3,
                                              'placeholder': '["Vacinação", "Consulta"]'}),
        }

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, actor=actor, **kwargs)
        existing = health.UnidadeSaudeMunicipal.objects.filter(
            organization=current_organization(),
        ).values_list('unidade_id', flat=True)
        self.fields['unidade'].queryset = self.scoped(UnidadeMunicipal).exclude(pk__in=existing)

    def clean_servicos(self):
        return self.cleaned_data.get('servicos') or []


class HealthTeamForm(_HealthScopedForm):
    class Meta:
        model = health.EquipeSaudeMunicipal
        fields = ('codigo', 'nome', 'tipo', 'unidade_saude', 'microarea', 'ativo')

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, actor=actor, **kwargs)
        self.fields['unidade_saude'].queryset = self.scoped(health.UnidadeSaudeMunicipal)


class HealthProfessionalForm(_HealthScopedForm):
    class Meta:
        model = health.ProfissionalSaudeMunicipal
        fields = ('servidor', 'unidade_saude', 'equipe', 'ocupacao', 'conselho',
                  'registro_conselho', 'especialidade', 'ativo')

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, actor=actor, **kwargs)
        self.fields['servidor'].queryset = self.scoped(Servidor)
        self.fields['unidade_saude'].queryset = self.scoped(health.UnidadeSaudeMunicipal)
        self.fields['equipe'].queryset = self.scoped(health.EquipeSaudeMunicipal)
        unit_id = self.data.get('unidade_saude') if self.is_bound else getattr(self.instance, 'unidade_saude_id', None)
        if unit_id:
            self.fields['equipe'].queryset = self.fields['equipe'].queryset.filter(unidade_saude_id=unit_id)

    def clean(self):
        cleaned = super().clean()
        team, unit = cleaned.get('equipe'), cleaned.get('unidade_saude')
        if team and unit and team.unidade_saude_id != unit.pk:
            self.add_error('equipe', 'A equipe selecionada pertence a outra unidade de saúde.')
        return cleaned


class HealthAgendaForm(_HealthScopedForm):
    class Meta:
        model = health.AgendaSaudeMunicipal
        fields = ('unidade_saude', 'profissional', 'inicio', 'fim', 'capacidade', 'situacao')
        widgets = {
            'inicio': forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'class': 'gp-input', 'type': 'datetime-local'}),
            'fim': forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'class': 'gp-input', 'type': 'datetime-local'}),
        }

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, actor=actor, **kwargs)
        self.fields['unidade_saude'].queryset = self.scoped(health.UnidadeSaudeMunicipal)
        self.fields['profissional'].queryset = self.scoped(health.ProfissionalSaudeMunicipal)
        unit_id = self.data.get('unidade_saude') if self.is_bound else getattr(self.instance, 'unidade_saude_id', None)
        if unit_id:
            self.fields['profissional'].queryset = self.fields['profissional'].queryset.filter(unidade_saude_id=unit_id)

    def clean(self):
        cleaned = super().clean()
        unit, professional, start = cleaned.get('unidade_saude'), cleaned.get('profissional'), cleaned.get('inicio')
        if unit and professional and professional.unidade_saude_id != unit.pk:
            self.add_error('profissional', 'O profissional não está vinculado à unidade selecionada.')
        if start and start <= timezone.now():
            self.add_error('inicio', 'A agenda deve começar em uma data futura.')
        return cleaned


class HealthAppointmentForm(_HealthScopedForm):
    request_id = forms.UUIDField(widget=forms.HiddenInput)

    class Meta:
        model = health.AtendimentoSaudeMunicipal
        fields = ('agenda', 'cidadao')

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, actor=actor, **kwargs)
        if not self.is_bound:
            import uuid
            self.fields['request_id'].initial = uuid.uuid4()
        self.fields['agenda'].queryset = self.scoped(health.AgendaSaudeMunicipal).filter(
            situacao='ABERTA', inicio__gt=timezone.now(),
        )
        self.fields['cidadao'].queryset = self.scoped(health.CidadaoSaudeMunicipal).filter(situacao='ATIVO')


class ClinicalEntryActionForm(forms.Form):
    action = forms.ChoiceField(
        choices=(('create', 'Registrar evolução'), ('read', 'Consultar evolução')),
        widget=forms.HiddenInput,
    )
    entry_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    content = forms.CharField(
        required=False, max_length=20000,
        widget=forms.Textarea(attrs={'class': 'gp-input', 'rows': 7,
                                     'autocomplete': 'off', 'spellcheck': 'false'}),
    )
    justification = forms.CharField(
        required=False, max_length=300,
        widget=forms.TextInput(attrs={'class': 'gp-input', 'autocomplete': 'off',
                                      'placeholder': 'Obrigatória para consultar evolução'}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('action') == 'create' and not (cleaned.get('content') or '').strip():
            self.add_error('content', 'Informe o texto da evolução clínica.')
        if cleaned.get('action') == 'read' and not (cleaned.get('justification') or '').strip():
            self.add_error('justification', 'Informe a justificativa para consultar o prontuário.')
        if cleaned.get('action') == 'read' and not cleaned.get('entry_id'):
            self.add_error('entry_id', 'Selecione uma evolução válida.')
        return cleaned


class HealthMedicineForm(_HealthScopedForm):
    class Meta:
        model = health.MedicamentoMunicipal
        fields = ('codigo', 'nome', 'apresentacao', 'unidade_medida', 'estoque_minimo', 'ativo')
        widgets = {
            'codigo': forms.TextInput(attrs={'class': 'gp-input'}),
            'nome': forms.TextInput(attrs={'class': 'gp-input'}),
            'apresentacao': forms.TextInput(attrs={'class': 'gp-input'}),
            'unidade_medida': forms.TextInput(attrs={'class': 'gp-input'}),
            'estoque_minimo': forms.NumberInput(attrs={'class': 'gp-input', 'min': '0', 'step': '0.001'}),
        }


class HealthLotReceiveForm(forms.Form):
    medicamento = forms.ModelChoiceField(queryset=health.MedicamentoMunicipal.objects.none(), label='Medicamento')
    unidade_saude = forms.ModelChoiceField(queryset=health.UnidadeSaudeMunicipal.objects.none(), label='Unidade de saúde')
    lot = forms.CharField(max_length=60, label='Lote')
    expires_on = forms.DateField(label='Validade', widget=forms.DateInput(attrs={'class': 'gp-input', 'type': 'date'}))
    quantity = forms.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0.001'), label='Quantidade recebida', widget=forms.NumberInput(attrs={'class': 'gp-input', 'min': '0.001', 'step': '0.001'}))

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        organization = current_organization()
        for field in self.fields.values():
            if field.widget.input_type != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        if organization and actor:
            self.fields['medicamento'].queryset = health.MedicamentoMunicipal.objects.filter(
                organization=organization, ativo=True,
            ).order_by('nome')
            self.fields['unidade_saude'].queryset = scoped_queryset(
                actor, health.UnidadeSaudeMunicipal.objects.filter(organization=organization, ativo=True),
            ).order_by('unidade__nome')


class HealthDispensationForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    cidadao = forms.ModelChoiceField(queryset=health.CidadaoSaudeMunicipal.objects.none(), label='Cidadão/paciente')
    unidade_saude = forms.ModelChoiceField(queryset=health.UnidadeSaudeMunicipal.objects.none(), label='Unidade dispensadora')
    medicamento = forms.ModelChoiceField(queryset=health.MedicamentoMunicipal.objects.none(), label='Medicamento')
    quantidade = forms.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal('0.001'), label='Quantidade', widget=forms.NumberInput(attrs={'class': 'gp-input', 'min': '0.001', 'step': '0.001'}))

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['request_id'].initial = uuid.uuid4()
        for field in self.fields.values():
            if field.widget.input_type != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        organization = current_organization()
        if organization and actor:
            if has_municipal_permission(actor, 'saude.patients.view', module=health.HEALTH_MODULE):
                self.fields['cidadao'].queryset = scoped_queryset(
                    actor, health.CidadaoSaudeMunicipal.objects.filter(organization=organization, situacao='ATIVO'),
                ).order_by('nome')
            self.fields['unidade_saude'].queryset = scoped_queryset(
                actor, health.UnidadeSaudeMunicipal.objects.filter(organization=organization, ativo=True),
            ).order_by('unidade__nome')
            self.fields['medicamento'].queryset = health.MedicamentoMunicipal.objects.filter(
                organization=organization, ativo=True,
            ).order_by('nome')


class HealthFieldVisitForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    tipo = forms.ChoiceField(label='Tipo de visita', choices=health.VisitaTerritorialSaude.Tipo.choices)
    agente = forms.ModelChoiceField(queryset=health.ProfissionalSaudeMunicipal.objects.none(), label='Agente responsável')
    familia = forms.ModelChoiceField(queryset=health.FamiliaTerritorialSaude.objects.none(), label='Família', required=False)
    cidadao = forms.ModelChoiceField(queryset=health.CidadaoSaudeMunicipal.objects.none(), label='Cidadão acompanhado', required=False)
    imovel = forms.ModelChoiceField(queryset=territory.ImovelMunicipal.objects.none(), label='Imóvel visitado', required=False)
    realizada_em = forms.DateTimeField(
        label='Data e hora da visita',
        widget=forms.DateTimeInput(attrs={'class': 'gp-input', 'type': 'datetime-local'}),
    )
    observacoes = forms.CharField(
        label='Registro da visita', max_length=4000,
        widget=forms.Textarea(attrs={
            'class': 'gp-input', 'rows': 4,
            'placeholder': 'Registre o resultado, acompanhamento ou providências. O conteúdo será cifrado.',
        }),
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['request_id'].initial = uuid.uuid4()
            self.fields['realizada_em'].initial = timezone.localtime().replace(second=0, microsecond=0)
        for field in self.fields.values():
            if getattr(field.widget, 'input_type', None) != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        organization = current_organization()
        if organization and actor:
            self.fields['agente'].queryset = scoped_queryset(
                actor, health.ProfissionalSaudeMunicipal.objects.filter(
                    organization=organization, ativo=True, servidor__ativo=True,
                    servidor__situacao_funcional='ATIVO', unidade_saude__ativo=True,
                ).select_related('servidor', 'equipe', 'unidade_saude__unidade'),
            ).order_by('servidor__nome')
            self.fields['familia'].queryset = scoped_queryset(
                actor, health.FamiliaTerritorialSaude.objects.filter(
                    organization=organization, ativo=True, unidade_saude__ativo=True,
                ).select_related('unidade_saude__unidade'),
            ).order_by('codigo')
            self.fields['cidadao'].queryset = scoped_queryset(
                actor, health.CidadaoSaudeMunicipal.objects.filter(
                    organization=organization, situacao='ATIVO',
                ),
            ).order_by('nome')
            self.fields['imovel'].queryset = scoped_queryset(
                actor, territory.ImovelMunicipal.objects.filter(
                    organization=organization, situacao=territory.ImovelMunicipal.Situacao.ATIVO,
                ),
            ).order_by('inscricao')

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get('tipo')
        agent = cleaned.get('agente')
        family = cleaned.get('familia')
        citizen = cleaned.get('cidadao')
        property_record = cleaned.get('imovel')
        if kind == health.VisitaTerritorialSaude.Tipo.ACS and not (family or citizen):
            self.add_error('familia', 'Para visita ACS, selecione a família ou o cidadão acompanhado.')
        if kind == health.VisitaTerritorialSaude.Tipo.ACE and not property_record:
            self.add_error('imovel', 'Para visita ACE, selecione o imóvel inspecionado.')
        if agent and agent.equipe_id:
            team_type = agent.equipe.tipo
            allowed = {'ACS', 'ESF'} if kind == health.VisitaTerritorialSaude.Tipo.ACS else {'ACE'}
            if team_type not in allowed:
                self.add_error('agente', 'O tipo de agente não corresponde à equipe selecionada.')
        if family and agent and family.unidade_saude_id != agent.unidade_saude_id:
            self.add_error('familia', 'A família não pertence à unidade de saúde do agente.')
        if citizen and family and not health.VinculoFamiliaSaude.objects.filter(
            organization=family.organization, familia=family, cidadao=citizen,
        ).exists():
            self.add_error('cidadao', 'O cidadão não está vinculado à família selecionada.')
        if citizen and agent and citizen.unidade_id and citizen.unidade_id != agent.unidade_saude.unidade_id:
            self.add_error('cidadao', 'O cidadão pertence a outra unidade de saúde.')
        if property_record and agent and property_record.organization_id != agent.organization_id:
            self.add_error('imovel', 'O imóvel pertence a outra Prefeitura.')
        return cleaned


class HealthSanitaryEstablishmentForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    contribuinte = forms.ModelChoiceField(
        queryset=territory.ContribuinteMunicipal.objects.none(), label='Contribuinte vinculado', required=False,
    )
    razao_social = forms.CharField(max_length=180, label='Nome/razão social')
    documento = forms.CharField(max_length=14, label='CPF/CNPJ', required=False)
    atividade = forms.CharField(max_length=120, label='Atividade', required=False)
    endereco = forms.CharField(max_length=220, label='Endereço')
    secretaria = forms.ModelChoiceField(queryset=Secretaria.objects.none(), label='Secretaria responsável')
    departamento = forms.ModelChoiceField(queryset=DepartamentoMunicipal.objects.none(), label='Departamento')
    setor = forms.ModelChoiceField(queryset=SetorMunicipal.objects.none(), label='Setor')
    unidade = forms.ModelChoiceField(queryset=UnidadeMunicipal.objects.none(), label='Unidade')

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['request_id'].initial = uuid.uuid4()
        for field in self.fields.values():
            if getattr(field.widget, 'input_type', None) != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        organization = current_organization()
        if not organization or not actor:
            return
        self.fields['contribuinte'].queryset = scoped_queryset(
            actor, territory.ContribuinteMunicipal.objects.filter(organization=organization, situacao='ATIVO'),
        ).order_by('nome')
        for name, model in (
            ('secretaria', Secretaria), ('departamento', DepartamentoMunicipal),
            ('setor', SetorMunicipal), ('unidade', UnidadeMunicipal),
        ):
            self.fields[name].queryset = scoped_queryset(actor, model.objects.filter(organization=organization))
        secretary_id = self.data.get('secretaria') if self.is_bound else None
        department_id = self.data.get('departamento') if self.is_bound else None
        sector_id = self.data.get('setor') if self.is_bound else None
        if secretary_id:
            self.fields['departamento'].queryset = self.fields['departamento'].queryset.filter(secretaria_id=secretary_id)
        if department_id:
            self.fields['setor'].queryset = self.fields['setor'].queryset.filter(departamento_id=department_id)
        if sector_id:
            self.fields['unidade'].queryset = self.fields['unidade'].queryset.filter(setor_id=sector_id)

    def clean(self):
        cleaned = super().clean()
        secretary, department = cleaned.get('secretaria'), cleaned.get('departamento')
        sector, unit = cleaned.get('setor'), cleaned.get('unidade')
        if department and secretary and department.secretaria_id != secretary.pk:
            self.add_error('departamento', 'O departamento não pertence à secretaria selecionada.')
        if sector and department and sector.departamento_id != department.pk:
            self.add_error('setor', 'O setor não pertence ao departamento selecionado.')
        if unit and sector and unit.setor_id != sector.pk:
            self.add_error('unidade', 'A unidade não pertence ao setor selecionado.')
        return cleaned


class HealthSanitaryInspectionForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    estabelecimento = forms.ModelChoiceField(
        queryset=health.EstabelecimentoSanitarioMunicipal.objects.none(), label='Estabelecimento',
    )
    realizada_por = forms.ModelChoiceField(
        queryset=health.ProfissionalSaudeMunicipal.objects.none(), label='Fiscal/profissional responsável',
    )
    realizada_em = forms.DateTimeField(
        label='Data e hora', widget=forms.DateTimeInput(attrs={'class': 'gp-input', 'type': 'datetime-local'}),
    )
    resultado = forms.ChoiceField(
        label='Resultado registrado', choices=health.InspecaoSanitariaMunicipal._meta.get_field('resultado').choices,
    )
    prazo_retorno = forms.DateField(
        label='Prazo de retorno', required=False,
        widget=forms.DateInput(attrs={'class': 'gp-input', 'type': 'date'}),
    )
    observacoes = forms.CharField(
        max_length=4000, label='Observações da inspeção',
        widget=forms.Textarea(attrs={'class': 'gp-input', 'rows': 4, 'placeholder': 'O conteúdo será cifrado.'}),
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['request_id'].initial = uuid.uuid4()
            self.fields['realizada_em'].initial = timezone.localtime().replace(second=0, microsecond=0)
        for field in self.fields.values():
            if getattr(field.widget, 'input_type', None) != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        organization = current_organization()
        if organization and actor:
            self.fields['estabelecimento'].queryset = scoped_queryset(
                actor, health.EstabelecimentoSanitarioMunicipal.objects.filter(
                    organization=organization,
                ).exclude(situacao='INATIVO'),
            ).order_by('razao_social')
            self.fields['realizada_por'].queryset = scoped_queryset(
                actor, health.ProfissionalSaudeMunicipal.objects.filter(
                    organization=organization, ativo=True, servidor__ativo=True,
                    servidor__situacao_funcional='ATIVO', unidade_saude__ativo=True,
                ).select_related('servidor', 'unidade_saude__unidade'),
            ).order_by('servidor__nome')


class HealthEpidemiologyNotificationForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    cidadao = forms.ModelChoiceField(
        queryset=health.CidadaoSaudeMunicipal.objects.none(), label='Cidadão relacionado', required=False,
    )
    codigo_agravo = forms.CharField(max_length=40, label='Código de referência local', required=False)
    descricao_agravo = forms.CharField(max_length=160, label='Descrição de referência local', required=False)
    notificada_em = forms.DateField(
        label='Data da notificação', widget=forms.DateInput(attrs={'class': 'gp-input', 'type': 'date'}),
    )
    detalhes = forms.CharField(
        max_length=4000, label='Detalhes restritos',
        widget=forms.Textarea(attrs={'class': 'gp-input', 'rows': 4, 'placeholder': 'Conteúdo cifrado; não use códigos clínicos não validados.'}),
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['request_id'].initial = uuid.uuid4()
            self.fields['notificada_em'].initial = timezone.localdate()
        for field in self.fields.values():
            if getattr(field.widget, 'input_type', None) != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        organization = current_organization()
        if organization and actor:
            self.fields['cidadao'].queryset = scoped_queryset(
                actor, health.CidadaoSaudeMunicipal.objects.filter(
                    organization=organization, situacao='ATIVO',
                ),
            ).order_by('nome')

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get('codigo_agravo') or '').strip() and not (cleaned.get('descricao_agravo') or '').strip():
            raise forms.ValidationError('Informe a referência local do agravo; o sistema não valida classificação oficial.')
        return cleaned


class HealthEpidemiologyTransitionForm(forms.Form):
    situacao = forms.ChoiceField(
        label='Nova situação', choices=(('EM_ANALISE', 'Em análise'), ('ENCERRADA', 'Encerrada')),
    )
    justificativa = forms.CharField(
        max_length=500, label='Justificativa da transição',
        widget=forms.Textarea(attrs={'class': 'gp-input', 'rows': 2}),
    )


class HealthTFDForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    cidadao = forms.ModelChoiceField(
        queryset=health.CidadaoSaudeMunicipal.objects.none(), label='Cidadão/paciente',
    )
    destino = forms.CharField(max_length=180, label='Destino do tratamento')
    tratamento = forms.CharField(max_length=180, label='Finalidade clínica')
    acompanhante = forms.CharField(max_length=180, label='Acompanhante', required=False)

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['request_id'].initial = uuid.uuid4()
        for field in self.fields.values():
            if getattr(field.widget, 'input_type', None) != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        organization = current_organization()
        if organization and actor and has_municipal_permission(actor, 'saude.patients.view', module=health.HEALTH_MODULE):
            self.fields['cidadao'].queryset = scoped_queryset(
                actor, health.CidadaoSaudeMunicipal.objects.filter(
                    organization=organization, situacao='ATIVO',
                ),
            ).order_by('nome')


class HealthTFDTransitionForm(forms.Form):
    status = forms.ChoiceField(
        label='Nova situação',
        choices=(('AUTORIZADA', 'Autorizar'), ('NEGADA', 'Negar'), ('CONCLUIDA', 'Concluir')),
    )
    justificativa = forms.CharField(
        max_length=500, label='Justificativa',
        widget=forms.Textarea(attrs={'class': 'gp-input', 'rows': 2}),
    )


class HealthProductionForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    competencia = forms.RegexField(
        regex=r'^\d{4}-(0[1-9]|1[0-2])$', max_length=7, label='Competência (AAAA-MM)',
        error_messages={'invalid': 'Use o formato AAAA-MM.'},
    )
    tipo = forms.ChoiceField(label='Tipo de registro', choices=health.RegistroProducaoSUS.Tipo.choices)
    unidade_saude = forms.ModelChoiceField(
        queryset=health.UnidadeSaudeMunicipal.objects.none(), label='Unidade de saúde',
    )
    cidadao = forms.ModelChoiceField(
        queryset=health.CidadaoSaudeMunicipal.objects.none(), label='Cidadão vinculado', required=False,
    )
    profissional = forms.ModelChoiceField(
        queryset=health.ProfissionalSaudeMunicipal.objects.none(), label='Profissional', required=False,
    )
    codigo_procedimento = forms.CharField(
        max_length=40, label='Código de referência local', required=False,
    )
    quantidade = forms.IntegerField(min_value=1, max_value=100000, label='Quantidade')

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['request_id'].initial = uuid.uuid4()
            self.fields['competencia'].initial = timezone.localdate().strftime('%Y-%m')
        for field in self.fields.values():
            if getattr(field.widget, 'input_type', None) != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        organization = current_organization()
        if organization and actor:
            self.fields['unidade_saude'].queryset = scoped_queryset(
                actor, health.UnidadeSaudeMunicipal.objects.filter(organization=organization, ativo=True),
            ).select_related('unidade').order_by('unidade__nome')
            self.fields['cidadao'].queryset = scoped_queryset(
                actor, health.CidadaoSaudeMunicipal.objects.filter(organization=organization, situacao='ATIVO'),
            ).order_by('nome')
            self.fields['profissional'].queryset = scoped_queryset(
                actor, health.ProfissionalSaudeMunicipal.objects.filter(
                    organization=organization, ativo=True, unidade_saude__ativo=True,
                ),
            ).select_related('servidor').order_by('servidor__nome')

    def clean(self):
        cleaned = super().clean()
        professional, unit = cleaned.get('profissional'), cleaned.get('unidade_saude')
        if professional and unit and professional.unidade_saude_id != unit.pk:
            self.add_error('profissional', 'O profissional não está vinculado à unidade selecionada.')
        return cleaned


class HealthRegulationRequestForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    cidadao = forms.ModelChoiceField(queryset=health.CidadaoSaudeMunicipal.objects.none(), label='Cidadão/paciente')
    unidade_origem = forms.ModelChoiceField(queryset=health.UnidadeSaudeMunicipal.objects.none(), label='Unidade solicitante')
    unidade_destino = forms.ModelChoiceField(
        queryset=health.UnidadeSaudeMunicipal.objects.none(), label='Unidade de destino preferencial', required=False,
    )
    procedimento = forms.CharField(max_length=160, label='Procedimento/especialidade solicitada')
    prioridade = forms.ChoiceField(
        choices=health.SolicitacaoRegulacaoSaude._meta.get_field('prioridade').choices,
        label='Prioridade inicial',
    )
    justificativa = forms.CharField(
        max_length=3000, label='Justificativa clínica',
        widget=forms.Textarea(attrs={'class': 'gp-input', 'rows': 4, 'placeholder': 'Será armazenada cifrada.'}),
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['request_id'].initial = uuid.uuid4()
        for field in self.fields.values():
            if getattr(field.widget, 'input_type', None) != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        organization = current_organization()
        if organization and actor:
            units = scoped_queryset(
                actor, health.UnidadeSaudeMunicipal.objects.filter(organization=organization, ativo=True),
            ).select_related('unidade').order_by('unidade__nome')
            self.fields['unidade_origem'].queryset = units
            self.fields['unidade_destino'].queryset = units
            if has_municipal_permission(actor, 'saude.patients.view', module=health.HEALTH_MODULE):
                self.fields['cidadao'].queryset = scoped_queryset(
                    actor, health.CidadaoSaudeMunicipal.objects.filter(organization=organization, situacao='ATIVO'),
                ).order_by('nome')


class HealthRegulationSlotForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    unidade_saude = forms.ModelChoiceField(queryset=health.UnidadeSaudeMunicipal.objects.none(), label='Unidade ofertante')
    procedimento = forms.CharField(max_length=160, label='Procedimento/especialidade')
    inicio = forms.DateTimeField(
        label='Data e hora', widget=forms.DateTimeInput(attrs={'class': 'gp-input', 'type': 'datetime-local'}),
    )
    capacidade = forms.IntegerField(min_value=1, max_value=500, label='Vagas disponíveis')

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['request_id'].initial = uuid.uuid4()
        for field in self.fields.values():
            if getattr(field.widget, 'input_type', None) != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        organization = current_organization()
        if organization and actor:
            self.fields['unidade_saude'].queryset = scoped_queryset(
                actor, health.UnidadeSaudeMunicipal.objects.filter(organization=organization, ativo=True),
            ).select_related('unidade').order_by('unidade__nome')

    def clean_inicio(self):
        value = self.cleaned_data['inicio']
        if value <= timezone.now():
            raise forms.ValidationError('A vaga deve ocorrer no futuro.')
        return value


class HealthRegulationScheduleForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    solicitacao = forms.ModelChoiceField(
        queryset=health.SolicitacaoRegulacaoSaude.objects.none(), label='Solicitação em fila',
    )
    vaga = forms.ModelChoiceField(queryset=health.VagaRegulacaoSaude.objects.none(), label='Vaga disponível')

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['request_id'].initial = uuid.uuid4()
        for field in self.fields.values():
            if getattr(field.widget, 'input_type', None) != 'hidden':
                field.widget.attrs['class'] = 'gp-input'
        organization = current_organization()
        if organization and actor:
            self.fields['solicitacao'].queryset = scoped_queryset(
                actor, health.SolicitacaoRegulacaoSaude.objects.filter(organization=organization, status='FILA'),
            ).order_by('-prioridade', 'solicitada_em')
            self.fields['vaga'].queryset = scoped_queryset(
                actor, health.VagaRegulacaoSaude.objects.filter(
                    organization=organization, ativo=True, inicio__gt=timezone.now(),
                ),
            ).select_related('unidade_saude__unidade').order_by('inicio')


class HealthRegulationScheduleTransitionForm(forms.Form):
    situacao = forms.ChoiceField(choices=(('ATENDIDO', 'Registrar atendimento'), ('CANCELADO', 'Cancelar agendamento')))
    justificativa = forms.CharField(
        max_length=500, widget=forms.Textarea(attrs={'class': 'gp-input', 'rows': 2}),
        label='Justificativa',
    )


class HealthRegulationPriorityForm(forms.Form):
    prioridade = forms.ChoiceField(choices=health.SolicitacaoRegulacaoSaude._meta.get_field('prioridade').choices)
    justificativa = forms.CharField(max_length=500, widget=forms.TextInput(attrs={'class': 'gp-input'}))
