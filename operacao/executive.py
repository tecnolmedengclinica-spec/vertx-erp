"""Visão executiva somente com dados existentes e permissões do ERP."""
from decimal import Decimal
from urllib.parse import urlencode
from django import forms
from django.db.models import Sum
from django.utils import timezone
from dateutil.relativedelta import relativedelta
from gestao import models as g
from gestao.permissions import allowed
from . import models as m
from .reporting import financial_dashboard
from .stock import metrics

class PeriodForm(forms.Form):
    periodo=forms.ChoiceField(required=False,choices=[('HOJE','Hoje'),('SEMANA','Esta semana'),('MES','Este mês'),('PERSONALIZADO','Personalizado')], widget=forms.Select(attrs={'class':'form-select form-select-sm'}))
    inicio=forms.DateField(required=False,widget=forms.DateInput(attrs={'type':'date','class':'form-control form-control-sm'},format='%Y-%m-%d'))
    fim=forms.DateField(required=False,widget=forms.DateInput(attrs={'type':'date','class':'form-control form-control-sm'},format='%Y-%m-%d'))
    grafico=forms.ChoiceField(required=False,choices=[('7','7 dias'),('30','30 dias'),('3M','3 meses'),('6M','6 meses'),('12M','12 meses')], widget=forms.Select(attrs={'class':'form-select form-select-sm'}))
    def __init__(self, *args, analytics_level='COMPLETO', **kwargs):
        self.analytics_level = analytics_level or 'BASICO'
        super().__init__(*args, **kwargs)
        if self.analytics_level == 'BASICO':
            self.fields['periodo'].choices = [('HOJE','Hoje'),('SEMANA','Esta semana'),('MES','Este mês')]
            self.fields['inicio'].required = False
            self.fields['fim'].required = False
            self.fields['grafico'].choices = [('7','7 dias'),('30','30 dias')]
        elif self.analytics_level == 'AVANCADO':
            self.fields['grafico'].choices = [('7','7 dias'),('30','30 dias'),('3M','3 meses'),('6M','6 meses')]
    def clean(self):
        data=super().clean()
        if data.get('periodo')=='PERSONALIZADO' and (not data.get('inicio') or not data.get('fim')):
            raise forms.ValidationError('Informe início e fim do período personalizado.')
        max_days = 180 if self.analytics_level == 'AVANCADO' else 366
        if data.get('inicio') and data.get('fim') and (data['fim'] < data['inicio'] or (data['fim']-data['inicio']).days>max_days):
            raise forms.ValidationError(f'Período deve ter até {max_days} dias e fim posterior ao início.')
        return data

def dates(form):
    today=timezone.localdate()
    mode=form.cleaned_data.get('periodo') or 'MES'
    if mode=='HOJE':
        return today,today
    if mode=='SEMANA':
        return today-timezone.timedelta(days=today.weekday()),today
    if mode=='PERSONALIZADO':
        return form.cleaned_data['inicio'],form.cleaned_data['fim']
    return today.replace(day=1),today

def link_ledger(kind, **filters):
    return '/financeiro/lancamentos/?'+urlencode({'tipo':kind,**filters})

def charts(start,end,interval):
    # Mesmo conceito de realizado do financeiro: pagamentos + liquidações legadas
    # sem pagamentos; nenhuma duplicação de contas parcialmente recebidas.
    periods=[]
    monthly=interval.endswith('M')
    date=start.replace(day=1) if monthly else start
    while date<=end:
        until=min(end,date+relativedelta(months=1)-timezone.timedelta(days=1)) if monthly else date
        periods.append({'inicio':max(date,start),'fim':until,'label':date.strftime('%m/%Y' if monthly else '%d/%m'),'entrada':g.ZERO,'saida':g.ZERO,'faturamento':g.ZERO})
        date=date+relativedelta(months=1) if monthly else date+timezone.timedelta(days=1)
    payments=m.Pagamento.objects.filter(data__range=(start,end)).select_related('lancamento')
    legacy=g.Lancamento.objects.filter(status='LIQUIDADO',pagamentos__isnull=True,data_liquidacao__range=(start,end)).exclude(eventos__tipo='ABATIMENTO')
    revenues=g.Lancamento.objects.filter(tipo='RECEBER',data_emissao__range=(start,end)).exclude(status='CANCELADO')
    for row in periods:
        row['entrada']=sum((p.valor for p in payments if row['inicio']<=p.data<=row['fim'] and p.lancamento.tipo=='RECEBER'),g.ZERO)+sum((p.valor for p in legacy if row['inicio']<=p.data_liquidacao<=row['fim'] and p.tipo=='RECEBER'),g.ZERO)
        row['saida']=sum((p.valor for p in payments if row['inicio']<=p.data<=row['fim'] and p.lancamento.tipo=='PAGAR'),g.ZERO)+sum((p.valor for p in legacy if row['inicio']<=p.data_liquidacao<=row['fim'] and p.tipo=='PAGAR'),g.ZERO)
        row['faturamento']=sum((p.valor for p in revenues if row['inicio']<=p.data_emissao<=row['fim']),g.ZERO)
    maximum=max([max(row['entrada'],row['saida'],row['faturamento']) for row in periods],default=1) or 1
    cash_max=max([max(row['entrada'],row['saida']) for row in periods],default=1) or 1
    revenue_max=max([row['faturamento'] for row in periods],default=1) or 1
    for index,row in enumerate(periods):
        row['x']=round(30+index*580/max(len(periods)-1,1))
        row['y_entrada']=round(175-150*row['entrada']/cash_max)
        row['y_saida']=round(175-150*row['saida']/cash_max)
        row['revenue_height']=round(150*row['faturamento']/revenue_max)
        row['revenue_y']=175-row['revenue_height']
        for key in ['entrada','saida','faturamento']:
            row[key+'_width']=round(100*row[key]/maximum)
    return periods


def sales_chart(start, end, interval):
    """Net confirmed sales by sale date, with formal returns shown in their event period."""
    periods=[]
    monthly=interval.endswith('M')
    date=start.replace(day=1) if monthly else start
    while date<=end:
        until=min(end,date+relativedelta(months=1)-timezone.timedelta(days=1)) if monthly else date
        periods.append({'inicio':max(date,start),'fim':until,'label':date.strftime('%m/%Y' if monthly else '%d/%m'),'vendas':g.ZERO})
        date=date+relativedelta(months=1) if monthly else date+timezone.timedelta(days=1)
    if not periods:
        return periods
    confirmed=m.Venda.objects.filter(status__in=['CONFIRMADA','FINALIZADA','PARCIALMENTE_DEVOLVIDA','DEVOLVIDA']).filter(
        Q(finalizada_em__date__range=(start,end)) |
        Q(finalizada_em__isnull=True,status='CONFIRMADA',criado_em__date__range=(start,end))
    ).prefetch_related('devolucoes')
    returns=m.DevolucaoVenda.objects.filter(criado_em__date__range=(start,end))
    for sale in confirmed:
        # DateField grouping follows the active business timezone. Calling
        # datetime.date() directly on an aware UTC timestamp can move a sale
        # into tomorrow (for example, 21:00 in Sao Paulo is 00:00 UTC).
        sale_date=timezone.localtime(sale.finalizada_em).date() if sale.finalizada_em else timezone.localtime(sale.criado_em).date()
        value=Decimal(sale.snapshot['total']) if sale.snapshot else sale.total
        for row in periods:
            if row['inicio']<=sale_date<=row['fim']:
                row['vendas']+=value
                break
    for returned in returns:
        return_date=timezone.localtime(returned.criado_em).date()
        for row in periods:
            if row['inicio']<=return_date<=row['fim']:
                row['vendas']-=returned.valor
                break
    maximum=max((max(row['vendas'],g.ZERO) for row in periods),default=g.ZERO) or Decimal('1')
    for index,row in enumerate(periods):
        row['x']=round(30+index*580/max(len(periods)-1,1))
        row['height']=round(150*max(row['vendas'],g.ZERO)/maximum)
        row['y']=175-row['height']
    return periods

def context(request, form, orders):
    start,end=dates(form)
    today=timezone.localdate()
    now=timezone.localtime()
    from platform_core.modules import analytics_level, analytics_at_least, module_enabled
    level=analytics_level(erp_code='assistencia')
    ctx={'executive':True,'analytics_level':level,'advanced_analytics':analytics_at_least('AVANCADO', erp_code='assistencia'),
         'complete_analytics':analytics_at_least('COMPLETO', erp_code='assistencia'),'period_form':form,'period_start':start,
         'period_end':end,'greeting':'Bom dia' if now.hour<12 else 'Boa tarde' if now.hour<18 else 'Boa noite'}
    ctx['agenda_hoje']=orders.filter(agendada_para__date=today).select_related('cliente','unidade','equipamento').order_by('agendada_para')[:12]
    completed=orders.filter(status__in=['FINALIZADA','APROVADA','FATURADA'],conclusao__date__range=(start,end))
    ctx['assistance_completed']={'preventivas':completed.filter(tipo_atendimento='PREVENTIVA').count(),'corretivas':completed.filter(tipo_atendimento='CORRETIVA').count(),'total':completed.count()}
    ctx['executive_operations']=[('OS abertas',orders.filter(status__in=['ABERTA','AGENDADA']).count(),'/ordens/?grupo=abertas'),('OS em andamento',orders.filter(status__in=['EXECUCAO','AG_PECA','AG_CLIENTE']).count(),'/ordens/?grupo=andamento'),('OS atrasadas',orders.filter(prazo__lt=timezone.now()).exclude(status__in=m.ENCERRADAS).count(),'/ordens/?atrasadas=1'),('Preventivas pendentes',m.PreventivaProgramada.objects.filter(cancelada=False,plano__ativo=True).exclude(ordem__status__in=m.ENCERRADAS).count(),'/preventivas/'),('Orçamentos aguardando aprovação',g.Orcamento.objects.filter(status='ENVIADO',validade__gte=today).count(),'/orcamentos/?status=ENVIADO')]
    stock=[metrics(part) for part in g.Peca.objects.filter(ativo=True).select_related('fornecedor','categoria')]
    ctx['stock_summary']={'baixo':sum(row['disponivel']>0 and row['disponivel']<=row['minimo'] for row in stock),'zerados':sum(row['disponivel']==0 for row in stock),'vencendo':m.LoteEstoque.objects.filter(quantidade__gt=0,validade__range=(today,today+timezone.timedelta(days=90))).count(),'vencidos':m.LoteEstoque.objects.filter(quantidade__gt=0,validade__lt=today).values('peca_id').distinct().count(),'repor':sum(row['disponivel']<=row['minimo'] for row in stock)}
    ctx['executive_operations'].append(('Estoque baixo',ctx['stock_summary']['repor'],'/estoque/ocorrencias/?tipo=REPOSICAO'))
    latest=m.RelatorioEstoque.objects.filter(automatico=True).first() or m.RelatorioEstoque.objects.first()
    ctx['weekly_report']=latest
    if latest:
        items=list(latest.itens.all())
        ctx['purchase_count']=sum(Decimal(row.dados['sugerido'])>0 for row in items)
        ctx['purchase_value']=sum((Decimal(row.dados['valor_estimado']) for row in items),g.ZERO)
    contracts=g.Contrato.objects.exclude(status__in=['SUSPENSO','ENCERRADO']).filter(data_inicial__lte=today,data_final__gte=today)
    ctx['contract_count']=contracts.count()
    frequencies={'MENSAL':1,'TRIMESTRAL':3,'SEMESTRAL':6,'ANUAL':12}
    ctx['contract_monthly']=sum((row.valor/frequencies[row.periodicidade_cobranca] for row in contracts if row.periodicidade_cobranca in frequencies),g.ZERO)
    ctx['contract_expiring']=contracts.filter(data_final__lte=today+timezone.timedelta(days=30)).count()
    ctx['contract_adjustments_unset']=True  # Regra textual não é data de reajuste.
    quotes=g.Orcamento.objects.filter(data__range=(start,end))
    open_quotes=quotes.filter(status__in=['RASCUNHO','ENVIADO'])
    approved=quotes.filter(status__in=['APROVADO','CONVERTIDO'])
    confirmed=list(m.Venda.objects.filter(status__in=['CONFIRMADA','FINALIZADA','PARCIALMENTE_DEVOLVIDA','DEVOLVIDA'],criado_em__date__range=(start,end)).prefetch_related('itens','devolucoes'))
    sales_value=sum(((Decimal(sale.snapshot['total']) if sale.snapshot else sale.total)-sum((row.valor for row in sale.devolucoes.all()),g.ZERO) for sale in confirmed),g.ZERO)
    ctx['commercial']={'abertos':open_quotes.count(),'aprovados':approved.count(),'vendas':len(confirmed),'valor':sales_value,'ticket':sales_value/len(confirmed) if confirmed else g.ZERO,'conversao':round(100*approved.count()/quotes.count(),1) if quotes.exists() else 0,'base':quotes.count()}
    interval=form.cleaned_data.get('grafico') or '30'
    chart_start=today.replace(day=1)-relativedelta(months=int(interval[:-1])-1) if interval.endswith('M') else today-timezone.timedelta(days=int(interval)-1)
    if module_enabled('pdv'):
        ctx['sales_chart']=sales_chart(chart_start,today,interval)
        ctx['sales_chart_total']=sum((row['vendas'] for row in ctx['sales_chart']),g.ZERO)
        if ctx['advanced_analytics']:
            span=(today-chart_start).days+1
            previous_end=chart_start-timezone.timedelta(days=1)
            previous_start=previous_end-timezone.timedelta(days=span-1)
            previous=sales_chart(previous_start,previous_end,interval)
            ctx['sales_comparison_previous']=sum((row['vendas'] for row in previous),g.ZERO)
            prior=ctx['sales_comparison_previous']
            ctx['sales_comparison_percent']=round((ctx['sales_chart_total']-prior)*100/abs(prior),1) if prior else None
    stock_chart=[]
    ctx['stock_chart_enabled'] = module_enabled('estoque_basico')
    if ctx['stock_chart_enabled']:
        for part in g.Peca.objects.filter(ativo=True).select_related('fornecedor','categoria'):
            data=metrics(part)
            stock_chart.append({'label':part.sku or part.descricao[:16],'disponivel':data['disponivel']})
        stock_chart=sorted(stock_chart,key=lambda row:row['disponivel'],reverse=True)[:10]
        maximum=max((row['disponivel'] for row in stock_chart),default=Decimal('1')) or Decimal('1')
        for row in stock_chart:
            row['width']=round(100*row['disponivel']/maximum)
    ctx['stock_chart']=stock_chart
    if allowed(request.user,'financeiro'):
        ledger=g.Lancamento.objects.filter(status__in=['PENDENTE','VENCIDO'])
        due=lambda kind:sum((row.saldo for row in ledger.filter(tipo=kind,vencimento=today)),g.ZERO)
        month=financial_dashboard(today.replace(day=1),today)
        period=financial_dashboard(start,end)
        ctx['financial_cards']=[{'title':'A receber hoje','value':due('RECEBER'),'url':link_ledger('RECEBER',vencimento_inicio=today,vencimento_fim=today,status='PENDENTE')},{'title':'A pagar hoje','value':due('PAGAR'),'url':link_ledger('PAGAR',vencimento_inicio=today,vencimento_fim=today,status='PENDENTE')},{'title':'Recebimentos do mês','value':month['recebido'],'url':link_ledger('RECEBER',pagamento_inicio=today.replace(day=1),pagamento_fim=today)},{'title':'Pagamentos do mês','value':month['pago'],'url':link_ledger('PAGAR',pagamento_inicio=today.replace(day=1),pagamento_fim=today)},{'title':'Saldo realizado no período','value':period['resultado'],'url':'/financeiro/?'+urlencode({'inicio':start,'fim':end})},{'title':'Inadimplência','value':period['vencido_receber'],'url':link_ledger('RECEBER',status='VENCIDO')}]
        banks=[]
        for account in m.ContaBancaria.objects.filter(ativo=True):
            moves=account.movimentos.filter(data__range=(start,end))
            banks.append({'account':account,'balance':account.saldo,'incoming':moves.filter(tipo='ENTRADA').aggregate(value=Sum('valor'))['value'] or g.ZERO,'outgoing':moves.filter(tipo='SAIDA').aggregate(value=Sum('valor'))['value'] or g.ZERO})
        ctx['bank_rows']=banks
        ctx['bank_total']=sum((row['balance'] for row in banks),g.ZERO)
        interval=form.cleaned_data.get('grafico') or '30'
        chart_start=today.replace(day=1)-relativedelta(months=int(interval[:-1])-1) if interval.endswith('M') else today-timezone.timedelta(days=int(interval)-1)
        ctx['cash_chart']=charts(chart_start,today,interval)
        ctx['cash_in_points']=' '.join(f'{row["x"]},{row["y_entrada"]}' for row in ctx['cash_chart'])
        ctx['cash_out_points']=' '.join(f'{row["x"]},{row["y_saida"]}' for row in ctx['cash_chart'])
        ctx['revenue_chart']=charts(chart_start,today,interval)
        ctx['revenue_chart_total']=sum((row['faturamento'] for row in ctx['revenue_chart']),g.ZERO)
        if ctx['advanced_analytics']:
            span=(today-chart_start).days+1
            previous_end=chart_start-timezone.timedelta(days=1)
            previous_start=previous_end-timezone.timedelta(days=span-1)
            previous_revenue=charts(previous_start,previous_end,interval)
            ctx['revenue_comparison_previous']=sum((row['faturamento'] for row in previous_revenue),g.ZERO)
            prior=ctx['revenue_comparison_previous']
            ctx['revenue_comparison_percent']=round((ctx['revenue_chart_total']-prior)*100/abs(prior),1) if prior else None
        entries=g.Lancamento.objects.filter(tipo='RECEBER',data_emissao__range=(start,end)).exclude(status='CANCELADO').select_related('contrato','orcamento','origem_raiz__contrato','origem_raiz__orcamento','origem_raiz__ordem').prefetch_related('orcamento__itens','origem_raiz__orcamento__itens')
        totals={'Serviços':g.ZERO,'Produtos':g.ZERO,'Contratos':g.ZERO,'Locações':g.ZERO,'Sem classificação':g.ZERO}
        sale_accounts=set(m.Venda.objects.exclude(conta=None).values_list('conta_id',flat=True))
        for entry in entries:
            origin=entry.origem_raiz or entry
            category='Locações' if origin.contrato_id and 'loca' in origin.contrato.tipo.lower() else 'Contratos' if origin.contrato_id else 'Produtos' if origin.pk in sale_accounts else 'Serviços' if origin.ordem_id or origin.orcamento_id and not origin.orcamento.itens.filter(tipo='PECA').exists() else 'Sem classificação'
            totals[category]+=entry.valor
        ctx['revenue_categories']=totals.items()
    return ctx


"""Executive indicators from official transactions, net of formal returns."""
from decimal import Decimal
from django.db.models import Q
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date
from platform_core.modules import require_capability, module_enabled
from platform_core.models import FilialEmpresa
from gestao import models as g
from . import models as m, reporting

def indicators(user,start,end,branch=None,center=None):
    require_capability(user,'executivo')
    if start>end: raise ValidationError('Período inválido.')
    sales=m.Venda.objects.filter(status__in=['CONFIRMADA','FINALIZADA','PARCIALMENTE_DEVOLVIDA','DEVOLVIDA']).filter(Q(finalizada_em__date__range=(start,end))|Q(finalizada_em__isnull=True,status='CONFIRMADA',criado_em__date__range=(start,end))).select_related('vendedor','cliente').prefetch_related('itens')
    returns=m.DevolucaoVenda.objects.filter(criado_em__date__range=(start,end)).select_related('venda__vendedor').prefetch_related('itens__movimento')
    if branch:
        branch=FilialEmpresa.objects.get(pk=branch,ativo=True);sales=sales.filter(filial=branch);returns=returns.filter(venda__filial=branch)
    if center:
        center=g.CentroCusto.objects.get(pk=center,ativo=True);sales=sales.filter(centro_custo=center);returns=returns.filter(venda__centro_custo=center)
    gross=Decimal('0');cost=Decimal('0');by_seller={}
    if module_enabled('pdv'):
        for sale in sales:
            value=Decimal(sale.snapshot['total']) if sale.snapshot else sale.total
            actual=sum((item.custo_total if item.custo_total is not None else item.quantidade*item.custo_unitario for item in sale.itens.all()),Decimal('0'))
            gross+=value;cost+=actual
            label=str(sale.vendedor or 'Vendedor não informado');row=by_seller.setdefault(label,{'vendedor':label,'vendas':0,'valor':Decimal('0'),'custo':Decimal('0')})
            row['vendas']+=1;row['valor']+=value;row['custo']+=actual
        for returned in returns:
            gross-=returned.valor
            restored=sum((item.movimento.valor_custo for item in returned.itens.all()),Decimal('0'));cost-=restored
            label=str(returned.venda.vendedor or 'Vendedor não informado');row=by_seller.setdefault(label,{'vendedor':label,'vendas':0,'valor':Decimal('0'),'custo':Decimal('0')});row['valor']-=returned.valor;row['custo']-=restored
    financial=reporting.financial_dashboard(start,end,{'filial':branch,'centro_custo':center}) if allowed(user,'financeiro') else {}
    from ordens.models import OrdemServico
    orders=OrdemServico.objects.filter(abertura__date__range=(start,end))
    if branch or center: orders=orders.none()  # Legacy OS lack own-company allocation; never infer from customer unit.
    lots=m.LoteEstoque.objects.filter(quantidade__gt=0)
    if branch or center: lots=lots.none()  # Deposit allocations are explicit, not inferred from a client address.
    total_stock=lots.count() if module_enabled('estoque') and not branch and not center else None
    return {'vendas_liquidas':gross,'custo_liquido_produtos':cost,'margem_registrada':gross-cost,'vendedores':list(by_seller.values()),'financeiro':financial,'os_no_periodo':orders.count() if module_enabled('assistencia') and not branch and not center else None,'lotes_com_saldo':total_stock,'lotes_vencidos':lots.filter(validade__lt=timezone.localdate()).count() if module_enabled('estoque') and not branch and not center else None,'funcionarios_ativos':m.Funcionario.objects.filter(ativo=True).count() if module_enabled('rh') and not branch and not center else None}

@login_required
def dashboard(request):
    today=timezone.localdate()
    require_capability(request.user,'executivo')
    try:
        start=parse_date(request.GET.get('inicio','')) or today.replace(day=1);end=parse_date(request.GET.get('fim','')) or today
        data=indicators(request.user,start,end,int(request.GET['filial']) if request.GET.get('filial') else None,int(request.GET['centro_custo']) if request.GET.get('centro_custo') else None)
    except (ValidationError,ValueError,FilialEmpresa.DoesNotExist,g.CentroCusto.DoesNotExist): return HttpResponse('Filtros inválidos.',status=400)
    return render(request,'operacao/executive_commercial.html',{'data':data,'inicio':start,'fim':end,'filiais':FilialEmpresa.objects.filter(ativo=True),'centros':g.CentroCusto.objects.filter(ativo=True)})
