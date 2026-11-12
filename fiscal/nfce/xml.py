"""Restricted, explicit model-65 XML. Unsupported tax branches fail closed."""
import re
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlsplit, parse_qs, urlencode
from lxml import etree
from django.core.exceptions import ValidationError
from .schema import NFE, parse

CENT = Decimal('.01')
NS = '{'+NFE+'}'
HOM = 'NOTA FISCAL EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL'
DEST_HOM = 'NF-E EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL'


def decimal(value, places=2):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number.as_tuple().exponent < -places:
            raise ValueError()
        return number
    except (ArithmeticError, ValueError):
        raise ValidationError('Valor fiscal inválido; use Decimal não negativo com precisão explícita.')


def money(value):
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), '.2f')


def digito(base):
    if not re.fullmatch(r'\d{43}', base):
        raise ValidationError('Base da chave deve conter 43 dígitos.')
    remainder = sum(int(d)*weight for d, weight in zip(reversed(base), [2,3,4,5,6,7,8,9]*6)) % 11
    return str(0 if remainder in (0, 1) else 11-remainder)


def chave(cnpj, serie, numero, emissao, cnf):
    if not re.fullmatch(r'\d{14}', cnpj) or not str(serie).isdigit() or not 0 <= int(serie) <= 889 or not 1 <= int(numero) <= 999999999 or not re.fullmatch(r'\d{8}', cnf):
        raise ValidationError('CNPJ numérico, série 0–889, número 1–999999999 e cNF explícitos são obrigatórios.')
    base = f'28{emissao:%y%m}{cnpj}65{int(serie):03}{int(numero):09}1{cnf}'
    return base + digito(base)


def qr_online(key, base):
    u = urlsplit(base)
    if not re.fullmatch(r'28\d{18}65\d{22}', key) or key[-1] != digito(key[:43]):
        raise ValidationError('QR exige chave NFC-e de Sergipe íntegra.')
    if u.scheme != 'https' or u.username or u.password or not u.hostname or u.fragment or u.query:
        raise ValidationError('URL oficial de QR deve ser HTTPS sem credenciais/parâmetros.')
    return base + '?p=' + key + '|3|2'


def tag(parent, name, value):
    node = etree.SubElement(parent, NS+name)
    node.text = str(value)
    return node


def produto_validar(row, crt, date):
    label = 'Produto '+str(row.get('sku', '?'))+': '
    required = ['sku','descricao','ncm','cfop','origem','unidade','quantidade','valor_unitario','liquido','tributacao']
    missing = [key for key in required if row.get(key) in ('', None)]
    tax = row.get('tributacao', {})
    for key in ['icms_codigo','pis_codigo','cofins_codigo','base_tributaria','rtc_aplicavel','rtc_fundamento','gtin_ausente','cest_aplicavel']:
        if key not in tax or tax[key] in ('', None): missing.append(key)
    if not row.get('gtin') and tax.get('gtin_ausente') is not True: missing.append('GTIN/EAN ou declaração explícita de ausência')
    if tax.get('cest_aplicavel') is True and not re.fullmatch(r'\d{7}', row.get('cest', '')): missing.append('CEST')
    if missing: raise ValidationError(label+'faltam '+', '.join(missing))
    if not re.fullmatch(r'\d{8}', row['ncm']) or row['cfop'] not in ('5101','5102') or row['origem'] not in tuple('012345678') or not re.fullmatch(r'[A-Za-z0-9]{1,6}', row['unidade']):
        raise ValidationError(label+'NCM/CFOP/origem/unidade inválidos ou operação não suportada nesta fase.')
    if row.get('gtin') and not re.fullmatch(r'\d{8}|\d{12}|\d{13}|\d{14}', row['gtin']):
        raise ValidationError(label+'GTIN inválido.')
    if tax['base_tributaria'] != 'LIQUIDO' or tax['icms_codigo'] not in ('102','00'):
        raise ValidationError(label+'regra de ICMS/base ainda não suportada; não será presumida.')
    if tax['icms_codigo'] == '102' and crt not in ('1','4') or tax['icms_codigo'] == '00' and crt not in ('2','3'):
        raise ValidationError(label+'CST/CSOSN incompatível com CRT explícito.')
    if tax['icms_codigo'] == '00' and ('icms_aliquota' not in tax or tax.get('icms_modalidade_bc') != '3'):
        raise ValidationError(label+'faltam ICMS alíquota/modalidade de base suportada (3).')
    for kind in ('pis','cofins'):
        if tax[kind+'_codigo'] not in ('01','04','06','07','08','09'):
            raise ValidationError(label+kind.upper()+' CST não suportado.')
        if tax[kind+'_codigo'] == '01' and kind+'_aliquota' not in tax:
            raise ValidationError(label+'falta '+kind.upper()+' alíquota.')
    if tax['rtc_aplicavel'] is True:
        rtc_cfg = tax.get('rtc')
        required_rtc = {'cst', 'c_class_trib', 'aliquota_cbs', 'aliquota_ibs_uf', 'aliquota_ibs_mun'}
        if not isinstance(rtc_cfg, dict) or not required_rtc.issubset(rtc_cfg.keys()):
            raise ValidationError(label+'IBS/CBS/IS aplicáveis: informe configuração explícita de CBS/IBS em rtc.')
    elif tax['rtc_aplicavel'] is not False:
        raise ValidationError(label+'IBS/CBS/IS aplicáveis: leiaute/regra RTC ainda pendente; emissão bloqueada.')
    elif crt not in ('1','4') or date.year >= 2027:
        raise ValidationError(label+'dispensa RTC não habilitada para este CRT/data; revisão normativa necessária.')
    for field in ['quantidade','valor_unitario','liquido']:
        decimal(row[field], 3 if field == 'quantidade' else 2)
    if decimal(row['quantidade'],3) <= 0: raise ValidationError(label+'quantidade deve ser positiva.')


def gerar(snapshot, numero, emissao, cnf):
    emit = snapshot['emitente']; items = snapshot['itens']; total = decimal(snapshot['total'])
    if not items or snapshot.get('ambiente') != 'HOMOLOGACAO' or emit['uf'] != 'SE' or not emit['municipio_ibge'].startswith('28'):
        raise ValidationError('Somente NFC-e presencial, interna, normal em Sergipe/homologação.')
    for item in items: produto_validar(item, emit['crt'], emissao.date())
    if sum((decimal(row['liquido']) for row in items), Decimal(0)) != total:
        raise ValidationError('Total dos itens não coincide com a venda confirmada.')
    key = chave(emit['cnpj'], snapshot['serie'], numero, emissao, cnf)
    root = etree.Element(NS+'NFe', nsmap={None:NFE})
    inf = etree.SubElement(root, NS+'infNFe', Id='NFe'+key, versao='4.00')
    ide = etree.SubElement(inf, NS+'ide')
    for k,v in [('cUF','28'),('cNF',cnf),('natOp',snapshot['natureza']),('mod','65'),('serie',int(snapshot['serie'])),('nNF',numero),('dhEmi',emissao.isoformat(timespec='seconds')),('tpNF','1'),('idDest','1'),('cMunFG',emit['municipio_ibge']),('tpImp','4'),('tpEmis','1'),('cDV',key[-1]),('tpAmb','2'),('finNFe','1'),('indFinal','1'),('indPres','1'),('procEmi','0'),('verProc','VERTYX-NFCE-HOM-2')]: tag(ide,k,v)
    issuer = etree.SubElement(inf, NS+'emit')
    tag(issuer,'CNPJ',emit['cnpj']);tag(issuer,'xNome',emit['razao_social'])
    address = etree.SubElement(issuer,NS+'enderEmit')
    for key_, field in [('xLgr','logradouro'),('nro','numero'),('xCpl','complemento'),('xBairro','bairro')]:
        if emit['endereco'].get(field): tag(address,key_,emit['endereco'][field])
    for k,v in [('cMun',emit['municipio_ibge']),('xMun',emit['municipio_nome']),('UF','SE'),('CEP',emit['endereco']['cep']),('cPais','1058'),('xPais','BRASIL')]:tag(address,k,v)
    tag(issuer,'IE',emit['ie']);tag(issuer,'CRT',emit['crt'])
    if snapshot.get('destinatario'):
        dest=etree.SubElement(inf,NS+'dest'); customer=snapshot['destinatario']
        tag(dest,'CPF' if len(customer['documento'])==11 else 'CNPJ',customer['documento']);tag(dest,'xNome',DEST_HOM);tag(dest,'indIEDest','9')
    sums={name:Decimal(0) for name in ('vProd','vDesc','vOutro','vBC','vICMS','vPIS','vCOFINS')}
    for index,row in enumerate(items,1):
        det=etree.SubElement(inf,NS+'det',nItem=str(index));prod=etree.SubElement(det,NS+'prod')
        gross=(decimal(row['quantidade'],3)*decimal(row['valor_unitario'])).quantize(CENT,rounding=ROUND_HALF_UP)
        net=decimal(row['liquido']);discount=max(gross-net,Decimal(0));addition=max(net-gross,Decimal(0))
        for k,v in [('cProd',row['sku']),('cEAN',row.get('gtin') or 'SEM GTIN'),('xProd',HOM if index==1 else row['descricao']),('NCM',row['ncm'])]:tag(prod,k,v)
        if row['tributacao']['cest_aplicavel']:tag(prod,'CEST',row['cest'])
        for k,v in [('CFOP',row['cfop']),('uCom',row['unidade']),('qCom',format(decimal(row['quantidade'],3),'.4f')),('vUnCom',format(decimal(row['valor_unitario']),'.10f')),('vProd',money(gross)),('cEANTrib',row.get('gtin') or 'SEM GTIN'),('uTrib',row['unidade']),('qTrib',format(decimal(row['quantidade'],3),'.4f')),('vUnTrib',format(decimal(row['valor_unitario']),'.10f'))]:tag(prod,k,v)
        if discount:tag(prod,'vDesc',money(discount))
        if addition:tag(prod,'vOutro',money(addition))
        tag(prod,'indTot','1')
        sums['vProd']+=gross;sums['vDesc']+=discount;sums['vOutro']+=addition
        tax=row['tributacao'];imposto=etree.SubElement(det,NS+'imposto');icms=etree.SubElement(imposto,NS+'ICMS')
        group=etree.SubElement(icms,NS+('ICMSSN102' if tax['icms_codigo']=='102' else 'ICMS00'))
        tag(group,'orig',row['origem']);tag(group,'CSOSN' if tax['icms_codigo']=='102' else 'CST',tax['icms_codigo'])
        if tax['icms_codigo']=='00':
            value=(net*decimal(tax['icms_aliquota'],4)/100).quantize(CENT,rounding=ROUND_HALF_UP)
            for k,v in [('modBC','3'),('vBC',money(net)),('pICMS',format(decimal(tax['icms_aliquota'],4),'.4f')),('vICMS',money(value))]:tag(group,k,v)
            sums['vBC']+=net;sums['vICMS']+=value
        for kind,title in [('pis','PIS'),('cofins','COFINS')]:
            code=tax[kind+'_codigo'];parent=etree.SubElement(imposto,NS+title);group=etree.SubElement(parent,NS+title+('Aliq' if code=='01' else 'NT'));tag(group,'CST',code)
            if code=='01':
                rate=decimal(tax[kind+'_aliquota'],4);value=(net*rate/100).quantize(CENT,rounding=ROUND_HALF_UP)
                for k,v in [('vBC',money(net)),('p'+title,format(rate,'.4f')),('v'+title,money(value))]:tag(group,k,v)
                sums['v'+title]+=value
    totals=etree.SubElement(inf,NS+'total');icmstot=etree.SubElement(totals,NS+'ICMSTot')
    for name in ['vBC','vICMS','vICMSDeson','vFCP','vBCST','vST','vFCPST','vFCPSTRet','vProd','vFrete','vSeg','vDesc','vII','vIPI','vIPIDevol','vPIS','vCOFINS','vOutro','vNF']:
        tag(icmstot,name,money(total if name=='vNF' else sums.get(name,Decimal(0))))
    transport=etree.SubElement(inf,NS+'transp');tag(transport,'modFrete','9')
    pag=etree.SubElement(inf,NS+'pag');received=Decimal(0);change=Decimal(0)
    for row in snapshot['pagamentos']:
        amount=decimal(row['valor']);recv=decimal(row['recebido']);troco=decimal(row['troco']);code=row['codigo']
        if code not in ('01','03','04','17','20') or recv-troco!=amount or code!='01' and troco:
            raise ValidationError('Pagamento fiscal incompatível com o registro comercial.')
        received+=recv;change+=troco;detail=etree.SubElement(pag,NS+'detPag');tag(detail,'tPag',code);tag(detail,'vPag',money(recv))
        if code in ('03','04','17','20'):
            card=etree.SubElement(detail,NS+'card')
            if row.get('integracao') not in ('1','2'):raise ValidationError('Falta tipo de integração do pagamento eletrônico.')
            tag(card,'tpIntegra',row['integracao'])
            if row.get('cnpj_instituicao'):tag(card,'CNPJ',row['cnpj_instituicao'])
            if row.get('bandeira'):tag(card,'tBand',row['bandeira'])
            if row.get('integracao')=='1':raise ValidationError('Pagamento integrado exige dados por transação ainda indisponíveis (cAut/PSP).')
    if received-change != total:raise ValidationError('Pagamentos menos troco não correspondem ao total fiscal.')
    if change:tag(pag,'vTroco',money(change))
    info=etree.SubElement(inf,NS+'infAdic');tag(info,'infCpl',HOM+'; documento local de teste, sem emissão fiscal real.')
    supplement=etree.SubElement(root,NS+'infNFeSupl');qr=tag(supplement,'qrCode',qr_online(key,snapshot['qr_url']));qr.text=etree.CDATA(qr.text);tag(supplement,'urlChave',snapshot['consulta_url'])
    return etree.tostring(root,encoding='utf-8',xml_declaration=True),key


def cancelamento(key, cnpj, protocolo, justificativa, emissao, lote):
    if len(justificativa.strip()) not in range(15,256) or len(key)!=44 or key[-1]!=digito(key[:43]) or key[6:20]!=cnpj or not re.fullmatch(r'\d{15}|\d{17}',protocolo):
        raise ValidationError('Cancelamento exige chave, protocolo e justificativa de 15–255 caracteres válidos.')
    root=etree.Element(NS+'envEvento',versao='1.00',nsmap={None:NFE});tag(root,'idLote',lote)
    event=etree.SubElement(root,NS+'evento',versao='1.00');info=etree.SubElement(event,NS+'infEvento',Id='ID110111'+key+'01')
    for k,v in [('cOrgao','28'),('tpAmb','2'),('CNPJ',cnpj),('chNFe',key),('dhEvento',emissao.isoformat(timespec='seconds')),('tpEvento','110111'),('nSeqEvento','1'),('verEvento','1.00')]:tag(info,k,v)
    detail=etree.SubElement(info,NS+'detEvento',versao='1.00')
    for k,v in [('descEvento','Cancelamento'),('nProt',protocolo),('xJust',justificativa.strip())]:tag(detail,k,v)
    return etree.tostring(root,encoding='utf-8',xml_declaration=True)
