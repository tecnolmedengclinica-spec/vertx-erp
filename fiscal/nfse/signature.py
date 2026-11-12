"""Módulo de Assinatura Digital XMLDSig (RSA-SHA256) para a NFS-e Nacional."""
import base64
import hashlib
from lxml import etree
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography import x509

XMLDSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
C14N_ALGO = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
ENVELOPED_ALGO = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
SHA256_DIGEST_ALGO = "http://www.w3.org/2001/04/xmlenc#sha256"
RSA_SHA256_SIG_ALGO = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"


def assinar_xml_dps(xml_bytes: bytes, chave_privada: RSAPrivateKey, certificado: x509.Certificate) -> bytes:
    """Assina digitalmente o documento XML da DPS usando XMLDSig padrão ICP-Brasil (RSA-SHA256)."""
    parser = etree.XMLParser(remove_blank_text=True)
    root = etree.fromstring(xml_bytes, parser=parser)

    # Localiza o elemento a ser assinado (<infDPS>)
    inf_dps = root.find("{*}infDPS")
    if inf_dps is None:
        raise ValueError("Elemento infDPS não encontrado no XML fornecido.")

    id_dps = inf_dps.get("Id")
    if not id_dps:
        raise ValueError("Atributo Id ausente no elemento infDPS.")

    # 1. Canonicalização C14N do elemento infDPS e cálculo do DigestValue
    c14n_inf_dps = etree.tostring(inf_dps, method="c14n", exclusive=False, with_comments=False)
    digest_bytes = hashlib.sha256(c14n_inf_dps).digest()
    digest_b64 = base64.b64encode(digest_bytes).decode("ascii")

    # 2. Construção do nó <Signature> com SignedInfo
    ds = f"{{{XMLDSIG_NS}}}"
    signature = etree.SubElement(root, ds + "Signature", nsmap={None: XMLDSIG_NS})

    signed_info = etree.SubElement(signature, ds + "SignedInfo")
    etree.SubElement(signed_info, ds + "CanonicalizationMethod", Algorithm=C14N_ALGO)
    etree.SubElement(signed_info, ds + "SignatureMethod", Algorithm=RSA_SHA256_SIG_ALGO)

    reference = etree.SubElement(signed_info, ds + "Reference", URI=f"#{id_dps}")
    transforms = etree.SubElement(reference, ds + "Transforms")
    etree.SubElement(transforms, ds + "Transform", Algorithm=ENVELOPED_ALGO)
    etree.SubElement(transforms, ds + "Transform", Algorithm=C14N_ALGO)

    etree.SubElement(reference, ds + "DigestMethod", Algorithm=SHA256_DIGEST_ALGO)
    etree.SubElement(reference, ds + "DigestValue").text = digest_b64

    # 3. Canonicalização C14N do SignedInfo e cálculo da SignatureValue
    c14n_signed_info = etree.tostring(signed_info, method="c14n", exclusive=False, with_comments=False)
    signature_bytes = chave_privada.sign(
        c14n_signed_info,
        padding.PKCS1v15(),
        hashes.SHA256()
    )
    signature_b64 = base64.b64encode(signature_bytes).decode("ascii")
    etree.SubElement(signature, ds + "SignatureValue").text = signature_b64

    # 4. Inclusão dos dados do Certificado Digital X.509 em KeyInfo
    key_info = etree.SubElement(signature, ds + "KeyInfo")
    x509_data = etree.SubElement(key_info, ds + "X509Data")
    cert_der = certificado.public_bytes(Encoding.DER)
    cert_b64 = base64.b64encode(cert_der).decode("ascii")
    etree.SubElement(x509_data, ds + "X509Certificate").text = cert_b64

    return etree.tostring(root, encoding="utf-8", xml_declaration=True, pretty_print=True)


def verificar_assinatura_dps(xml_assinado_bytes: bytes) -> bool:
    """Valida a integridade da assinatura XMLDSig de uma DPS assinada."""
    parser = etree.XMLParser(remove_blank_text=True)
    root = etree.fromstring(xml_assinado_bytes, parser=parser)

    inf_dps = root.find("{*}infDPS")
    signature = root.find("{*}Signature")
    if inf_dps is None or signature is None:
        return False

    ds = f"{{{XMLDSIG_NS}}}"
    signed_info = signature.find(ds + "SignedInfo")
    sig_val_elem = signature.find(ds + "SignatureValue")
    cert_elem = signature.find(f".//{ds}X509Certificate")

    if signed_info is None or sig_val_elem is None or cert_elem is None:
        return False

    # 1. Verificar DigestValue
    ref_elem = signed_info.find(ds + "Reference")
    digest_elem = ref_elem.find(ds + "DigestValue") if ref_elem is not None else None
    if digest_elem is None:
        return False

    c14n_inf_dps = etree.tostring(inf_dps, method="c14n", exclusive=False, with_comments=False)
    computed_digest = base64.b64encode(hashlib.sha256(c14n_inf_dps).digest()).decode("ascii")
    if computed_digest != digest_elem.text.strip():
        return False

    # 2. Verificar RSA Signature
    try:
        cert_der = base64.b64decode(cert_elem.text.strip())
        cert = x509.load_der_x509_certificate(cert_der)
        public_key = cert.public_key()
        sig_bytes = base64.b64decode(sig_val_elem.text.strip())

        c14n_signed_info = etree.tostring(signed_info, method="c14n", exclusive=False, with_comments=False)
        public_key.verify(
            sig_bytes,
            c14n_signed_info,
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        return True
    except Exception:
        return False
