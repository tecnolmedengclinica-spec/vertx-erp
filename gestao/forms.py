import uuid
from decimal import Decimal
from django import forms
from django.utils import timezone
from platform_core.forms import TenantModelForm
from platform_core.context import current_organization
from clientes.models import Cliente, Unidade
from equipamentos.models import Equipamento
from contas.models import Tecnico
from ordens import services as os_services
from . import models as m
from .permissions import allowed


class Estilizado(TenantModelForm):
    required_css_class = 'required'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-check-input' if isinstance(field.widget, forms.CheckboxInput) else 'form-select' if isinstance(field.widget, forms.Select) else 'form-control')
            if field.required:
                field.widget.attrs.setdefault('required', 'required')
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault('rows', 3)
            if isinstance(field, forms.DateField):
                field.widget = forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d')


class PecaForm(Estilizado):
    class Meta:
        model = m.Peca
        fields = ['sku', 'descricao', 'descricao_curta', 'imagem', 'fabricante', 'marca', 'referencia', 'categoria', 'subcategoria', 'gtin', 'codigo_barras', 'unidade_medida', 'unidade_compra', 'fator_compra', 'unidade_venda', 'fator_venda', 'fracionavel', 'exige_lote', 'estoque_minimo', 'estoque_maximo', 'controla_validade', 'localizacao', 'custo_aquisicao', 'preco_venda', 'fornecedor', 'registro_anvisa', 'ncm', 'cest', 'origem_fiscal', 'cfop', 'cst', 'csosn', 'cst_csosn', 'observacoes', 'ativo']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['imagem'].queryset = m.Peca._meta.get_field('imagem').remote_field.model.objects.filter(tipo='IMAGEM',peca=self.instance) if self.instance.pk else m.Peca._meta.get_field('imagem').remote_field.model.objects.none()
        for key in ['fator_compra','fator_venda']:
            self.fields[key].required = False
        if not self.instance.pk and not self.is_bound:
            from operacao.models import Configuracao
            config = Configuracao.objects.first()
            if config:
                self.initial.update(estoque_minimo=config.estoque_minimo_padrao,estoque_maximo=config.estoque_ideal_padrao)

    def clean_fator_compra(self):
        return self.cleaned_data['fator_compra'] if self.cleaned_data.get('fator_compra') is not None else self.instance.fator_compra

    def clean_fator_venda(self):
        return self.cleaned_data['fator_venda'] if self.cleaned_data.get('fator_venda') is not None else self.instance.fator_venda


class FornecedorForm(Estilizado):
    class Meta:
        model = m.Fornecedor
        fields = ['nome', 'nome_fantasia', 'documento', 'contato', 'representante', 'telefone', 'email', 'endereco', 'produtos_categorias', 'observacoes', 'ativo']


class OrcamentoForm(Estilizado):
    class Meta:
        model = m.Orcamento
        fields = ['data', 'validade', 'cliente', 'unidade', 'equipamento', 'ordem', 'diagnostico', 'servicos', 'desconto', 'deslocamento', 'impostos', 'observacoes', 'condicoes_pagamento', 'prazo_execucao', 'gerar_recebivel']

    def clean_deslocamento(self):
        return self.cleaned_data.get('deslocamento') or m.ZERO

    def clean_impostos(self):
        return self.cleaned_data.get('impostos') or m.ZERO


class ItemOrcamentoForm(Estilizado):
    class Meta:
        model = m.ItemOrcamento
        fields = ['tipo', 'peca', 'descricao', 'quantidade', 'valor_unitario', 'desconto']


class ContratoForm(Estilizado):
    unidades = forms.ModelMultipleChoiceField(queryset=Unidade.objects.none(), required=False, help_text='Sem seleção: todas as unidades do cliente. Selecione somente unidades atendidas.')
    equipamentos = forms.ModelMultipleChoiceField(queryset=Equipamento.objects.none(), required=False)

    class Meta:
        model = m.Contrato
        fields = ['numero', 'cliente', 'unidades', 'equipamentos', 'data_inicial', 'data_final', 'valor', 'periodicidade_cobranca', 'tipo', 'quantidade_visitas', 'periodicidade_preventiva_meses', 'sla_horas', 'reajuste', 'cobertura_servicos', 'cobertura_pecas', 'observacoes', 'status']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial['unidades'] = list(self.instance.unidades.values_list('pk', flat=True))
            self.initial['equipamentos'] = list(self.instance.equipamentos.values_list('pk', flat=True))

    def clean(self):
        data = super().clean()
        customer = data.get('cliente')
        units, equipment = data.get('unidades', []), data.get('equipamentos', [])
        if customer:
            if any(unit.cliente_id != customer.pk for unit in units):
                self.add_error('unidades', 'Use somente unidades deste cliente.')
            if any(item.cliente_id != customer.pk for item in equipment):
                self.add_error('equipamentos', 'Use somente equipamentos deste cliente.')
            if units and any(item.unidade_id not in {unit.pk for unit in units} for item in equipment):
                self.add_error('equipamentos', 'Equipamento deve estar nas unidades selecionadas.')
        return data


class LancamentoForm(Estilizado):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and (self.instance.origem_raiz_id or self.instance.ordem_id or self.instance.orcamento_id or self.instance.contrato_id or self.instance.pagamentos.exists() or hasattr(self.instance, 'compra_origem') or hasattr(self.instance, 'venda_origem') or hasattr(self.instance,'devolucao_origem')):
            for name in list(self.fields):
                if name not in ['categoria', 'centro_custo', 'forma_pagamento', 'observacoes']:
                    self.fields.pop(name)

    class Meta:
        model = m.Lancamento
        fields = ['tipo', 'descricao', 'cliente', 'fornecedor', 'categoria', 'centro_custo', 'forma_pagamento', 'valor', 'data_emissao', 'vencimento', 'observacoes']


class CategoriaFinanceiraForm(Estilizado):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Preserve compatibility with existing category forms and records that
        # predate nature-based classification.
        self.fields['natureza'].required = False

    def clean_natureza(self):
        return self.cleaned_data.get('natureza') or getattr(self.instance, 'natureza', None) or 'AMBAS'

    class Meta:
        model = m.CategoriaFinanceira
        fields = ['nome', 'natureza', 'ativo']


class CentroCustoForm(Estilizado):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['tipo'].required = False

    def clean_tipo(self):
        return self.cleaned_data.get('tipo') or self.instance.tipo

    class Meta:
        model = m.CentroCusto
        fields = ['nome', 'tipo', 'ativo']


class FormaPagamentoForm(Estilizado):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from operacao.models import ContaBancaria
        organization = current_organization()
        self.fields['conta_pdv'].queryset = ContaBancaria.objects.filter(organization=organization, ativo=True) if organization else ContaBancaria.objects.none()

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get('tipo_pdv')
        account = cleaned.get('conta_pdv')
        active = cleaned.get('ativo')
        if active and kind in {'PIX', 'DEBITO', 'CREDITO', 'TRANSFERENCIA', 'OUTRO'} and not account:
            self.add_error('conta_pdv', 'Associe uma conta ativa da organização para registrar o recebimento no financeiro.')
        if account and not account.ativo:
            self.add_error('conta_pdv', 'Selecione uma conta ativa da organização.')
        return cleaned

    class Meta:
        model = m.FormaPagamento
        fields = ['nome', 'tipo_pdv', 'conta_pdv', 'ativo']


class MovimentoForm(forms.Form):
    peca = forms.ModelChoiceField(label='Peça', queryset=m.Peca.objects.none())
    tipo = forms.ChoiceField(choices=m.Movimentacao.Tipo.choices)
    quantidade = forms.DecimalField(max_digits=14, decimal_places=3, min_value=0, help_text='No ajuste, informe o novo saldo absoluto; nos demais tipos, a quantidade movimentada.')
    ordem = forms.ModelChoiceField(label='Ordem de Serviço', queryset=None, required=False)
    observacao = forms.CharField(label='Observação / justificativa', required=False, widget=forms.Textarea(attrs={'rows': 3}))
    request_id = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    origem = forms.ModelChoiceField(queryset=None, required=False, label='Depósito de origem')
    destino = forms.ModelChoiceField(queryset=None, required=False, label='Depósito de destino')
    lote = forms.ModelChoiceField(queryset=None,required=False,label='Lote específico',help_text='Deixe vazio para aplicar FEFO. Perda/descarte exige motivo e lote explícito; entradas com validade usam a tela Entrada por lote.')

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        from ordens.models import OrdemServico
        from operacao.models import Deposito, LoteEstoque
        self.fields['lote'].queryset = LoteEstoque.objects.filter(quantidade__gt=0).select_related('peca','deposito')
        self.fields['origem'].queryset = self.fields['destino'].queryset = Deposito.objects.filter(ativo=True)
        self.fields['peca'].queryset = m.Peca.objects.filter(ativo=True)
        self.fields['tipo'].choices = [(key,label) for key,label in self.fields['tipo'].choices if key!='INVENTARIO']
        self.fields['ordem'].queryset = (OrdemServico.objects.all() if allowed(user, 'estoque_escrever') else os_services.visiveis(user)).exclude(status__in=['FINALIZADA', 'APROVADA', 'FATURADA', 'CANCELADA'])
        from platform_core.access import effective_role
        if effective_role(user) == 'TECNICO' or not allowed(user, 'estoque_escrever'):
            self.fields['tipo'].choices = [('OS', 'Utilização em OS'), ('DEVOLUCAO', 'Devolução da OS')]
            self.fields['ordem'].queryset = os_services.visiveis(user).exclude(status__in=['FINALIZADA','APROVADA','FATURADA','CANCELADA'])
            self.fields['ordem'].required = True
            self.fields['ordem'].widget = forms.HiddenInput()
            self.fields['lote'].queryset = self.fields['lote'].queryset.none()
            self.fields['lote'].widget = forms.HiddenInput()
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')


class TecnicoForm(forms.Form):
    tecnico = forms.ModelChoiceField(label='Técnico responsável', queryset=Tecnico.objects.none(), widget=forms.Select(attrs={'class': 'form-select'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['tecnico'].queryset = Tecnico.objects.filter(ativo=True)


class OperacaoFinanceiraForm(forms.Form):
    data = forms.DateField(label='Data de pagamento/recebimento', required=False, initial=timezone.localdate, widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'))
    valor = forms.DecimalField(label='Valor pago/recebido', required=False, max_digits=14, decimal_places=2, min_value=Decimal('.01'))
    desconto = forms.DecimalField(required=False, initial=Decimal('0.00'), max_digits=14, decimal_places=2, min_value=0)
    juros = forms.DecimalField(required=False, initial=Decimal('0.00'), max_digits=14, decimal_places=2, min_value=0)
    multa = forms.DecimalField(required=False, initial=Decimal('0.00'), max_digits=14, decimal_places=2, min_value=0)
    conta = forms.ModelChoiceField(label='Conta de recebimento/pagamento', queryset=__import__('operacao.models', fromlist=['ContaBancaria']).ContaBancaria.objects.none(), required=False)
    observacao = forms.CharField(label='Observação / justificativa', required=False, widget=forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from operacao.models import ContaBancaria
        self.fields['conta'].queryset = ContaBancaria.objects.filter(ativo=True)


class FiltrosForm(forms.Form):
    inicio = forms.DateField(label='De', required=False, widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    fim = forms.DateField(label='Até', required=False, widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    cliente = forms.ModelChoiceField(queryset=Cliente.objects.none(), required=False)
    unidade = forms.ModelChoiceField(queryset=Unidade.objects.none(), required=False)
    tecnico = forms.ModelChoiceField(label='Técnico', queryset=Tecnico.objects.none(), required=False)
    equipamento = forms.ModelChoiceField(queryset=Equipamento.objects.none(), required=False)
    contrato = forms.ModelChoiceField(queryset=m.Contrato.objects.none(), required=False)
    tipo_atendimento = forms.ChoiceField(label='Tipo de atendimento', required=False, choices=[('', 'Todos')] + list(os_services.OrdemServico._meta.get_field('tipo_atendimento').choices))
    status = forms.ChoiceField(required=False)

    def __init__(self, *args, statuses=(), **kwargs):
        super().__init__(*args, **kwargs)
        for name in ['cliente', 'unidade', 'tecnico', 'equipamento', 'contrato']:
            self.fields[name].queryset = self.fields[name].queryset.model.objects.all()
        self.fields['status'].choices = [('', 'Todos')] + list(statuses)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')

    def clean(self):
        data = super().clean()
        if data.get('inicio') and data.get('fim') and data['fim'] < data['inicio']:
            raise forms.ValidationError('Período final deve ser posterior ou igual ao inicial.')
        if data.get('cliente') and data.get('unidade') and data['unidade'].cliente_id != data['cliente'].pk:
            self.add_error('unidade', 'Unidade não pertence ao cliente selecionado.')
        if data.get('cliente') and data.get('equipamento') and data['equipamento'].cliente_id != data['cliente'].pk:
            self.add_error('equipamento', 'Equipamento não pertence ao cliente selecionado.')
        return data


class FinanceiroFiltrosForm(FiltrosForm):
    periodo = forms.ChoiceField(label='Período', required=False, choices=[('', 'Personalizado'), ('HOJE', 'Hoje'), ('7D', '7 dias'), ('30D', '30 dias'), ('MES', 'Mês atual'), ('MES_ANTERIOR', 'Mês anterior')])
    origem = forms.ChoiceField(required=False, choices=[('', 'Todas as origens'), ('OS', 'OS'), ('CONTRATO', 'Contrato'), ('ORCAMENTO', 'Orçamento'), ('VENDA', 'Venda'), ('COMPRA', 'Compra'), ('DEVOLUCAO', 'Devolução de venda'), ('MANUAL', 'Manual')])
    conta = forms.ModelChoiceField(label='Conta bancária / caixa', queryset=m.Lancamento.objects.none(), required=False)
    controle = forms.ChoiceField(required=False, choices=[('', 'Todos os pagamentos'), ('BANCARIZADO', 'Com conta bancária / caixa'), ('NAO_BANCARIZADO', 'Não bancarizados')])
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from operacao.models import ContaBancaria
        for name in ['cliente', 'unidade', 'tecnico', 'equipamento', 'status', 'contrato', 'tipo_atendimento']:
            self.fields.pop(name)
        self.fields['conta'].queryset = ContaBancaria.objects.all()
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')
    def clean(self):
        data = super().clean()
        if data.get('conta') and data.get('controle') == 'NAO_BANCARIZADO':
            self.add_error('controle', 'Pagamentos sem banco não pertencem a uma conta bancária.')
        return data
