import uuid
from decimal import Decimal
from pathlib import PurePosixPath
from django import forms
from django.db.models import Q
from django.utils import timezone
from platform_core.context import current_organization
from gestao.forms import Estilizado
from gestao.models import CategoriaFinanceira, CentroCusto
from contas.models import Usuario
from equipamentos.models import Equipamento
from . import models as m

def model_form(model, fields):
    return type(model.__name__ + 'Form', (Estilizado,), {'__module__': __name__, 'Meta': type('Meta', (), {'model': model, 'fields': fields})})

DepositoForm = model_form(m.Deposito, ['nome', 'endereco', 'ativo'])
CategoriaPecaForm = model_form(m.CategoriaPeca, ['nome', 'ativo'])
ChamadoForm = model_form(m.Chamado, ['cliente', 'unidade', 'equipamento', 'contrato', 'titulo', 'descricao', 'prioridade', 'solicitante', 'telefone', 'email', 'origem', 'tipo', 'tecnico', 'agendada_para', 'agendada_ate', 'prazo', 'observacoes_internas'])
ChamadoBaseForm = ChamadoForm
class ChamadoForm(ChamadoBaseForm):
    request_id = forms.UUIDField(required=False, initial=uuid.uuid4, widget=forms.HiddenInput)
    anexo = forms.FileField(label='Foto / anexo', required=False)
    def clean(self):
        # Token replay is validated against the intent by the locked service.
        # It must not be rejected by the generic tenant uniqueness validator.
        token = self.cleaned_data.pop('request_id', None)
        data = super().clean()
        data['request_id'] = token
        return data
    def clean_anexo(self):
        upload = self.cleaned_data.get('anexo')
        if upload:
            if not upload.name.lower().endswith(('.pdf', '.jpg', '.jpeg', '.png', '.webp')):
                raise forms.ValidationError('Anexe PDF ou imagem JPG, PNG ou WebP.')
            validator = DocumentoForm()
            validator.cleaned_data = {'arquivo': upload}
            return validator.clean_arquivo()
        return None
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ['agendada_para', 'agendada_ate', 'prazo']:
            self.fields[name].widget = forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}, format='%Y-%m-%dT%H:%M')

CompraForm = model_form(m.Compra, ['fornecedor', 'deposito', 'necessidade', 'vencimento', 'previsao_recebimento', 'observacoes'])
ConhecimentoForm = model_form(m.ArtigoConhecimento, ['titulo', 'fabricante', 'modelo', 'equipamento', 'ordem', 'manual', 'sintoma', 'defeito', 'solucao', 'procedimento', 'observacoes', 'ativo'])
NecessidadePecaForm = model_form(m.NecessidadePecaOS, ['peca', 'deposito', 'quantidade', 'urgencia', 'observacoes'])
ItemCompraForm = model_form(m.ItemCompra, ['peca', 'quantidade', 'custo_unitario', 'lote', 'fabricacao', 'validade', 'documento_entrada'])
ItemCompraBaseForm=ItemCompraForm
class ItemCompraForm(ItemCompraBaseForm):
    unidade_entrada=forms.ChoiceField(choices=[('base','Unidade base do estoque'),('compra','Unidade de compra configurada')],required=False,initial='base')
    def clean(self):
        data=super().clean()
        if data.get('peca') and data.get('quantidade') is not None and data.get('custo_unitario') is not None:
            from gestao.products import convert
            result=convert(data['peca'],data['quantidade'],data.get('unidade_entrada') or 'base',data['custo_unitario'])
            data['quantidade']=result['quantidade_base'];data['custo_unitario']=result['valor_base']
            self.instance.conversao={key:str(value) for key,value in result.items()}
        return data
CotacaoCompraForm = model_form(m.CotacaoCompra, ['fornecedor', 'valor', 'validade', 'prazo', 'observacoes'])
ItemVendaForm = model_form(m.ItemVenda, ['peca', 'quantidade', 'valor_unitario', 'desconto'])
ContaBancariaForm = model_form(m.ContaBancaria, ['nome', 'banco', 'agencia', 'numero', 'titular', 'tipo', 'deposito_padrao', 'saldo_inicial', 'permite_saldo_negativo', 'ativo'])
class ContaBancariaForm(ContaBancariaForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        depot_options = m.Deposito.objects.filter(ativo=True)
        if self.instance.pk and self.instance.deposito_padrao_id:
            depot_options = m.Deposito.objects.filter(Q(ativo=True) | Q(pk=self.instance.deposito_padrao_id))
        self.fields['deposito_padrao'].queryset = depot_options
        self.fields['deposito_padrao'].required = False
        self.fields['deposito_padrao'].label = 'Depósito padrão do caixa'
        self.fields['deposito_padrao'].help_text = 'Obrigatório para contas do tipo Caixa. Vendas e baixas de estoque usarão este depósito automaticamente.'
        # Keep the existing field ordering intuitive in the shared finance form.
        self.order_fields(['nome', 'tipo', 'deposito_padrao', 'banco', 'agencia', 'numero', 'titular', 'saldo_inicial', 'permite_saldo_negativo', 'ativo'])

    def clean(self):
        data = super().clean()
        if data.get('tipo') == 'CAIXA' and not data.get('deposito_padrao'):
            self.add_error('deposito_padrao', 'Selecione o depósito padrão para este caixa.')
        elif data.get('tipo') == 'CAIXA' and not data['deposito_padrao'].ativo:
            self.add_error('deposito_padrao', 'O depósito padrão do caixa precisa estar ativo.')
        return data
ExtratoForm = model_form(m.ExtratoBancario, ['conta', 'identificador', 'data', 'valor', 'descricao'])
FiscalForm = model_form(m.DocumentoFiscal, ['tipo', 'cliente', 'lancamento', 'ordem', 'venda', 'serie', 'ambiente', 'municipio', 'codigo_servico', 'impostos', 'retencoes', 'descricao', 'valor'])

class RecebimentoCompraForm(forms.Form):
    request_id = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    deposito = forms.ModelChoiceField(queryset=m.Deposito.objects.none())
    documento = forms.CharField(label='Documento/NF de referência', max_length=180, required=False)
    def __init__(self, *args, compra, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['deposito'].queryset = m.Deposito.objects.filter(ativo=True)
        self.initial['deposito'] = compra.deposito_id
        self.items = list(compra.itens.select_related('peca', 'compra'))
        for item in self.items:
            self.fields[f'quantidade_{item.pk}'] = forms.DecimalField(label=f'{item.peca} — receber (pendente: {item.quantidade_pendente})', max_digits=14, decimal_places=3, min_value=0, max_value=item.quantidade_pendente, initial=0, required=False)
            self.fields[f'lote_{item.pk}'] = forms.CharField(label=f'Lote — {item.peca.sku}', max_length=100, required=False, initial=item.lote)
            self.fields[f'fabricacao_{item.pk}'] = forms.DateField(label=f'Fabricação — {item.peca.sku}', required=False, initial=item.fabricacao, widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
            self.fields[f'validade_{item.pk}'] = forms.DateField(label=f'Validade — {item.peca.sku}', required=False, initial=item.validade, widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')
    def clean(self):
        data = super().clean()
        lines = []
        for item in self.items:
            quantity = data.get(f'quantidade_{item.pk}')
            if quantity:
                lot, validity = data.get(f'lote_{item.pk}'), data.get(f'validade_{item.pk}')
                if item.peca.controla_validade and (not lot or not validity):
                    self.add_error(f'lote_{item.pk}', 'Produto exige lote e validade.')
                lines.append({'item_id': item.pk, 'quantidade': quantity, 'lote': lot or '', 'fabricacao': data.get(f'fabricacao_{item.pk}'), 'validade': validity})
        if not lines:
            raise forms.ValidationError('Informe quantidade para pelo menos um item.')
        data['linhas'] = lines
        return data

class VendaForm(Estilizado):
    class Meta:
        model = m.Venda
        fields = ['cliente', 'vendedor', 'deposito', 'vencimento', 'observacoes']
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['vendedor'].queryset = Usuario.objects.filter(memberships__organization=current_organization(), memberships__status='ATIVO', is_active=True).distinct()

class ChecklistForm(Estilizado):
    itens = forms.CharField(label='Itens do checklist', widget=forms.Textarea(attrs={'rows': 8, 'data-checklist-builder': 'true'}), help_text='Adicione perguntas e escolha o tipo de resposta. Sem o editor, use um item por linha (* obrigatório).')
    class Meta:
        model = m.ModeloChecklist
        fields = ['nome', 'ativo', 'categoria', 'tipo_equipamento', 'fabricante', 'modelo', 'tipo_servico', 'itens']
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial['itens'] = '\n'.join(('* ' if row.get('obrigatorio') else '') + row['item'] for row in self.instance.itens)
        if any(row.get('tipo') for row in self.instance.itens):
            import json
            self.initial['itens'] = json.dumps(self.instance.itens, ensure_ascii=False, indent=2)
    def clean_itens(self):
        rows = [{'item': line.strip().lstrip('*').strip(), 'obrigatorio': line.strip().startswith('*')} for line in self.cleaned_data['itens'].splitlines() if line.strip()]
        if self.cleaned_data['itens'].lstrip().startswith('['):
            import json
            try:
                rows = json.loads(self.cleaned_data['itens'])
            except (ValueError, TypeError):
                raise forms.ValidationError('Lista JSON inválida.')
        m.checklist_valido(rows)
        for row in rows:
            row.pop('resposta', None)
            row['concluido'] = False
        return rows

class PlanoForm(Estilizado):
    equipamentos = forms.ModelMultipleChoiceField(queryset=Equipamento.objects.none())
    class Meta:
        model = m.PlanoPreventivo
        fields = ['nome', 'cliente', 'unidade', 'contrato', 'equipamentos', 'checklist', 'tecnico', 'equipe', 'tipo_manutencao', 'periodicidade', 'intervalo_dias', 'inicio', 'fim', 'observacoes', 'ativo']
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial['equipamentos'] = self.instance.equipamentos.all()
            self.initial['equipe'] = self.instance.equipe.all()
    def clean(self):
        data = super().clean()
        if data.get('cliente') and any(eq.cliente_id != data['cliente'].pk for eq in data.get('equipamentos', [])):
            self.add_error('equipamentos', 'Use equipamentos do cliente selecionado.')
        if data.get('contrato') and any(not data['contrato'].equipamentos.filter(pk=eq.pk).exists() for eq in data.get('equipamentos', [])):
            self.add_error('equipamentos', 'Use equipamentos cobertos pelo contrato.')
        return data

class DocumentoForm(Estilizado):
    class Meta:
        model = m.Documento
        fields = ['titulo', 'tipo', 'categoria', 'arquivo', 'cliente', 'unidade', 'equipamento', 'tecnico', 'fornecedor', 'chamado', 'ordem', 'contrato', 'orcamento', 'fiscal', 'peca', 'funcionario', 'compra', 'venda', 'emissao', 'validade', 'observacoes', 'disponivel_portal']
    def clean_arquivo(self):
        upload = self.cleaned_data['arquivo']
        if upload.size > 25 * 1024 * 1024:
            raise forms.ValidationError('Arquivo limitado a 25 MB.')
        suffix = PurePosixPath(upload.name).suffix.lower()
        head = upload.read(4096)
        upload.seek(0)
        if suffix not in ['.pdf', '.jpg', '.jpeg', '.png', '.webp', '.xml', '.mp4', '.webm']:
            raise forms.ValidationError('Use PDF, imagem, XML, MP4 ou WebM.')
        if suffix == '.pdf' and not head.startswith(b'%PDF-'):
            raise forms.ValidationError('PDF inválido.')
        if suffix in ['.jpg', '.jpeg', '.png', '.webp']:
            from PIL import Image
            try:
                with Image.open(upload) as img:
                    if img.width * img.height > 25000000:
                        raise ValueError()
                    img.verify()
            except (OSError, ValueError, Image.DecompressionBombError):
                raise forms.ValidationError('Imagem inválida ou grande demais.')
            upload.seek(0)
        if suffix == '.xml':
            import xml.etree.ElementTree as xml
            content = upload.read()
            upload.seek(0)
            try:
                if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
                    raise ValueError()
                xml.fromstring(content)
            except (ValueError, xml.ParseError):
                raise forms.ValidationError('XML inválido ou com entidades externas.')
        if suffix == '.mp4' and head[4:8] != b'ftyp' or suffix == '.webm' and not head.startswith(b'\x1aE\xdf\xa3'):
            raise forms.ValidationError('Vídeo inválido.')
        return upload

class PortalForm(Estilizado):
    class Meta:
        model = m.PortalCliente
        fields = ['usuario', 'cliente', 'ativo']
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['usuario'].queryset = Usuario.objects.filter(memberships__organization=current_organization(), memberships__role='CLIENTE', memberships__status='ATIVO', is_active=True).distinct()

class PagamentoForm(forms.Form):
    valor = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal('.01'))
    desconto = forms.DecimalField(required=False, initial=Decimal('0.00'), max_digits=14, decimal_places=2, min_value=0)
    juros = forms.DecimalField(required=False, initial=Decimal('0.00'), max_digits=14, decimal_places=2, min_value=0)
    multa = forms.DecimalField(required=False, initial=Decimal('0.00'), max_digits=14, decimal_places=2, min_value=0)
    data = forms.DateField(initial=timezone.localdate, widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    conta = forms.ModelChoiceField(queryset=m.ContaBancaria.objects.none(), required=False, help_text='Sem conta: pagamento não bancarizado, registrado no financeiro e identificado no histórico/relatório; não altera saldo bancário.')
    observacao = forms.CharField(required=False, widget=forms.Textarea)
    request_id = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['conta'].queryset = m.ContaBancaria.objects.filter(ativo=True)

class CaixaForm(PagamentoForm):
    tipo = forms.ChoiceField(choices=[('ENTRADA', 'Entrada'), ('SAIDA', 'Saída')])
    descricao = forms.CharField(max_length=255)
    categoria = forms.ModelChoiceField(queryset=CategoriaFinanceira.objects.none(), required=False)
    centro_custo = forms.ModelChoiceField(queryset=CentroCusto.objects.none(), required=False)
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['categoria'].queryset = CategoriaFinanceira.objects.filter(ativo=True)
        self.fields['centro_custo'].queryset = CentroCusto.objects.filter(ativo=True)
        self.fields['conta'].required = True
        for name in ['desconto','juros','multa']:
            self.fields.pop(name)

class TransferenciaForm(PagamentoForm):
    destino = forms.ModelChoiceField(queryset=m.ContaBancaria.objects.none())
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['conta'].required = True
        self.fields['destino'].queryset = m.ContaBancaria.objects.filter(ativo=True)
        for name in ['desconto','juros','multa']:
            self.fields.pop(name)

class ParcelasForm(forms.Form):
    quantidade = forms.IntegerField(min_value=2, max_value=60)
    primeiro_vencimento = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))

class ConciliacaoForm(forms.Form):
    movimento = forms.ModelChoiceField(queryset=m.MovimentoCaixa.objects.none())
    def __init__(self, *args, conta, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['movimento'].queryset = m.MovimentoCaixa.objects.filter(conta=conta, extratobancario__isnull=True)

class ExtratoImportForm(forms.Form):
    conta = forms.ModelChoiceField(queryset=m.ContaBancaria.objects.none())
    arquivo = forms.FileField(help_text='OFX ou CSV, até 10 MB. O arquivo é processado localmente e não é armazenado.')
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['conta'].queryset = m.ContaBancaria.objects.filter(ativo=True)
    def clean_arquivo(self):
        upload = self.cleaned_data['arquivo']
        suffix = PurePosixPath(upload.name).suffix.lower()
        if upload.size > 10 * 1024 * 1024:
            raise forms.ValidationError('O arquivo excede 10 MB.')
        if suffix not in ['.ofx', '.csv']:
            raise forms.ValidationError('Envie um arquivo OFX ou CSV.')
        return upload

ConfiguracaoForm = model_form(m.Configuracao, ['endereco', 'prefixo_os', 'prefixo_orcamento', 'prefixo_contrato', 'digitos_numero', 'prazo_financeiro_dias', 'juros_mensal_percentual', 'multa_percentual', 'municipio_fiscal', 'codigo_servico', 'ambiente_fiscal', 'provedor_fiscal'])


class ConfiguracaoEmailForm(Estilizado):
    email_teste = forms.EmailField(
        label='E-mail de Destino para Teste',
        required=False,
        help_text='Digite um e-mail para validar a conexão SMTP clicando em Testar Envio.'
    )

    class Meta:
        model = m.ConfiguracaoNotificacaoEmail
        fields = [
            'servidor_smtp',
            'porta_smtp',
            'email_remetente',
            'nome_remetente',
            'usuario_smtp',
            'senha_smtp',
            'usar_tls',
            'usar_ssl',
            'ativo',
            'notificar_conclusao_os',
            'notificar_preventiva',
            'dias_antecedencia_preventiva',
            'notificar_cobranca',
        ]
        widgets = {
            'senha_smtp': forms.PasswordInput(render_value=True, attrs={'class': 'form-control', 'placeholder': '••••••••••••'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ['servidor_smtp', 'porta_smtp', 'email_remetente', 'nome_remetente']:
            self.fields[name].required = True
            self.fields[name].widget.attrs['required'] = 'required'
