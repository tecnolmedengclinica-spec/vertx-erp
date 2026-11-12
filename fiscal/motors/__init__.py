from .nfse import MotorNFSeSimulado
from .nfe import MotorNFeSimulado, MotorNFeHomologacao
from .nfce import MotorNFCeSimulado

def resolver(tipo, ambiente='SIMULACAO', motor=None):
    if motor in ('NFSE_ADN', 'NFSE_ADN_HOM') or tipo in ('NFSE_ADN', 'NFSE_ADN_HOM'):
        from .nfse_adn import MotorNFSeADN
        return MotorNFSeADN()
    if motor == 'NFSE_RPS_CONTINGENCIA' or tipo == 'NFSE_RPS_CONTINGENCIA':
        from fiscal.nfse.contingencia import MotorNFSeContingencia
        return MotorNFSeContingencia()
    if motor == 'NFE_SEFAZ_HOM' or tipo == 'NFE_SEFAZ_HOM':
        return MotorNFeHomologacao()
    if ambiente == 'HOMOLOGACAO':
        if tipo != 'NFCE':
            from django.core.exceptions import ValidationError
            raise ValidationError('Homologação desta fase é exclusiva de NFC-e.')
        from .nfce_homologacao import MotorNFCeHomologacao
        return MotorNFCeHomologacao()
    if ambiente != 'SIMULACAO':
        from django.core.exceptions import ValidationError
        raise ValidationError('Produção fiscal não habilitada.')
    return {'NFSE': MotorNFSeSimulado, 'NFE': MotorNFeSimulado, 'NFCE': MotorNFCeSimulado}[tipo]()
