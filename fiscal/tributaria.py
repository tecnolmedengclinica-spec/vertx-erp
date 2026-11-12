"""Motor de cálculo, serialização nota.json e nós XML da Reforma Tributária (CBS / IBS / IS).

Compatível com a NT 2025.002 / pacote XSD PL_010f_v1.04 (EC 132/2023 e LC 214/2025).
Não presume alíquota em homologação/produção sem parametrização explícita: presets
normativos devem ser selecionados ou informados explicitamente pelo responsável fiscal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from lxml import etree
from django.core.exceptions import ValidationError


CENT = Decimal('.01')
ZERO = Decimal('0.00')
RATE_PLACES = Decimal('.0001')

# Alíquotas de referência por fase da transição tributária (LC 214/2025)
REGIMES_TRANSICAO = {
    '2026_TESTE': {
        'label': '2026 · Ano-teste RTC (CBS 0,90% + IBS UF 0,10%)',
        'aliquota_cbs': Decimal('0.9000'),
        'aliquota_ibs_uf': Decimal('0.1000'),
        'aliquota_ibs_mun': Decimal('0.0000'),
        'cst_padrao': '000',
        'c_class_trib_padrao': '000001',
    },
    '2027_CBS': {
        'label': '2027–2028 · CBS plena / IBS teste (8,80% CBS + 0,10% IBS)',
        'aliquota_cbs': Decimal('8.8000'),
        'aliquota_ibs_uf': Decimal('0.0500'),
        'aliquota_ibs_mun': Decimal('0.0500'),
        'cst_padrao': '000',
        'c_class_trib_padrao': '000001',
    },
    '2029_2032_TRANSICAO': {
        'label': '2029–2032 · Transição gradual ICMS/ISS -> IBS',
        'aliquota_cbs': Decimal('8.8000'),
        'aliquota_ibs_uf': Decimal('5.2500'),
        'aliquota_ibs_mun': Decimal('3.4500'),
        'cst_padrao': '000',
        'c_class_trib_padrao': '000001',
    },
    'DEFINITIVO': {
        'label': 'Alíquota padrão estimada integral (CBS 8,80% + IBS 17,70%)',
        'aliquota_cbs': Decimal('8.8000'),
        'aliquota_ibs_uf': Decimal('11.8000'),
        'aliquota_ibs_mun': Decimal('5.9000'),
        'cst_padrao': '000',
        'c_class_trib_padrao': '000001',
    },
}


def _dec(value: Any, places: Decimal = CENT, *, name: str = 'valor') -> Decimal:
    try:
        num = Decimal(str(ZERO if value in (None, '') else value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError(f'Valor inválido para {name}.') from exc
    if not num.is_finite() or num < 0:
        raise ValidationError(f'{name} não pode ser negativo ou não finito.')
    return num.quantize(places, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), '.2f')


def _rate(value: Decimal) -> str:
    return format(value.quantize(RATE_PLACES, rounding=ROUND_HALF_UP), '.4f')


@dataclass(frozen=True)
class ParametrosReformaTributaria:
    cst: str = '000'
    c_class_trib: str = '000001'
    aliquota_cbs: Decimal = Decimal('0.9000')
    aliquota_ibs_uf: Decimal = Decimal('0.1000')
    aliquota_ibs_mun: Decimal = Decimal('0.0000')
    reducao_cbs_pct: Decimal = Decimal('0.0000')
    reducao_ibs_pct: Decimal = Decimal('0.0000')
    diferimento_cbs_pct: Decimal = Decimal('0.0000')
    diferimento_ibs_pct: Decimal = Decimal('0.0000')
    aliquota_is: Decimal = Decimal('0.0000')
    cst_is: str = ''
    c_class_trib_is: str = ''
    regime_transicao: str = '2026_TESTE'
    ind_doacao: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_preset(cls, regime_transicao: str = '2026_TESTE', **overrides: Any) -> 'ParametrosReformaTributaria':
        preset = REGIMES_TRANSICAO.get(regime_transicao)
        if preset is None:
            raise ValidationError(f'Regime de transição RTC desconhecido: {regime_transicao}.')
        data = {
            'cst': preset['cst_padrao'],
            'c_class_trib': preset['c_class_trib_padrao'],
            'aliquota_cbs': preset['aliquota_cbs'],
            'aliquota_ibs_uf': preset['aliquota_ibs_uf'],
            'aliquota_ibs_mun': preset['aliquota_ibs_mun'],
            'regime_transicao': regime_transicao,
        }
        data.update(overrides)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> 'ParametrosReformaTributaria':
        data = dict(raw or {})
        regime = str(data.get('regime_transicao') or '').strip()
        if regime in REGIMES_TRANSICAO and not any(
            k in data for k in ('aliquota_cbs', 'aliquota_ibs_uf', 'aliquota_ibs_mun')
        ):
            base = dict(REGIMES_TRANSICAO[regime])
            data = {
                'cst': base['cst_padrao'],
                'c_class_trib': base['c_class_trib_padrao'],
                'aliquota_cbs': base['aliquota_cbs'],
                'aliquota_ibs_uf': base['aliquota_ibs_uf'],
                'aliquota_ibs_mun': base['aliquota_ibs_mun'],
                **data,
            }
        cst = str(data.get('cst') or '000').strip()
        c_class = str(data.get('c_class_trib') or '000001').strip()
        if not cst.isdigit() or len(cst) != 3:
            raise ValidationError('CST IBS/CBS deve possuir 3 dígitos.')
        if not c_class.isdigit() or len(c_class) != 6:
            raise ValidationError('cClassTrib IBS/CBS deve possuir 6 dígitos.')
        for pct_name in ('reducao_cbs_pct', 'reducao_ibs_pct', 'diferimento_cbs_pct', 'diferimento_ibs_pct'):
            val = _dec(data.get(pct_name, ZERO), RATE_PLACES, name=pct_name)
            if val > Decimal('100'):
                raise ValidationError(f'{pct_name} deve estar entre 0 e 100%.')
        return cls(
            cst=cst,
            c_class_trib=c_class,
            aliquota_cbs=_dec(data.get('aliquota_cbs', ZERO), RATE_PLACES, name='aliquota_cbs'),
            aliquota_ibs_uf=_dec(data.get('aliquota_ibs_uf', ZERO), RATE_PLACES, name='aliquota_ibs_uf'),
            aliquota_ibs_mun=_dec(data.get('aliquota_ibs_mun', ZERO), RATE_PLACES, name='aliquota_ibs_mun'),
            reducao_cbs_pct=_dec(data.get('reducao_cbs_pct', ZERO), RATE_PLACES, name='reducao_cbs_pct'),
            reducao_ibs_pct=_dec(data.get('reducao_ibs_pct', ZERO), RATE_PLACES, name='reducao_ibs_pct'),
            diferimento_cbs_pct=_dec(data.get('diferimento_cbs_pct', ZERO), RATE_PLACES, name='diferimento_cbs_pct'),
            diferimento_ibs_pct=_dec(data.get('diferimento_ibs_pct', ZERO), RATE_PLACES, name='diferimento_ibs_pct'),
            aliquota_is=_dec(data.get('aliquota_is', ZERO), RATE_PLACES, name='aliquota_is'),
            cst_is=str(data.get('cst_is') or '').strip(),
            c_class_trib_is=str(data.get('c_class_trib_is') or '').strip(),
            regime_transicao=str(data.get('regime_transicao') or 'PERSONALIZADO'),
            ind_doacao=bool(data.get('ind_doacao', False)),
        )


def _aliquota_efetiva(aliquota: Decimal, reducao_pct: Decimal, diferimento_pct: Decimal) -> Decimal:
    fator_red = Decimal('1') - (reducao_pct / Decimal('100'))
    fator_dif = Decimal('1') - (diferimento_pct / Decimal('100'))
    return (aliquota * fator_red * fator_dif).quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


def calcular_item_rtc(
    valor_base: Decimal | str | int | float,
    params: ParametrosReformaTributaria | dict[str, Any] | None = None,
    *,
    item_id: str = '1',
    descricao: str = '',
) -> dict[str, Any]:
    """Calcula CBS, IBS (UF + Municipal) e IS para um item fiscal."""
    cfg = params if isinstance(params, ParametrosReformaTributaria) else ParametrosReformaTributaria.from_dict(params)
    v_bc = _dec(valor_base, CENT, name='valor_base')

    p_cbs_ef = _aliquota_efetiva(cfg.aliquota_cbs, cfg.reducao_cbs_pct, cfg.diferimento_cbs_pct)
    p_ibs_uf_ef = _aliquota_efetiva(cfg.aliquota_ibs_uf, cfg.reducao_ibs_pct, cfg.diferimento_ibs_pct)
    p_ibs_mun_ef = _aliquota_efetiva(cfg.aliquota_ibs_mun, cfg.reducao_ibs_pct, cfg.diferimento_ibs_pct)

    v_cbs = (v_bc * p_cbs_ef / Decimal('100')).quantize(CENT, rounding=ROUND_HALF_UP)
    v_ibs_uf = (v_bc * p_ibs_uf_ef / Decimal('100')).quantize(CENT, rounding=ROUND_HALF_UP)
    v_ibs_mun = (v_bc * p_ibs_mun_ef / Decimal('100')).quantize(CENT, rounding=ROUND_HALF_UP)
    v_ibs = (v_ibs_uf + v_ibs_mun).quantize(CENT, rounding=ROUND_HALF_UP)
    v_is = (v_bc * cfg.aliquota_is / Decimal('100')).quantize(CENT, rounding=ROUND_HALF_UP) if cfg.aliquota_is > 0 else ZERO

    return {
        'item': str(item_id),
        'descricao': descricao[:120],
        'CST': cfg.cst,
        'cClassTrib': cfg.c_class_trib,
        'indDoacao': cfg.ind_doacao,
        'vBC': _money(v_bc),
        'cbs': {
            'pCBS': _rate(cfg.aliquota_cbs),
            'pCBS_efetiva': _rate(p_cbs_ef),
            'pRedAliq': _rate(cfg.reducao_cbs_pct),
            'pDif': _rate(cfg.diferimento_cbs_pct),
            'vCBS': _money(v_cbs),
        },
        'ibs': {
            'pIBSUF': _rate(cfg.aliquota_ibs_uf),
            'pIBSUF_efetiva': _rate(p_ibs_uf_ef),
            'vIBSUF': _money(v_ibs_uf),
            'pIBSMun': _rate(cfg.aliquota_ibs_mun),
            'pIBSMun_efetiva': _rate(p_ibs_mun_ef),
            'vIBSMun': _money(v_ibs_mun),
            'pRedAliq': _rate(cfg.reducao_ibs_pct),
            'pDif': _rate(cfg.diferimento_ibs_pct),
            'vIBS': _money(v_ibs),
        },
        'is': {
            'CSTIS': cfg.cst_is or '000',
            'cClassTribIS': cfg.c_class_trib_is or '000001',
            'vBCIS': _money(v_bc),
            'pIS': _rate(cfg.aliquota_is),
            'vIS': _money(v_is),
        } if cfg.aliquota_is > 0 else None,
    }


def gerar_nota_json_rtc(
    itens: list[dict[str, Any]],
    *,
    numero: str | int = '',
    chave: str = '',
    modelo: str = '',
    serie: str = '',
    data_emissao: str = '',
    regime_transicao: str = '2026_TESTE',
    default_params: ParametrosReformaTributaria | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Gera o objeto JSON canônico `nota.json` com blocos estruturados `"cbs"` e `"ibs"`."""
    base_params = (
        default_params
        if isinstance(default_params, ParametrosReformaTributaria)
        else (
            ParametrosReformaTributaria.from_dict(default_params)
            if default_params is not None
            else ParametrosReformaTributaria.from_preset(regime_transicao)
        )
    )

    calculados: list[dict[str, Any]] = []
    tot_bc = ZERO
    tot_cbs = ZERO
    tot_ibs_uf = ZERO
    tot_ibs_mun = ZERO
    tot_ibs = ZERO
    tot_is = ZERO

    for idx, row in enumerate(itens, start=1):
        val = row.get('valor_base')
        if val is None:
            val = row.get('liquido', row.get('valor', row.get('total')))
        if val is None and 'valor_unitario' in row:
            val = _dec(row.get('quantidade', 1), Decimal('0.0001')) * _dec(row.get('valor_unitario', 0), Decimal('0.0001'))
        if val is None:
            val = 0
        row_params = row.get('rtc') or row.get('reforma_tributaria') or base_params
        calc = calcular_item_rtc(
            val,
            row_params,
            item_id=str(row.get('nItem', idx)),
            descricao=str(row.get('descricao', '')),
        )
        calculados.append(calc)
        tot_bc += Decimal(calc['vBC'])
        tot_cbs += Decimal(calc['cbs']['vCBS'])
        tot_ibs_uf += Decimal(calc['ibs']['vIBSUF'])
        tot_ibs_mun += Decimal(calc['ibs']['vIBSMun'])
        tot_ibs += Decimal(calc['ibs']['vIBS'])
        if calc['is']:
            tot_is += Decimal(calc['is']['vIS'])

    p_cbs_ref = calculados[0]['cbs']['pCBS'] if calculados else _rate(base_params.aliquota_cbs)
    p_ibs_uf_ref = calculados[0]['ibs']['pIBSUF'] if calculados else _rate(base_params.aliquota_ibs_uf)
    p_ibs_mun_ref = calculados[0]['ibs']['pIBSMun'] if calculados else _rate(base_params.aliquota_ibs_mun)

    return {
        'numero': str(numero or chave or '0'),
        'chave': str(chave or ''),
        'modelo': str(modelo or ''),
        'serie': str(serie or ''),
        'data_emissao': str(data_emissao or ''),
        'reforma_tributaria': True,
        'regime_transicao': base_params.regime_transicao,
        'vBCIBSCBS': _money(tot_bc),
        'cbs': {
            'CST': calculados[0]['CST'] if calculados else base_params.cst,
            'cClassTrib': calculados[0]['cClassTrib'] if calculados else base_params.c_class_trib,
            'aliquota': p_cbs_ref,
            'vBC': _money(tot_bc),
            'vCBS': _money(tot_cbs),
            'itens': [
                {'item': c['item'], 'descricao': c['descricao'], 'vBC': c['vBC'], **c['cbs']}
                for c in calculados
            ],
        },
        'ibs': {
            'CST': calculados[0]['CST'] if calculados else base_params.cst,
            'cClassTrib': calculados[0]['cClassTrib'] if calculados else base_params.c_class_trib,
            'aliquota_uf': p_ibs_uf_ref,
            'aliquota_mun': p_ibs_mun_ref,
            'vBC': _money(tot_bc),
            'vIBSUF': _money(tot_ibs_uf),
            'vIBSMun': _money(tot_ibs_mun),
            'vIBS': _money(tot_ibs),
            'itens': [
                {'item': c['item'], 'descricao': c['descricao'], 'vBC': c['vBC'], **c['ibs']}
                for c in calculados
            ],
        },
        'is': {
            'vIS': _money(tot_is),
        },
        'totais': {
            'vProdServ': _money(tot_bc),
            'vCBS': _money(tot_cbs),
            'vIBS': _money(tot_ibs),
            'vIS': _money(tot_is),
            'vNFTot': _money(tot_bc + tot_cbs + tot_ibs + tot_is),
        },
        'itens_rtc': calculados,
    }


def anexar_ibscbs_item_xml(imposto_node: etree._Element, ns_prefix: str, item_rtc: dict[str, Any]) -> None:
    """Anexa os elementos XML `<IS>` (quando houver) e `<IBSCBS>` conforme DFeTiposBasicos_v1.00.xsd."""
    def sub(parent: etree._Element, name: str, text: str | None = None) -> etree._Element:
        el = etree.SubElement(parent, f'{ns_prefix}{name}')
        if text is not None:
            el.text = str(text)
        return el

    if item_rtc.get('is') and Decimal(item_rtc['is']['vIS']) > ZERO:
        is_node = sub(imposto_node, 'IS')
        is_data = item_rtc['is']
        sub(is_node, 'CSTIS', is_data['CSTIS'])
        sub(is_node, 'cClassTribIS', is_data['cClassTribIS'])
        sub(is_node, 'vBCIS', is_data['vBCIS'])
        sub(is_node, 'pIS', is_data['pIS'])
        sub(is_node, 'vIS', is_data['vIS'])

    ibscbs = sub(imposto_node, 'IBSCBS')
    sub(ibscbs, 'CST', item_rtc['CST'])
    sub(ibscbs, 'cClassTrib', item_rtc['cClassTrib'])
    if item_rtc.get('indDoacao'):
        sub(ibscbs, 'indDoacao', '1')

    g = sub(ibscbs, 'gIBSCBS')
    sub(g, 'vBC', item_rtc['vBC'])

    ibs = item_rtc['ibs']
    g_uf = sub(g, 'gIBSUF')
    sub(g_uf, 'pIBSUF', ibs['pIBSUF'])
    if Decimal(ibs['pRedAliq']) > ZERO:
        g_red = sub(g_uf, 'gRed')
        sub(g_red, 'pRedAliq', ibs['pRedAliq'])
        sub(g_red, 'pAliqEfet', ibs['pIBSUF_efetiva'])
    sub(g_uf, 'vIBSUF', ibs['vIBSUF'])

    g_mun = sub(g, 'gIBSMun')
    sub(g_mun, 'pIBSMun', ibs['pIBSMun'])
    if Decimal(ibs['pRedAliq']) > ZERO:
        g_red_m = sub(g_mun, 'gRed')
        sub(g_red_m, 'pRedAliq', ibs['pRedAliq'])
        sub(g_red_m, 'pAliqEfet', ibs['pIBSMun_efetiva'])
    sub(g_mun, 'vIBSMun', ibs['vIBSMun'])

    sub(g, 'vIBS', ibs['vIBS'])

    cbs = item_rtc['cbs']
    g_cbs = sub(g, 'gCBS')
    sub(g_cbs, 'pCBS', cbs['pCBS'])
    if Decimal(cbs['pRedAliq']) > ZERO:
        g_red_c = sub(g_cbs, 'gRed')
        sub(g_red_c, 'pRedAliq', cbs['pRedAliq'])
        sub(g_red_c, 'pAliqEfet', cbs['pCBS_efetiva'])
    sub(g_cbs, 'vCBS', cbs['vCBS'])


def anexar_ibscbs_total_xml(total_node: etree._Element, ns_prefix: str, nota_json: dict[str, Any]) -> None:
    """Anexa `<ISTot>`, `<IBSCBSTot>` e `<vNFTot>` dentro de `<total>` conforme NT 2025.002."""
    def sub(parent: etree._Element, name: str, text: str | None = None) -> etree._Element:
        el = etree.SubElement(parent, f'{ns_prefix}{name}')
        if text is not None:
            el.text = str(text)
        return el

    v_is = Decimal(nota_json.get('is', {}).get('vIS', '0.00'))
    if v_is > ZERO:
        is_tot = sub(total_node, 'ISTot')
        sub(is_tot, 'vIS', _money(v_is))

    tot = sub(total_node, 'IBSCBSTot')
    sub(tot, 'vBCIBSCBS', nota_json['vBCIBSCBS'])

    ibs_tot = sub(tot, 'gIBS')
    g_uf = sub(ibs_tot, 'gIBSUF')
    sub(g_uf, 'vDif', '0.00')
    sub(g_uf, 'vDevTrib', '0.00')
    sub(g_uf, 'vIBSUF', nota_json['ibs']['vIBSUF'])

    g_mun = sub(ibs_tot, 'gIBSMun')
    sub(g_mun, 'vDif', '0.00')
    sub(g_mun, 'vDevTrib', '0.00')
    sub(g_mun, 'vIBSMun', nota_json['ibs']['vIBSMun'])

    sub(ibs_tot, 'vIBS', nota_json['ibs']['vIBS'])
    sub(ibs_tot, 'vCredPres', '0.00')
    sub(ibs_tot, 'vCredPresCondSus', '0.00')

    cbs_tot = sub(tot, 'gCBS')
    sub(cbs_tot, 'vDif', '0.00')
    sub(cbs_tot, 'vDevTrib', '0.00')
    sub(cbs_tot, 'vCBS', nota_json['cbs']['vCBS'])
    sub(cbs_tot, 'vCredPres', '0.00')
    sub(cbs_tot, 'vCredPresCondSus', '0.00')


def extrair_ibscbs_de_xml(root: etree._Element) -> dict[str, Any]:
    """Extrai valores e grupos IBS/CBS/IS de qualquer XML NF-e / NFC-e / procNFe recebido."""
    def find_text(el: etree._Element, name: str, default: str = '0.00') -> str:
        node = el.find(f'.//{{*}}{name}')
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
        return default

    tot_node = root.find('.//{*}IBSCBSTot')
    is_tot_node = root.find('.//{*}ISTot')
    item_nodes = root.findall('.//{*}det')

    itens_rtc: list[dict[str, Any]] = []
    for idx, det in enumerate(item_nodes, start=1):
        n_item = det.get('nItem') or str(idx)
        x_prod = find_text(det, 'xProd', '')
        ibscbs = det.find('.//{*}IBSCBS')
        if ibscbs is None:
            continue
        v_bc = _dec(find_text(ibscbs, 'vBC', '0.00'))
        v_cbs = _dec(find_text(ibscbs, 'vCBS', '0.00'))
        v_ibs_uf = _dec(find_text(ibscbs, 'vIBSUF', '0.00'))
        v_ibs_mun = _dec(find_text(ibscbs, 'vIBSMun', '0.00'))
        v_ibs = _dec(find_text(ibscbs, 'vIBS', _money(v_ibs_uf + v_ibs_mun)))
        p_cbs = _dec(find_text(ibscbs, 'pCBS', '0.0000'), RATE_PLACES)
        p_ibs_uf = _dec(find_text(ibscbs, 'pIBSUF', '0.0000'), RATE_PLACES)
        p_ibs_mun = _dec(find_text(ibscbs, 'pIBSMun', '0.0000'), RATE_PLACES)
        itens_rtc.append({
            'item': n_item,
            'descricao': x_prod,
            'CST': find_text(ibscbs, 'CST', '000'),
            'cClassTrib': find_text(ibscbs, 'cClassTrib', '000001'),
            'vBC': _money(v_bc),
            'cbs': {'pCBS': _rate(p_cbs), 'vCBS': _money(v_cbs)},
            'ibs': {'pIBSUF': _rate(p_ibs_uf), 'vIBSUF': _money(v_ibs_uf), 'pIBSMun': _rate(p_ibs_mun), 'vIBSMun': _money(v_ibs_mun), 'vIBS': _money(v_ibs)},
        })

    if tot_node is not None:
        v_bc_tot = _dec(find_text(tot_node, 'vBCIBSCBS', '0.00'))
        v_cbs_tot = _dec(find_text(tot_node, 'vCBS', '0.00'))
        v_ibs_uf_tot = _dec(find_text(tot_node, 'vIBSUF', '0.00'))
        v_ibs_mun_tot = _dec(find_text(tot_node, 'vIBSMun', '0.00'))
        v_ibs_tot = _dec(find_text(tot_node, 'vIBS', _money(v_ibs_uf_tot + v_ibs_mun_tot)))
    else:
        v_bc_tot = sum((_dec(i['vBC']) for i in itens_rtc), ZERO)
        v_cbs_tot = sum((_dec(i['cbs']['vCBS']) for i in itens_rtc), ZERO)
        v_ibs_uf_tot = sum((_dec(i['ibs']['vIBSUF']) for i in itens_rtc), ZERO)
        v_ibs_mun_tot = sum((_dec(i['ibs']['vIBSMun']) for i in itens_rtc), ZERO)
        v_ibs_tot = sum((_dec(i['ibs']['vIBS']) for i in itens_rtc), ZERO)

    v_is_tot = _dec(find_text(is_tot_node, 'vIS', '0.00')) if is_tot_node is not None else ZERO
    numero = find_text(root, 'nNF', '') or find_text(root, 'nNFSe', '')
    inf_nfe = root.find('.//{*}infNFe')
    chave = (inf_nfe.get('Id') or '').removeprefix('NFe') if inf_nfe is not None else find_text(root, 'chNFe', '')
    v_nf = _dec(find_text(root, 'vNF', '0.00'))

    return {
        'numero': numero or chave,
        'chave': chave,
        'reforma_tributaria': bool(itens_rtc or tot_node is not None),
        'vBCIBSCBS': _money(v_bc_tot),
        'cbs': {
            'vBC': _money(v_bc_tot),
            'vCBS': _money(v_cbs_tot),
            'itens': [{'item': i['item'], 'descricao': i['descricao'], 'vBC': i['vBC'], **i['cbs']} for i in itens_rtc],
        },
        'ibs': {
            'vBC': _money(v_bc_tot),
            'vIBSUF': _money(v_ibs_uf_tot),
            'vIBSMun': _money(v_ibs_mun_tot),
            'vIBS': _money(v_ibs_tot),
            'itens': [{'item': i['item'], 'descricao': i['descricao'], 'vBC': i['vBC'], **i['ibs']} for i in itens_rtc],
        },
        'is': {'vIS': _money(v_is_tot)},
        'totais': {
            'vNF': _money(v_nf),
            'vCBS': _money(v_cbs_tot),
            'vIBS': _money(v_ibs_tot),
            'vIS': _money(v_is_tot),
        },
    }
