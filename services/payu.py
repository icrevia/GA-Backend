import hashlib
import hmac
from core.config import settings

def generate_payu_hash(txnid: str, amount: float, productinfo: str, firstname: str, email: str, udf1: str = "", udf2: str = "", udf3: str = "", udf4: str = "", udf5: str = "") -> str:
    """
    Generate hash for PayU request.
    Hash format: key|txnid|amount|productinfo|firstname|email|udf1|udf2|udf3|udf4|udf5||||||SALT
    """
    amount_str = f"{amount:.2f}" # PayU expects 2 decimal places usually
    hash_string = f"{settings.PAYU_MERCHANT_KEY}|{txnid}|{amount_str}|{productinfo}|{firstname}|{email}|{udf1}|{udf2}|{udf3}|{udf4}|{udf5}||||||{settings.PAYU_MERCHANT_SALT}"
    return hashlib.sha512(hash_string.encode('utf-8')).hexdigest()

def verify_payu_hash(txnid: str, amount: float, productinfo: str, firstname: str, email: str, status: str, received_hash: str, udf1: str = "", udf2: str = "", udf3: str = "", udf4: str = "", udf5: str = "") -> bool:
    """
    Verify hash from PayU response (webhook/success callback).
    Reverse hash format: SALT|status||||||udf5|udf4|udf3|udf2|u1|email|firstname|productinfo|amount|txnid|key
    """
    amount_str = f"{amount:.2f}"
    hash_string = f"{settings.PAYU_MERCHANT_SALT}|{status}||||||{udf5}|{udf4}|{udf3}|{udf2}|{udf1}|{email}|{firstname}|{productinfo}|{amount_str}|{txnid}|{settings.PAYU_MERCHANT_KEY}"
    expected_hash = hashlib.sha512(hash_string.encode('utf-8')).hexdigest()
    
    # Use hmac.compare_digest to prevent timing attacks
    return hmac.compare_digest(expected_hash, received_hash)
